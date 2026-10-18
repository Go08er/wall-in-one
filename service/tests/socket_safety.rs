use std::fs;
use std::io::{BufRead, BufReader, Write};
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

fn spawn(config: &Path, socket: &Path) -> Child {
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
        .unwrap()
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
