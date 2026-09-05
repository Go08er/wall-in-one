//! Optional system-power observation. Only the runtime thread changes wallpaper
//! policy; this worker owns a private system-bus connection and a single latest
//! observation, never an unbounded event queue or a child process.
use std::ffi::{c_char, c_int, c_void};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum PowerSource {
    Ac,
    Battery,
    #[default]
    Unknown,
}

impl PowerSource {
    pub fn name(self) -> &'static str {
        match self {
            Self::Ac => "ac",
            Self::Battery => "battery",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PowerObservation {
    pub source: PowerSource,
    // Preserve the last reliable state even when a short Battery -> Unknown
    // burst is coalesced before the runtime next checks its mailbox.
    pub last_known: PowerSource,
}

impl PowerObservation {
    pub fn observe(&mut self, source: PowerSource) {
        self.source = source;
        if source != PowerSource::Unknown {
            self.last_known = source;
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct PowerPolicy {
    pub observation: PowerObservation,
}

#[derive(Default)]
struct StablePower {
    observation: PowerObservation,
    ac_since: Option<Instant>,
}

impl StablePower {
    fn sample(&mut self, source: PowerSource, now: Instant) -> (PowerObservation, Duration) {
        // Suppression is immediate; restoring animation needs a stable AC
        // reading, preventing connector bounce from repeatedly spawning media.
        if source == PowerSource::Ac && self.observation.last_known == PowerSource::Battery {
            let since = *self.ac_since.get_or_insert(now);
            let remaining =
                Duration::from_millis(500).saturating_sub(now.saturating_duration_since(since));
            if !remaining.is_zero() {
                return (self.observation, remaining);
            }
        }
        self.ac_since = None;
        self.observation.observe(source);
        (
            self.observation,
            if source == PowerSource::Unknown {
                Duration::from_secs(5)
            } else {
                Duration::from_secs(3600)
            },
        )
    }
}

impl PowerPolicy {
    pub fn inhibited(&self, enabled: bool) -> bool {
        enabled && self.observation.last_known == PowerSource::Battery
    }

    pub fn reason(&self, enabled: bool) -> &'static str {
        if !self.inhibited(enabled) {
            ""
        } else if self.observation.source == PowerSource::Unknown {
            "power-unavailable"
        } else {
            "battery"
        }
    }
}

#[derive(Default)]
struct Mailbox {
    enabled: bool,
    shutdown: bool,
    generation: u64,
    sampled: u64,
    observation: PowerObservation,
}

#[derive(Default)]
pub struct PowerObserver {
    shared: Arc<(Mutex<Mailbox>, Condvar)>,
    started: bool,
}

impl PowerObserver {
    /// Preflight a validated candidate without disabling an active observer
    /// until reload commits. This avoids losing a held battery observation if
    /// a candidate disabling the option is subsequently rejected.
    pub fn prepare(&mut self, enabled: bool) -> PowerObservation {
        if enabled {
            self.synchronize(true)
        } else {
            self.shared
                .0
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .observation
        }
    }

    /// A bounded initial snapshot before the first apply. Disabled-by-default
    /// sessions allocate no observer thread and never connect to the bus.
    pub fn synchronize(&mut self, enabled: bool) -> PowerObservation {
        let (lock, wake) = &*self.shared;
        let mut state = lock.lock().unwrap_or_else(|error| error.into_inner());
        if state.enabled != enabled {
            state.enabled = enabled;
            state.generation = state.generation.wrapping_add(1);
            state.observation = PowerObservation::default();
            wake.notify_all();
            if enabled {
                if !self.started {
                    let shared = Arc::clone(&self.shared);
                    if thread::Builder::new()
                        .name("wall-power".into())
                        .stack_size(128 * 1024)
                        .spawn(move || observe_bus(shared))
                        .is_ok()
                    {
                        self.started = true;
                    }
                }
                let generation = state.generation;
                (state, _) = wake
                    .wait_timeout_while(state, Duration::from_millis(750), |state| {
                        state.sampled != generation
                    })
                    .unwrap_or_else(|error| error.into_inner());
            }
        }
        state.observation
    }
}

impl Drop for PowerObserver {
    fn drop(&mut self) {
        let (lock, wake) = &*self.shared;
        let mut state = lock.lock().unwrap_or_else(|error| error.into_inner());
        state.shutdown = true;
        wake.notify_all();
        // Never join a bus authentication blocked outside our method timeout.
        // There is at most one worker for this observer's whole lifetime.
    }
}

fn observe_bus(shared: Arc<(Mutex<Mailbox>, Condvar)>) {
    // SAFETY: this process has no other libdbus users. Initialize its internal
    // synchronization before creating the worker's sole private connection.
    if unsafe { dbus_threads_init_default() } == 0 {
        return;
    }
    let (lock, wake) = &*shared;
    loop {
        let mut state = lock.lock().unwrap_or_else(|error| error.into_inner());
        while !state.enabled && !state.shutdown {
            state = wake.wait(state).unwrap_or_else(|error| error.into_inner());
        }
        if state.shutdown {
            return;
        }
        let generation = state.generation;
        drop(state);
        let mut connection = Bus::open();
        let mut next_query = Instant::now();
        let mut last_query = Instant::now() - Duration::from_secs(1);
        let mut stable = StablePower::default();
        loop {
            let now = Instant::now();
            let mut state = lock.lock().unwrap_or_else(|error| error.into_inner());
            if state.shutdown {
                return;
            }
            if !state.enabled || state.generation != generation {
                break;
            }
            drop(state);
            if now >= next_query {
                let source = connection.as_mut().map_or(PowerSource::Unknown, Bus::query);
                last_query = Instant::now();
                let (observation, refresh_after) = stable.sample(source, last_query);
                state = lock.lock().unwrap_or_else(|error| error.into_inner());
                if state.generation == generation && state.enabled {
                    state.observation = observation;
                    state.sampled = generation;
                    wake.notify_all();
                }
                // Healthy subscriptions get a defensive hourly refresh;
                // missing providers are retried every five seconds.
                next_query = last_query + refresh_after;
                drop(state);
            }
            if let Some(bus) = connection.as_mut() {
                match bus.changed() {
                    Some(true) => {
                        next_query = next_query
                            .min(Instant::now().max(last_query + Duration::from_millis(250)));
                    }
                    Some(false) => {}
                    None => {
                        connection = None;
                        next_query = Instant::now();
                    }
                }
            } else {
                let state = lock.lock().unwrap_or_else(|error| error.into_inner());
                let (state, _) = wake
                    .wait_timeout(state, Duration::from_secs(1))
                    .unwrap_or_else(|error| error.into_inner());
                if state.shutdown {
                    return;
                }
                drop(state);
                if Instant::now() >= next_query {
                    connection = Bus::open();
                }
            }
        }
    }
}

// These are the stable public libdbus C ABI types/functions, checked against
// dbus-message.h. Opaque handles never leave their creating worker thread.
#[repr(C)]
struct MessageIter {
    pointers: [*mut c_void; 2],
    serial: u32,
    integers: [c_int; 9],
    padding: [*mut c_void; 2],
}

#[link(name = "dbus-1")]
unsafe extern "C" {
    fn dbus_threads_init_default() -> u32;
    fn dbus_bus_get_private(kind: c_int, error: *mut c_void) -> *mut c_void;
    fn dbus_connection_set_exit_on_disconnect(connection: *mut c_void, enabled: u32);
    fn dbus_connection_close(connection: *mut c_void);
    fn dbus_connection_unref(connection: *mut c_void);
    fn dbus_bus_add_match(connection: *mut c_void, rule: *const c_char, error: *mut c_void);
    fn dbus_connection_read_write(connection: *mut c_void, timeout: c_int) -> u32;
    fn dbus_connection_pop_message(connection: *mut c_void) -> *mut c_void;
    fn dbus_connection_send_with_reply_and_block(
        connection: *mut c_void,
        message: *mut c_void,
        timeout: c_int,
        error: *mut c_void,
    ) -> *mut c_void;
    fn dbus_message_new_method_call(
        destination: *const c_char,
        path: *const c_char,
        interface: *const c_char,
        method: *const c_char,
    ) -> *mut c_void;
    fn dbus_message_append_args(message: *mut c_void, first_type: c_int, ...) -> u32;
    fn dbus_message_unref(message: *mut c_void);
    fn dbus_message_is_signal(
        message: *mut c_void,
        interface: *const c_char,
        signal: *const c_char,
    ) -> u32;
    fn dbus_message_iter_init(message: *mut c_void, iter: *mut MessageIter) -> u32;
    fn dbus_message_iter_get_arg_type(iter: *mut MessageIter) -> c_int;
    fn dbus_message_iter_recurse(iter: *mut MessageIter, child: *mut MessageIter);
    fn dbus_message_iter_get_basic(iter: *mut MessageIter, value: *mut c_void);
}

struct Bus(*mut c_void);

impl Bus {
    fn open() -> Option<Self> {
        // SAFETY: all strings are static NUL-terminated literals. Passing a
        // null DBusError is explicitly supported; failures become Unknown.
        unsafe {
            let connection = dbus_bus_get_private(1, std::ptr::null_mut());
            if connection.is_null() {
                return None;
            }
            Some(Self::subscribe(connection))
        }
    }

    unsafe fn subscribe(connection: *mut c_void) -> Self {
        // SAFETY: caller transfers its unique private connection ownership.
        unsafe {
            dbus_connection_set_exit_on_disconnect(connection, 0);
            for rule in [
                c"type='signal',sender='org.freedesktop.UPower',path='/org/freedesktop/UPower',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged'",
                c"type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.freedesktop.UPower'",
                c"type='signal',sender='org.freedesktop.login1',interface='org.freedesktop.login1.Manager',member='PrepareForSleep'",
            ] {
                // A null error requests nonblocking match installation.
                dbus_bus_add_match(connection, rule.as_ptr(), std::ptr::null_mut());
            }
            Self(connection)
        }
    }

    fn query(&mut self) -> PowerSource {
        // SAFETY: this worker exclusively owns the connection; every owned
        // message is unreferenced once, and iterator types are checked before
        // reading the boolean. No unchecked external string is dereferenced.
        unsafe {
            let message = dbus_message_new_method_call(
                c"org.freedesktop.UPower".as_ptr(),
                c"/org/freedesktop/UPower".as_ptr(),
                c"org.freedesktop.DBus.Properties".as_ptr(),
                c"Get".as_ptr(),
            );
            if message.is_null() {
                return PowerSource::Unknown;
            }
            let interface = c"org.freedesktop.UPower".as_ptr();
            let property = c"OnBattery".as_ptr();
            let appended = dbus_message_append_args(
                message,
                b's' as c_int,
                &interface,
                b's' as c_int,
                &property,
                0 as c_int,
            );
            let reply = if appended != 0 {
                dbus_connection_send_with_reply_and_block(
                    self.0,
                    message,
                    500,
                    std::ptr::null_mut(),
                )
            } else {
                std::ptr::null_mut()
            };
            dbus_message_unref(message);
            if reply.is_null() {
                return PowerSource::Unknown;
            }
            let mut outer: MessageIter = std::mem::zeroed();
            let mut inner: MessageIter = std::mem::zeroed();
            let mut value = 0_u32;
            let mut source = PowerSource::Unknown;
            if dbus_message_iter_init(reply, &mut outer) != 0
                && dbus_message_iter_get_arg_type(&mut outer) == b'v' as c_int
            {
                dbus_message_iter_recurse(&mut outer, &mut inner);
                if dbus_message_iter_get_arg_type(&mut inner) == b'b' as c_int {
                    dbus_message_iter_get_basic(&mut inner, (&mut value as *mut u32).cast());
                    source = if value != 0 {
                        PowerSource::Battery
                    } else {
                        PowerSource::Ac
                    };
                }
            }
            dbus_message_unref(reply);
            source
        }
    }

    fn changed(&mut self) -> Option<bool> {
        // SAFETY: bounded non-dispatching reads use the worker-owned private
        // connection; every popped message is released, including irrelevant
        // bus replies. Only signals trigger a fresh authoritative Get.
        unsafe {
            if dbus_connection_read_write(self.0, 500) == 0 {
                return None;
            }
            let mut changed = false;
            for _ in 0..64 {
                let message = dbus_connection_pop_message(self.0);
                if message.is_null() {
                    break;
                }
                changed |= dbus_message_is_signal(
                    message,
                    c"org.freedesktop.DBus.Properties".as_ptr(),
                    c"PropertiesChanged".as_ptr(),
                ) != 0
                    || dbus_message_is_signal(
                        message,
                        c"org.freedesktop.DBus".as_ptr(),
                        c"NameOwnerChanged".as_ptr(),
                    ) != 0
                    || dbus_message_is_signal(
                        message,
                        c"org.freedesktop.login1.Manager".as_ptr(),
                        c"PrepareForSleep".as_ptr(),
                    ) != 0;
                dbus_message_unref(message);
            }
            Some(changed)
        }
    }
}

impl Drop for Bus {
    fn drop(&mut self) {
        // SAFETY: this is a private connection, owned exactly once by Bus.
        unsafe {
            dbus_connection_close(self.0);
            dbus_connection_unref(self.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::{CStr, CString};
    use std::io::{BufRead, BufReader, Write};
    use std::process::{Child, Command, Stdio};
    use std::sync::mpsc;

    unsafe extern "C" {
        fn dbus_error_init(error: *mut BusError);
        fn dbus_error_free(error: *mut BusError);
        fn dbus_connection_open_private(address: *const c_char, error: *mut c_void) -> *mut c_void;
        fn dbus_bus_register(connection: *mut c_void, error: *mut c_void) -> u32;
        fn dbus_bus_request_name(
            connection: *mut c_void,
            name: *const c_char,
            flags: u32,
            error: *mut c_void,
        ) -> c_int;
        fn dbus_message_is_method_call(
            message: *mut c_void,
            interface: *const c_char,
            member: *const c_char,
        ) -> u32;
        fn dbus_message_new_method_return(message: *mut c_void) -> *mut c_void;
        fn dbus_message_new_signal(
            path: *const c_char,
            interface: *const c_char,
            name: *const c_char,
        ) -> *mut c_void;
        fn dbus_message_iter_init_append(message: *mut c_void, iter: *mut MessageIter);
        fn dbus_message_iter_open_container(
            iter: *mut MessageIter,
            kind: c_int,
            signature: *const c_char,
            child: *mut MessageIter,
        ) -> u32;
        fn dbus_message_iter_append_basic(
            iter: *mut MessageIter,
            kind: c_int,
            value: *const c_void,
        ) -> u32;
        fn dbus_message_iter_close_container(
            iter: *mut MessageIter,
            child: *mut MessageIter,
        ) -> u32;
        fn dbus_connection_send(
            connection: *mut c_void,
            message: *mut c_void,
            serial: *mut u32,
        ) -> u32;
        fn dbus_connection_flush(connection: *mut c_void);
    }

    #[repr(C)]
    struct BusError {
        name: *const c_char,
        message: *const c_char,
        flags: u32,
        padding: *mut c_void,
    }

    struct TestDaemon(Child);

    struct TestConfig(std::path::PathBuf);

    impl Drop for TestConfig {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }

    impl Drop for TestDaemon {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    fn test_bus(address: &CString) -> Bus {
        unsafe {
            let mut error: BusError = std::mem::zeroed();
            dbus_error_init(&mut error);
            let connection = dbus_connection_open_private(
                address.as_ptr(),
                (&mut error as *mut BusError).cast(),
            );
            let diagnostic = if error.message.is_null() {
                String::new()
            } else {
                CStr::from_ptr(error.message).to_string_lossy().into_owned()
            };
            dbus_error_free(&mut error);
            assert!(
                !connection.is_null(),
                "cannot connect to disposable bus: {diagnostic}"
            );
            assert_ne!(dbus_bus_register(connection, std::ptr::null_mut()), 0);
            Bus::subscribe(connection)
        }
    }

    #[test]
    fn isolated_bus_checks_boolean_reply_signals_and_provider_loss() {
        // All traffic stays on this disposable session bus; no environment
        // overrides, system bus, real UPower provider or desktop is touched.
        unsafe {
            assert_ne!(dbus_threads_init_default(), 0);
        }
        // An explicit config avoids a dependency on /etc/dbus-1/session.conf,
        // and deliberately exposes no host service-activation directories.
        let config = TestConfig(std::env::temp_dir().join(format!(
            "wall-in-one-power-bus-{}-{}.conf",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        )));
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&config.0)
            .unwrap()
            .write_all(
                br#"<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
    <allow own="*"/>
  </policy>
</busconfig>"#,
            )
            .unwrap();
        let mut daemon = TestDaemon(
            Command::new("dbus-daemon")
                .arg("--config-file")
                .arg(&config.0)
                .args(["--nofork", "--nopidfile", "--print-address=1"])
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
                .expect("dbus-daemon is required for the isolated power-observer test"),
        );
        let mut address = String::new();
        BufReader::new(daemon.0.stdout.take().unwrap())
            .read_line(&mut address)
            .unwrap();
        assert!(
            !address.trim().is_empty(),
            "disposable bus did not publish an address"
        );
        let address = CString::new(address.trim()).unwrap();
        let server_address = address.clone();
        let (ready, started) = mpsc::sync_channel(1);
        let server = thread::spawn(move || {
            let connection = test_bus(&server_address);
            unsafe {
                assert_eq!(
                    dbus_bus_request_name(
                        connection.0,
                        c"org.freedesktop.UPower".as_ptr(),
                        0,
                        std::ptr::null_mut()
                    ),
                    1
                );
            }
            ready.send(()).unwrap();
            let deadline = Instant::now() + Duration::from_secs(5);
            let mut replies = 0;
            while replies < 2 && Instant::now() < deadline {
                unsafe {
                    dbus_connection_read_write(connection.0, 100);
                    let message = dbus_connection_pop_message(connection.0);
                    if message.is_null() {
                        continue;
                    }
                    if dbus_message_is_method_call(
                        message,
                        c"org.freedesktop.DBus.Properties".as_ptr(),
                        c"Get".as_ptr(),
                    ) != 0
                    {
                        let reply = dbus_message_new_method_return(message);
                        assert!(!reply.is_null());
                        let mut outer: MessageIter = std::mem::zeroed();
                        let mut inner: MessageIter = std::mem::zeroed();
                        let value = if replies == 0 { 1_u32 } else { 0_u32 };
                        dbus_message_iter_init_append(reply, &mut outer);
                        assert_ne!(
                            dbus_message_iter_open_container(
                                &mut outer,
                                b'v' as c_int,
                                c"b".as_ptr(),
                                &mut inner
                            ),
                            0
                        );
                        assert_ne!(
                            dbus_message_iter_append_basic(
                                &mut inner,
                                b'b' as c_int,
                                (&value as *const u32).cast()
                            ),
                            0
                        );
                        assert_ne!(dbus_message_iter_close_container(&mut outer, &mut inner), 0);
                        assert_ne!(
                            dbus_connection_send(connection.0, reply, std::ptr::null_mut()),
                            0
                        );
                        dbus_message_unref(reply);
                        replies += 1;
                        if replies == 1 {
                            // Invalidated/changed property contents both cause
                            // the observer to obtain a fresh authoritative Get.
                            let signal = dbus_message_new_signal(
                                c"/org/freedesktop/UPower".as_ptr(),
                                c"org.freedesktop.DBus.Properties".as_ptr(),
                                c"PropertiesChanged".as_ptr(),
                            );
                            assert!(!signal.is_null());
                            assert_ne!(
                                dbus_connection_send(connection.0, signal, std::ptr::null_mut()),
                                0
                            );
                            dbus_message_unref(signal);
                        }
                        dbus_connection_flush(connection.0);
                    }
                    dbus_message_unref(message);
                }
            }
            assert_eq!(replies, 2);
        });
        started.recv_timeout(Duration::from_secs(2)).unwrap();
        let mut client = test_bus(&address);
        assert_eq!(client.query(), PowerSource::Battery);
        assert_eq!(client.changed(), Some(true));
        assert_eq!(client.query(), PowerSource::Ac);
        server.join().unwrap();
        assert_eq!(client.changed(), Some(true));
        assert_eq!(client.query(), PowerSource::Unknown);
    }

    #[test]
    fn unknown_is_not_ac_and_inhibition_never_rewrites_the_option() {
        let mut policy = PowerPolicy::default();
        assert!(!policy.inhibited(true));
        policy.observation.observe(PowerSource::Battery);
        assert!(policy.inhibited(true));
        assert!(!policy.inhibited(false));
        policy.observation.observe(PowerSource::Unknown);
        assert!(policy.inhibited(true));
        assert_eq!(policy.reason(true), "power-unavailable");
        policy.observation.observe(PowerSource::Ac);
        assert!(!policy.inhibited(true));
    }

    #[test]
    fn disabled_observer_never_starts_a_thread_or_connects() {
        let mut observer = PowerObserver::default();
        assert_eq!(observer.synchronize(false), PowerObservation::default());
        assert!(!observer.started);
    }

    #[test]
    fn preparing_a_disabled_candidate_preserves_observation_until_commit() {
        let mut observer = PowerObserver::default();
        {
            let mut state = observer.shared.0.lock().unwrap();
            state.enabled = true;
            state.observation.observe(PowerSource::Battery);
        }
        assert_eq!(observer.prepare(false).last_known, PowerSource::Battery);
        assert!(observer.shared.0.lock().unwrap().enabled);
        assert_eq!(observer.synchronize(false), PowerObservation::default());
        assert!(!observer.shared.0.lock().unwrap().enabled);
    }

    #[test]
    fn brief_ac_bounce_does_not_release_battery_inhibition() {
        let mut stable = StablePower::default();
        let now = Instant::now();
        assert_eq!(
            stable.sample(PowerSource::Battery, now).0.source,
            PowerSource::Battery
        );
        assert_eq!(
            stable.sample(PowerSource::Ac, now).0.source,
            PowerSource::Battery
        );
        assert_eq!(
            stable
                .sample(PowerSource::Battery, now + Duration::from_millis(300))
                .0
                .source,
            PowerSource::Battery
        );
        assert_eq!(
            stable
                .sample(PowerSource::Ac, now + Duration::from_millis(400))
                .0
                .source,
            PowerSource::Battery
        );
        assert_eq!(
            stable
                .sample(PowerSource::Ac, now + Duration::from_millis(899))
                .0
                .source,
            PowerSource::Battery
        );
        assert_eq!(
            stable
                .sample(PowerSource::Ac, now + Duration::from_millis(900))
                .0
                .source,
            PowerSource::Ac
        );
    }
}
