use chrono::Local;
use std::env;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::BufReader;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime};
use wall_in_one_service::config::{Config, ConfigError};
use wall_in_one_service::power::PowerObserver;
use wall_in_one_service::protocol::{Request, Response, read_request_until, write_response};
use wall_in_one_service::renderer::SystemDriver;
use wall_in_one_service::runtime::Runtime;

static TERMINATE: AtomicBool = AtomicBool::new(false);
const STARTUP_RETRY_WINDOW: Duration = Duration::from_secs(8);
const STARTUP_RETRY_INTERVAL: Duration = Duration::from_millis(250);
// sysexits.h's EX_CONFIG.  The packaged unit names this in
// RestartPreventExitStatus so a present incompatible or malformed runtime
// document stops once instead of consuming its restart burst.  Failures after
// configuration has loaded remain ordinary status 1 failures and retain
// systemd recovery.
const EX_CONFIG: i32 = 78;

enum ServiceError {
    Config(ConfigError),
    Runtime(String),
}

impl ServiceError {
    fn exit_status(&self) -> i32 {
        match self {
            Self::Config(_) => EX_CONFIG,
            Self::Runtime(_) => 1,
        }
    }
}

impl fmt::Display for ServiceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Config(error) => error.fmt(formatter),
            Self::Runtime(error) => formatter.write_str(error),
        }
    }
}

impl From<String> for ServiceError {
    fn from(error: String) -> Self {
        Self::Runtime(error)
    }
}

struct SocketOwner {
    listener: UnixListener,
    path: PathBuf,
    identity: (u64, u64),
    _lock: File,
}

impl Drop for SocketOwner {
    fn drop(&mut self) {
        remove_owned_socket(&self.path, self.identity);
    }
}

fn remove_owned_socket(path: &Path, identity: (u64, u64)) {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return;
    };
    if metadata.file_type().is_socket() && (metadata.dev(), metadata.ino()) == identity {
        let _ = fs::remove_file(path);
    }
}

struct StartupRetry {
    deadline: Instant,
    next_attempt: Instant,
    first_error: String,
    authoritative_generation: u64,
}

extern "C" fn terminate(_: libc::c_int) {
    TERMINATE.store(true, Ordering::Relaxed);
}

struct Options {
    config: PathBuf,
    socket: PathBuf,
    wait_for_config: bool,
    check_config: bool,
}

fn xdg(variable: &str, fallback: PathBuf) -> PathBuf {
    env::var_os(variable)
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .unwrap_or(fallback)
}

fn defaults() -> Options {
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"));
    let state = xdg("XDG_STATE_HOME", home.join(".local/state"));
    let runtime = xdg("XDG_RUNTIME_DIR", state.join("wall-in-one"));
    Options {
        config: state.join("wall-in-one/runtime.toml"),
        socket: runtime.join("wall-in-one-runtime.sock"),
        wait_for_config: false,
        check_config: false,
    }
}

fn parse() -> Result<Options, String> {
    let mut options = defaults();
    let mut arguments = env::args_os().skip(1);
    while let Some(argument) = arguments.next() {
        match argument.to_str() {
            Some("--config") => {
                options.config = arguments
                    .next()
                    .map(PathBuf::from)
                    .ok_or("--config needs a path")?
            }
            Some("--socket") => {
                options.socket = arguments
                    .next()
                    .map(PathBuf::from)
                    .ok_or("--socket needs a path")?
            }
            Some("--wait-for-config") => options.wait_for_config = true,
            Some("--check-config") => options.check_config = true,
            Some("--version") => {
                println!("wall-in-one-service {}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            Some("--help") => {
                println!(
                    "usage: wall-in-one-service [--config PATH] [--socket PATH] \
                     [--wait-for-config] [--check-config]"
                );
                std::process::exit(0);
            }
            _ => return Err(format!("unknown argument {}", argument.to_string_lossy())),
        }
    }
    if !options.config.is_absolute() || !options.socket.is_absolute() {
        return Err("config and socket paths must be absolute".into());
    }
    Ok(options)
}

fn serve(
    stream: UnixStream,
    runtime: &mut Runtime<SystemDriver>,
    power: &mut PowerObserver,
) -> bool {
    let _ = stream.set_write_timeout(Some(Duration::from_millis(250)));
    let mut reader = BufReader::new(&stream);
    let deadline = Instant::now() + Duration::from_millis(250);
    let (response, was_reload) = match read_request_until(&mut reader, &stream, deadline) {
        Ok(request) => {
            let was_reload = request.verb == "reload";
            (
                runtime.handle_with_power(request, Local::now().naive_local(), |enabled| {
                    power.prepare(enabled)
                }),
                was_reload,
            )
        }
        Err(error) => (Response::failure(error), false),
    };
    let mut writer = stream;
    if let Err(error) = write_response(&mut writer, &response) {
        eprintln!("wall-in-one-service: response write failed: {error}");
    }
    was_reload
}

// One silent local client can consume the complete 250 ms request deadline.
// Always return to config polling, renderer supervision and signal checks
// after a bounded batch instead of letting a continuously-ready listener
// monopolise the single service thread.
const MAX_ACCEPTS_PER_ITERATION: usize = 4;

fn accept_ready_batch(
    listener: &UnixListener,
    mut accepted: impl FnMut(UnixStream),
) -> std::io::Result<usize> {
    let mut count = 0;
    while count < MAX_ACCEPTS_PER_ITERATION {
        match listener.accept() {
            Ok((stream, _)) => {
                accepted(stream);
                count += 1;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(error) => return Err(error),
        }
    }
    Ok(count)
}

fn fingerprint(path: &Path) -> Option<(u64, u64, SystemTime)> {
    let metadata = fs::metadata(path).ok()?;
    Some((metadata.ino(), metadata.len(), metadata.modified().ok()?))
}

fn install_signal_handlers() {
    unsafe {
        libc::signal(libc::SIGTERM, terminate as *const () as usize);
        libc::signal(libc::SIGINT, terminate as *const () as usize);
    }
}

fn wait_for_config(path: &Path) -> Result<bool, String> {
    while !TERMINATE.load(Ordering::Relaxed) {
        match fs::metadata(path) {
            Ok(_) => return Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                thread::sleep(Duration::from_millis(250));
            }
            Err(error) => {
                return Err(format!(
                    "cannot inspect config path {}: {error}",
                    path.display()
                ));
            }
        }
    }
    Ok(false)
}

fn lock_path(socket: &Path) -> PathBuf {
    let mut name = socket.as_os_str().to_os_string();
    name.push(".lock");
    PathBuf::from(name)
}

fn claim_lock_with_hook(lock_path: &Path, after_open: impl FnOnce()) -> Result<File, String> {
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(lock_path)
        .map_err(|error| {
            format!(
                "cannot open singleton lock {}: {error}",
                lock_path.display()
            )
        })?;
    let opened = lock
        .metadata()
        .map_err(|error| format!("cannot inspect singleton lock: {error}"))?;
    if !opened.is_file() || opened.nlink() != 1 || opened.uid() != unsafe { libc::geteuid() } {
        return Err(format!(
            "refusing singleton lock {} unless it is one regular, privately owned link",
            lock_path.display()
        ));
    }
    // Operate on the descriptor, never the pathname: replacing the name after
    // open must not let this process chmod a symlink target or somebody else's
    // new lock inode.
    lock.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("cannot secure singleton lock: {error}"))?;
    let locked = unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if locked != 0 {
        let error = std::io::Error::last_os_error();
        if error.kind() == std::io::ErrorKind::WouldBlock {
            return Err(format!(
                "another service owns singleton lock {}",
                lock_path.display()
            ));
        }
        return Err(format!(
            "cannot lock singleton guard {}: {error}",
            lock_path.display()
        ));
    }
    after_open();
    let current = fs::symlink_metadata(lock_path).map_err(|error| {
        format!(
            "cannot recheck singleton lock {}: {error}",
            lock_path.display()
        )
    })?;
    if !current.is_file()
        || current.nlink() != 1
        || current.uid() != unsafe { libc::geteuid() }
        || (current.dev(), current.ino()) != (opened.dev(), opened.ino())
    {
        return Err(format!(
            "singleton lock {} changed while it was being claimed",
            lock_path.display()
        ));
    }
    Ok(lock)
}

fn clear_stale_socket_with_hook(path: &Path, before_recheck: impl FnOnce()) -> Result<(), String> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("cannot inspect socket {}: {error}", path.display())),
    };
    if UnixStream::connect(path).is_ok() {
        return Err(format!(
            "another service is listening on {}",
            path.display()
        ));
    }
    if !metadata.file_type().is_socket() {
        return Err(format!(
            "refusing to replace non-socket path {}",
            path.display()
        ));
    }

    before_recheck();
    let current = match fs::symlink_metadata(path) {
        Ok(current) => current,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => {
            return Err(format!(
                "cannot recheck stale socket {}: {error}",
                path.display()
            ));
        }
    };
    if !current.file_type().is_socket()
        || (current.dev(), current.ino()) != (metadata.dev(), metadata.ino())
    {
        return Err(format!(
            "socket path {} changed while its stale owner was being checked",
            path.display()
        ));
    }
    fs::remove_file(path).map_err(|error| format!("cannot remove stale socket: {error}"))
}

fn claim_socket(path: &Path) -> Result<SocketOwner, String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("socket {} has no parent directory", path.display()))?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("cannot create socket directory: {error}"))?;
    let parent_metadata = fs::metadata(parent).map_err(|error| {
        format!(
            "cannot inspect socket directory {}: {error}",
            parent.display()
        )
    })?;
    if !parent_metadata.is_dir()
        || parent_metadata.uid() != unsafe { libc::geteuid() }
        || parent_metadata.mode() & 0o022 != 0
    {
        return Err(format!(
            "socket directory {} must be owned by this user and not group/world writable",
            parent.display()
        ));
    }
    let lock_path = lock_path(path);
    let lock = claim_lock_with_hook(&lock_path, || {})?;

    clear_stale_socket_with_hook(path, || {})?;
    let listener = UnixListener::bind(path)
        .map_err(|error| format!("cannot bind {}: {error}", path.display()))?;
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect newly bound socket: {error}"))?;
    let identity = (metadata.dev(), metadata.ino());
    if let Err(error) = fs::set_permissions(path, fs::Permissions::from_mode(0o600)) {
        remove_owned_socket(path, identity);
        return Err(format!("cannot secure socket: {error}"));
    }
    if let Err(error) = listener.set_nonblocking(true) {
        remove_owned_socket(path, identity);
        return Err(error.to_string());
    }
    Ok(SocketOwner {
        listener,
        path: path.to_path_buf(),
        identity,
        _lock: lock,
    })
}

fn startup_readiness_error(error: &str) -> bool {
    [
        "cannot run noctalia",
        "noctalia exited",
        "noctalia timed out",
        "cannot run niri outputs",
        "niri outputs exited",
        "niri outputs timed out",
        "niri outputs returned invalid JSON",
        "niri reported no usable outputs",
    ]
    .iter()
    .any(|marker| error.contains(marker))
}

fn initial_apply(runtime: &mut Runtime<SystemDriver>, now: Instant) -> Option<StartupRetry> {
    match runtime.apply_current() {
        Ok(_) => None,
        Err(error) if startup_readiness_error(&error) => {
            eprintln!("wall-in-one-service: initial apply waiting for desktop readiness: {error}");
            Some(StartupRetry {
                deadline: now + STARTUP_RETRY_WINDOW,
                next_attempt: now + STARTUP_RETRY_INTERVAL,
                first_error: error,
                authoritative_generation: runtime.authoritative_generation(),
            })
        }
        Err(error) => {
            if runtime.schedule_initial_apply_retry(now) {
                eprintln!(
                    "wall-in-one-service: initial apply failed; queued automatic quarantine retries: {error}"
                );
            } else {
                eprintln!("wall-in-one-service: initial apply: {error}");
            }
            None
        }
    }
}

fn release_startup_allocator_slack() {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    {
        // Config::load has already released parser slack. Route construction
        // and the first staged apply can leave their own short-lived buffers;
        // glibc otherwise keeps those free arenas resident indefinitely.
        // This startup cleanup is one-shot: trimming on status/tick would trade a
        // small RSS win for ongoing allocator and CPU overhead.
        // SAFETY: malloc_trim accepts any `pad` value and only asks glibc to
        // release completely free allocator pages owned by this process.
        unsafe {
            libc::malloc_trim(0);
        }
    }
}

fn retry_initial_apply(
    retry: &mut Option<StartupRetry>,
    runtime: &mut Runtime<SystemDriver>,
    now: Instant,
) -> Result<(), String> {
    let Some(pending) = retry.as_mut() else {
        return Ok(());
    };
    if runtime.authoritative_generation() != pending.authoritative_generation {
        *retry = None;
        return Ok(());
    }
    if now < pending.next_attempt {
        return Ok(());
    }
    if now >= pending.deadline {
        return Err(format!(
            "initial apply readiness deadline expired after {} seconds; first error: {}",
            STARTUP_RETRY_WINDOW.as_secs(),
            pending.first_error
        ));
    }
    match runtime.apply_current() {
        Ok(_) => {
            eprintln!("wall-in-one-service: desktop became ready; initial wallpaper applied");
            *retry = None;
        }
        Err(error) if startup_readiness_error(&error) && now < pending.deadline => {
            pending.next_attempt = now + STARTUP_RETRY_INTERVAL;
        }
        Err(error) => {
            if now >= pending.deadline {
                return Err(format!(
                    "initial apply readiness deadline expired after {} seconds; first error: {}; last error: {error}",
                    STARTUP_RETRY_WINDOW.as_secs(),
                    pending.first_error
                ));
            } else {
                eprintln!("wall-in-one-service: initial apply stopped retrying: {error}");
            }
            *retry = None;
        }
    }
    Ok(())
}

fn run() -> Result<(), ServiceError> {
    let options = parse()?;
    install_signal_handlers();
    if options.wait_for_config && !wait_for_config(&options.config)? {
        return Ok(());
    }
    let config = Config::load(&options.config).map_err(ServiceError::Config)?;
    if options.check_config {
        return Ok(());
    }
    let driver = SystemDriver::new_for_service_socket(config.renderer.clone(), &options.socket);
    let mut runtime = Runtime::new(
        options.config.clone(),
        config,
        driver,
        Local::now().naive_local(),
    )?;
    // Own and secure the public endpoint before applying anything. A losing
    // process must have exactly zero wallpaper or renderer side effects.
    let socket = claim_socket(&options.socket)?;
    let listener = &socket.listener;
    let mut power = PowerObserver::default();
    runtime.observe_power(power.synchronize(runtime.battery_policy_enabled()));
    let mut startup_retry = initial_apply(&mut runtime, Instant::now());
    release_startup_allocator_slack();
    let mut known = fingerprint(&options.config);
    let mut next_config_check = Instant::now() + Duration::from_secs(1);
    while !runtime.should_quit() && !TERMINATE.load(Ordering::Relaxed) {
        if let Err(error) = accept_ready_batch(listener, |stream| {
            // Capture before loading. If an atomic rename races this request,
            // retaining the older fingerprint can cause one harmless extra
            // reload; capturing afterward could mark unseen newer bytes as
            // loaded and miss them entirely.
            let observed = fingerprint(&options.config);
            if serve(stream, &mut runtime, &mut power) {
                known = observed;
            }
        }) {
            eprintln!("wall-in-one-service: accept: {error}");
        }
        if runtime.should_quit() || TERMINATE.load(Ordering::Relaxed) {
            break;
        }
        let now = Instant::now();
        if now >= next_config_check {
            let current = fingerprint(&options.config);
            if current.is_some() && current != known {
                let response = runtime.handle_with_power(
                    Request {
                        verb: "reload".into(),
                        argument: None,
                    },
                    Local::now().naive_local(),
                    |enabled| power.prepare(enabled),
                );
                if !response.ok {
                    eprintln!("wall-in-one-service: reload: {}", response.message);
                }
                // A broken generation is reported once. The next atomic
                // rename changes the inode and gets another attempt; polling
                // the same bad bytes forever would only spam the journal.
                known = current;
            }
            next_config_check = now + Duration::from_secs(1);
        }
        runtime.observe_power(power.synchronize(runtime.battery_policy_enabled()));
        runtime.tick(Local::now().naive_local(), now);
        retry_initial_apply(&mut startup_retry, &mut runtime, now)?;
        thread::sleep(Duration::from_millis(25));
    }
    runtime.shutdown();
    drop(socket);
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        let exit_status = error.exit_status();
        eprintln!("wall-in-one-service: {error}");
        std::process::exit(exit_status);
    }
}

#[cfg(test)]
mod tests {
    use super::{
        MAX_ACCEPTS_PER_ITERATION, accept_ready_batch, claim_lock_with_hook,
        clear_stale_socket_with_hook,
    };
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn directory(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "wall-in-one-main-{label}-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir(&path).unwrap();
        path
    }

    #[test]
    fn a_replacement_lock_is_not_chmodded_or_accepted() {
        let root = directory("lock-replacement");
        let lock = root.join("runtime.sock.lock");
        let opened = root.join("opened.lock");
        let result = claim_lock_with_hook(&lock, || {
            fs::rename(&lock, &opened).unwrap();
            fs::write(&lock, b"replacement").unwrap();
            fs::set_permissions(&lock, fs::Permissions::from_mode(0o644)).unwrap();
        });
        assert!(
            result
                .unwrap_err()
                .contains("changed while it was being claimed")
        );
        assert_eq!(fs::read(&lock).unwrap(), b"replacement");
        assert_eq!(
            fs::metadata(&lock).unwrap().permissions().mode() & 0o777,
            0o644
        );
        assert_eq!(
            fs::metadata(&opened).unwrap().permissions().mode() & 0o777,
            0o600
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn stale_cleanup_never_unlinks_a_replacement_path() {
        let root = directory("stale-replacement");
        let socket = root.join("runtime.sock");
        drop(UnixListener::bind(&socket).unwrap());
        let result = clear_stale_socket_with_hook(&socket, || {
            fs::remove_file(&socket).unwrap();
            fs::write(&socket, b"replacement").unwrap();
        });
        assert!(
            result
                .unwrap_err()
                .contains("changed while its stale owner")
        );
        assert_eq!(fs::read(&socket).unwrap(), b"replacement");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_ready_client_flood_is_bounded_before_runtime_maintenance() {
        let root = directory("bounded-accepts");
        let socket = root.join("runtime.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        listener.set_nonblocking(true).unwrap();
        let clients: Vec<_> = (0..=MAX_ACCEPTS_PER_ITERATION)
            .map(|_| UnixStream::connect(&socket).unwrap())
            .collect();
        let mut accepted = Vec::new();

        assert_eq!(
            accept_ready_batch(&listener, |stream| accepted.push(stream)).unwrap(),
            MAX_ACCEPTS_PER_ITERATION
        );
        assert_eq!(accepted.len(), MAX_ACCEPTS_PER_ITERATION);
        // A connection is still immediately ready. The first call returned
        // solely because of the cap, leaving the caller free to poll config,
        // tick renderers and observe termination before the next batch.
        assert_eq!(
            accept_ready_batch(&listener, |stream| accepted.push(stream)).unwrap(),
            1
        );

        drop(clients);
        drop(accepted);
        drop(listener);
        fs::remove_dir_all(root).unwrap();
    }
}
