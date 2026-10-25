use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn directory(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "wall-in-one-socket-{label}-{}-{nonce}",
        std::process::id()
    ));
    fs::create_dir_all(&path).unwrap();
    path
}

fn executable(path: &Path, body: &str) {
    fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

fn fixture(root: &Path) -> PathBuf {
    let noctalia = root.join("noctalia");
    executable(&noctalia, "exit 0");
    let config = root.join("runtime.toml");
    fs::write(
        &config,
        format!(
            r#"schema_version = 4
config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
default_playlist = "only"
[settings]
cycle_interval_seconds = 300
cycle_enabled = false
shuffle = false
dynamics_enabled = false
display_mode = "mirrored"
theme_source_connector = ""
[renderer]
noctalia_program = {noctalia:?}
mpvpaper_program = "/bin/true"
linux_wallpaperengine_program = "/bin/true"
own_scene_renderer = false
layer = "background"
video_when_hidden = "pause"
video_hardware_decode = true
video_interpolation = "off"
video_muted = true
video_volume = 0
scene_fps = 30
scene_muted = true
scene_volume = 0
scene_pause_when_covered = true
scene_scaling = ""
scene_clamp = ""
[[playlists]]
id = "only"
name = "Only"
[[playlists.entries]]
id = "still"
kind = "still"
still = "/tmp/still.png"
palette = {{ kind = "keep", mode = "keep" }}
"#,
            noctalia = noctalia.display(),
        ),
    )
    .unwrap();
    config
}

/// A spawned service that is killed and reaped when it goes out of scope, so a
/// failing assertion never leaves it running (holding cargo's output pipe).
struct ServiceChild(Child);

impl Drop for ServiceChild {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

impl std::ops::Deref for ServiceChild {
    type Target = Child;

    fn deref(&self) -> &Child {
        &self.0
    }
}

impl std::ops::DerefMut for ServiceChild {
    fn deref_mut(&mut self) -> &mut Child {
        &mut self.0
    }
}

fn spawn(config: &Path, socket: &Path) -> ServiceChild {
    ServiceChild(
        Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
            // Never a desktop's compositor.
            .env_remove("WAYLAND_DISPLAY")
            .env_remove("WAYLAND_SOCKET")
            .arg("--config")
            .arg(config)
            .arg("--socket")
            .arg(socket)
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    )
}

fn wait_for_socket(socket: &Path) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if UnixStream::connect(socket).is_ok() {
            return;
        }
        thread::sleep(Duration::from_millis(20));
    }
    panic!("service socket did not become ready: {}", socket.display());
}

fn quit(socket: &Path) {
    let mut stream = UnixStream::connect(socket).unwrap();
    writeln!(stream, r#"{{"verb":"quit"}}"#).unwrap();
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line).unwrap();
    assert!(line.contains(r#""ok":true"#), "{line}");
}

fn wait(child: &mut Child) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if child.try_wait().unwrap().is_some() {
            return;
        }
        thread::sleep(Duration::from_millis(20));
    }
    let _ = child.kill();
    panic!("service did not exit");
}

#[test]
fn a_regular_file_at_the_socket_path_is_never_replaced() {
    let root = directory("regular-collision");
    let config = fixture(&root);
    let socket = root.join("runtime.sock");
    fs::write(&socket, b"sentinel").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .env_remove("WAYLAND_DISPLAY")
        .env_remove("WAYLAND_SOCKET")
        .arg("--config")
        .arg(&config)
        .arg("--socket")
        .arg(&socket)
        .output()
        .unwrap();
    assert_eq!(
        output.status.code(),
        Some(1),
        "post-config socket failures must remain restartable"
    );
    assert_eq!(fs::read(&socket).unwrap(), b"sentinel");
    assert!(String::from_utf8_lossy(&output.stderr).contains("refusing to replace non-socket"));
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_dropped_unix_socket_is_reclaimed() {
    let root = directory("stale-socket");
    let config = fixture(&root);
    let socket = root.join("runtime.sock");
    drop(UnixListener::bind(&socket).unwrap());

    let mut child = spawn(&config, &socket);
    wait_for_socket(&socket);
    quit(&socket);
    wait(&mut child);
    assert!(!socket.exists());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn shutdown_does_not_delete_a_replacement_path() {
    let root = directory("replacement");
    let config = fixture(&root);
    let socket = root.join("runtime.sock");
    let mut child = spawn(&config, &socket);
    wait_for_socket(&socket);

    fs::remove_file(&socket).unwrap();
    fs::write(&socket, b"replacement").unwrap();
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    }
    wait(&mut child);
    assert_eq!(fs::read(&socket).unwrap(), b"replacement");
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_wedged_listener_is_an_owner_and_never_hangs_startup() {
    // A listener that never accepts, with its backlog full: a blocking
    // connect to it waits forever. Startup must say the socket is owned and
    // stop, not hang before its runtime loop.
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::os::unix::ffi::OsStrExt;
    let root = directory("wedged-owner");
    let config = fixture(&root);
    let socket = root.join("runtime.sock");
    let listener =
        unsafe { OwnedFd::from_raw_fd(libc::socket(libc::AF_UNIX, libc::SOCK_STREAM, 0)) };
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (slot, byte) in address
        .sun_path
        .iter_mut()
        .zip(socket.as_os_str().as_bytes())
    {
        *slot = *byte as libc::c_char;
    }
    let length = std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t;
    assert_eq!(
        unsafe { libc::bind(listener.as_raw_fd(), (&raw const address).cast(), length) },
        0,
        "{}",
        std::io::Error::last_os_error()
    );
    assert_eq!(unsafe { libc::listen(listener.as_raw_fd(), 0) }, 0);
    // The backlog's one place, taken and never accepted.
    let _parked = UnixStream::connect(&socket).unwrap();

    let mut child = spawn(&config, &socket);
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("startup hung on a listener that never accepts");
        }
        thread::sleep(Duration::from_millis(20));
    };
    let mut stderr = String::new();
    std::io::Read::read_to_string(child.stderr.as_mut().unwrap(), &mut stderr).unwrap();
    assert_eq!(status.code(), Some(1), "{stderr}");
    assert!(stderr.contains("another service is listening"), "{stderr}");
    assert!(socket.exists(), "an owned socket is never removed");
    drop(listener);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_failing_assertion_never_leaves_the_service_running() {
    // r13b N-h: these tests' services were bare `Child`ren, which a panic drops without a
    // kill. The service then outlives the test, holding cargo's output pipe.
    let root = directory("guarded");
    let config = fixture(&root);
    let socket = root.join("runtime.sock");
    let mut process = None;
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let child = spawn(&config, &socket);
        process = Some(ProcessHandle::of(&child));
        wait_for_socket(&socket);
        panic!("an assertion fails while the service runs");
    }));
    // The closure's own panic, not one on the way to it (a spawn or a socket that never
    // came), which would say nothing about the guard.
    let payload = outcome.expect_err("the closure panics");
    assert_eq!(
        payload.downcast_ref::<&str>(),
        Some(&"an assertion fails while the service runs"),
        "{:?}",
        payload.downcast_ref::<String>()
    );
    let process = process
        .expect("the service was started")
        .unwrap_or_else(|error| panic!("pidfd_open: {error}"));
    // The guard killed and reaped it. Whatever happened, end and collect this one process:
    // through its handle, never its pid, which once reaped may be another's (review r13c N-4).
    let alive = process.is_running();
    let reaped = process.is_reaped();
    if !reaped {
        if alive {
            process.kill();
        }
        process.reap();
    }
    fs::remove_dir_all(root).unwrap();
    assert!(!alive, "the service outlived the failed test");
    assert!(reaped, "the guard left the service unreaped");
}

#[test]
fn a_process_handle_tells_running_exited_and_reaped_apart() {
    // Review r13e N10: what the guard test above reads. A process still running is neither
    // exited nor reaped; once it exits it is exited but still ours to collect; once collected,
    // reaped. Every signal goes through the handle, never a pid.
    let child = Command::new("sleep")
        .arg("30")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let process = ProcessHandle::of(&child).unwrap();
    let running = (process.is_running(), process.is_reaped());
    process.kill();
    let deadline = Instant::now() + Duration::from_secs(10);
    while process.is_running() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    let exited = (process.is_running(), process.is_reaped());
    process.reap();
    let reaped = (process.is_running(), process.is_reaped());
    drop(child);
    assert_eq!(running, (true, false), "running");
    assert_eq!(exited, (false, false), "exited, not yet collected");
    assert_eq!(reaped, (false, true), "collected");
}

/// A handle on one process (a pidfd), taken while it is a child of ours and unreaped. Unlike
/// its pid, it never names another process once that one is reaped.
struct ProcessHandle(OwnedFd);

impl ProcessHandle {
    fn of(child: &Child) -> std::io::Result<Self> {
        let pid = libc::pid_t::try_from(child.id()).unwrap();
        let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(Self(unsafe {
            OwnedFd::from_raw_fd(RawFd::try_from(fd).unwrap())
        }))
    }

    /// Whether the process still runs: its pidfd turns readable once it has exited. A poll
    /// that fails says nothing either way, so it is never read as "not running".
    fn is_running(&self) -> bool {
        loop {
            let mut poll = libc::pollfd {
                fd: self.0.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            match unsafe { libc::poll(&mut poll, 1, 0) } {
                0 => return true,
                -1 if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted => {
                }
                -1 => panic!("poll on a pidfd: {}", std::io::Error::last_os_error()),
                _ => return false,
            }
        }
    }

    /// `waitid` on this one process with `flags`, again for as long as a signal interrupts
    /// it (review r13e N10: an interrupted wait says nothing about the process).
    fn wait(&self, flags: libc::c_int) -> std::io::Result<()> {
        loop {
            let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
            let waited = unsafe {
                libc::waitid(
                    libc::P_PIDFD,
                    libc::id_t::try_from(self.0.as_raw_fd()).unwrap(),
                    &mut info,
                    flags,
                )
            };
            if waited == 0 {
                return Ok(());
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }

    /// Wait for it to exit and collect it. Only for a child of ours not yet reaped.
    fn reap(&self) {
        if let Err(error) = self.wait(libc::WEXITED) {
            panic!("waitid: {error}");
        }
    }

    /// Whether whoever started it has collected it: a pidfd of a process
    /// already reaped has nothing left to wait for (ECHILD). One still there,
    /// running or exited, is waited for without being collected. Any other
    /// failure says neither, so it is never read as either.
    fn is_reaped(&self) -> bool {
        match self.wait(libc::WEXITED | libc::WNOHANG | libc::WNOWAIT) {
            Ok(()) => false,
            Err(error) if error.raw_os_error() == Some(libc::ECHILD) => true,
            Err(error) => panic!("waitid on a pidfd: {error}"),
        }
    }

    /// SIGKILL to this one process, if it is still there.
    fn kill(&self) {
        unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                self.0.as_raw_fd(),
                libc::SIGKILL,
                std::ptr::null::<libc::siginfo_t>(),
                0,
            );
        }
    }
}
