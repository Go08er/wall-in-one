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
    let niri = root.join("niri");
    executable(&noctalia, "exit 0");
    executable(
        &niri,
        "printf '%s\\n' '{\"eDP-1\":{\"current_mode\":0,\"modes\":[{\"refresh_rate\":60000}]}}'",
    );
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
niri_program = {niri:?}
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
            niri = niri.display(),
        ),
    )
    .unwrap();
    config
}

fn spawn(config: &Path, socket: &Path) -> Child {
    Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
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
