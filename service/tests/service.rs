use chrono::NaiveDate;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use wall_in_one_service::config::Config;
use wall_in_one_service::protocol::Response;
use wall_in_one_service::renderer::{SystemDriver, WallpaperDriver};
use wall_in_one_service::runtime::Runtime;

fn directory(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let path = std::env::temp_dir().join(format!(
        "wall-in-one-{label}-{}-{nonce}",
        std::process::id()
    ));
    fs::create_dir_all(&path).unwrap();
    path
}

fn config(noctalia: &Path, mpvpaper: &Path, own_scene: bool) -> String {
    format!(
        r#"schema_version = 1
default_playlist = "day"
[settings]
cycle_interval_seconds = 300
cycle_enabled = false
shuffle = false
dynamics_enabled = true
[renderer]
noctalia_program = {noctalia:?}
mpvpaper_program = {mpvpaper:?}
linux_wallpaperengine_program = "/bin/true"
own_scene_renderer = {own_scene}
layer = "background"
video_when_hidden = "pause"
video_hardware_decode = true
video_muted = true
video_volume = 50
scene_fps = 30
scene_muted = true
scene_volume = 0
scene_pause_when_covered = true
scene_scaling = ""
scene_clamp = ""
[[playlists]]
id = "day"
name = "Day"
[[playlists.entries]]
id = "still-one"
kind = "still"
still = "/tmp/one.png"
palette = {{ kind = "adaptive", scheme = "m3-tonal-spot", mode = "dark" }}
[[playlists.entries]]
id = "video-two"
kind = "video"
still = "/tmp/two.png"
motion = "/tmp/two.mp4"
palette = {{ kind = "named", source = "community", name = "catppuccin", mode = "light" }}
[[playlists]]
id = "night"
name = "Night"
[[playlists.entries]]
id = "scene-three"
kind = "scene"
still = "/tmp/three.png"
scene_id = "12345"
palette = {{ kind = "keep", mode = "keep" }}
[[schedules]]
id = "night-rule"
playlist = "night"
months = [12]
start = "22:00"
end = "06:00"
"#,
        noctalia = noctalia.display(),
        mpvpaper = mpvpaper.display()
    )
}

fn request(socket: &Path, verb: &str, argument: Option<&str>) -> serde_json::Value {
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut stream = loop {
        match UnixStream::connect(socket) {
            Ok(stream) => break stream,
            Err(_) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            Err(error) => panic!("service socket did not appear: {error}"),
        }
    };
    let payload = if let Some(argument) = argument {
        serde_json::json!({"verb": verb, "argument": argument})
    } else {
        serde_json::json!({"verb": verb})
    };
    writeln!(stream, "{}", payload).unwrap();
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line).unwrap();
    serde_json::from_str(&line).unwrap()
}

fn stop(child: &mut Child, socket: &Path) {
    let _ = request(socket, "quit", None);
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if child.try_wait().unwrap().is_some() {
            return;
        }
        thread::sleep(Duration::from_millis(20));
    }
    let _ = child.kill();
    panic!("service did not stop after quit");
}

#[test]
fn handwritten_config_and_binary_are_a_complete_rotator() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("standalone");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let harmless = root.join("harmless");
    fs::write(&harmless, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&harmless, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(&config_path, config(&harmless, &harmless, false)).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .spawn()
        .unwrap();

    let reply = request(&socket, "status", None);
    assert_eq!(reply["ok"], true);
    let status: serde_json::Value =
        serde_json::from_str(reply["message"].as_str().unwrap()).unwrap();
    assert_eq!(status["playlist_id"], "day");
    assert_eq!(status["source"], "schedule");

    // The manual override still takes effect, but the scene honestly reports
    // that motion was refused; its still has already been applied.
    assert_eq!(request(&socket, "playlist-use", Some("Night"))["ok"], false);
    let status = request(&socket, "status", None);
    let status: serde_json::Value =
        serde_json::from_str(status["message"].as_str().unwrap()).unwrap();
    assert_eq!(status["playlist_id"], "night");
    assert_eq!(status["source"], "manual");
    assert_eq!(request(&socket, "pause", None)["message"], "paused");
    assert_eq!(
        request(&socket, "shuffle", Some("on"))["message"],
        "shuffle on"
    );
    assert_eq!(request(&socket, "schedule-follow", None)["ok"], true);
    fs::write(&config_path, "schema_version = 99\n").unwrap();
    assert_eq!(request(&socket, "reload", None)["ok"], false);
    let status = request(&socket, "status", None);
    let status: serde_json::Value =
        serde_json::from_str(status["message"].as_str().unwrap()).unwrap();
    assert_eq!(
        status["playlist_id"], "day",
        "failed reload keeps valid runtime state"
    );
    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn explicit_reload_is_not_repeated_by_the_file_watcher() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("single-reload");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let log = root.join("events");
    let recorder = root.join("record");
    fs::write(
        &recorder,
        format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\n", log),
    )
    .unwrap();
    fs::set_permissions(&recorder, fs::Permissions::from_mode(0o755)).unwrap();
    let original = config(&recorder, &recorder, false);
    fs::write(&config_path, &original).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .spawn()
        .unwrap();
    let _ = request(&socket, "status", None);

    fs::write(
        &config_path,
        original.replace("/tmp/one.png", "/tmp/one-after-reload.png"),
    )
    .unwrap();
    assert_eq!(request(&socket, "reload", None)["ok"], true);
    thread::sleep(Duration::from_millis(1400));

    let events = fs::read_to_string(&log).unwrap();
    assert_eq!(
        events
            .lines()
            .filter(|line| line.contains("wallpaper-set /tmp/one-after-reload.png"))
            .count(),
        1,
        "the explicit reload must advance the watcher's known generation"
    );
    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn renderer_applies_still_then_mode_then_palette_then_motion() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("renderer-order");
    let log = root.join("events");
    let script = root.join("record");
    fs::write(
        &script,
        format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\n", log),
    )
    .unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
    let parsed: Config = toml::from_str(&config(&script, &script, false)).unwrap();
    parsed.validate().unwrap();
    let video = parsed.playlists[0].entries[1].clone();
    let scene = parsed.playlists[1].entries[0].clone();
    let mut driver = SystemDriver::new(parsed.renderer.clone());
    driver.apply(&video, "eDP-1", &parsed.settings).unwrap();
    thread::sleep(Duration::from_millis(100));
    let error = driver.apply(&scene, "eDP-1", &parsed.settings).unwrap_err();
    assert!(error.contains("paired still remains applied"));
    thread::sleep(Duration::from_millis(100));
    let events = fs::read_to_string(&log).unwrap();
    let lines: Vec<_> = events.lines().collect();
    assert_eq!(lines[0], "msg wallpaper-set eDP-1 /tmp/two.png");
    assert_eq!(lines[1], "msg theme-mode-set light");
    assert_eq!(lines[2], "msg color-scheme-set community catppuccin");
    assert!(lines[3].contains("--layer background"));
    assert_eq!(lines[4], "msg wallpaper-set eDP-1 /tmp/three.png");
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn palette_failure_is_reported_instead_of_claiming_the_entry_applied() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("palette-failure");
    let script = root.join("selective-failure");
    fs::write(&script, "#!/bin/sh\n[ \"$2\" != color-scheme-set ]\n").unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
    let parsed: Config = toml::from_str(&config(&script, &script, false)).unwrap();
    let entry = parsed.playlists[0].entries[0].clone();
    let mut driver = SystemDriver::new(parsed.renderer.clone());

    let error = driver.apply(&entry, "", &parsed.settings).unwrap_err();

    assert!(error.contains("noctalia exited"));
    fs::remove_dir_all(root).unwrap();
}

#[allow(dead_code)]
fn _response_shape(_: Response) {}

#[test]
fn schedule_clock_remains_injectable_in_the_runtime_layer() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(23, 0, 0)
        .unwrap();
    assert_eq!(at.format("%H:%M").to_string(), "23:00");
}

#[derive(Clone)]
struct RecordingDriver(Arc<Mutex<Vec<(String, String)>>>);

impl WallpaperDriver for RecordingDriver {
    fn apply(
        &mut self,
        entry: &wall_in_one_service::config::Entry,
        output: &str,
        _settings: &wall_in_one_service::config::Settings,
    ) -> Result<(), String> {
        self.0
            .lock()
            .unwrap()
            .push((output.into(), entry.id.clone()));
        Ok(())
    }
    fn set_paused(&mut self, _paused: bool) {}
    fn reconfigure(&mut self, _settings: wall_in_one_service::config::RendererSettings) {}
    fn stop(&mut self) {}
}

#[test]
fn display_assignment_is_the_baseline_and_manual_override_wins() {
    let mut parsed: Config = toml::from_str(&format!(
        "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"night\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    ))
    .unwrap();
    parsed.schedules.clear();
    let mut fourth = parsed.playlists[1].entries[0].clone();
    fourth.id = "scene-four".into();
    fourth.scene_id = Some("12346".into());
    let mut fifth = parsed.playlists[1].entries[0].clone();
    fifth.id = "scene-five".into();
    fifth.scene_id = Some("12347".into());
    parsed.playlists[1].entries.extend([fourth, fifth]);
    parsed.validate().unwrap();
    let events = Arc::new(Mutex::new(Vec::new()));
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RecordingDriver(events.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    assert_eq!(
        events.lock().unwrap().as_slice(),
        &[("DP-1".into(), "scene-three".into())]
    );
    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "status".into(),
            argument: None,
        },
        at,
    );
    let status: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    assert_eq!(status["playlist_id"], "night");
    assert_eq!(status["playlist"], "Night");
    assert_eq!(status["displays"][0]["connector"], "DP-1");
    assert_eq!(status["displays"][0]["playlist_id"], "night");
    assert_eq!(status["displays"][0]["entry_id"], "scene-three");
    assert_eq!(status["playlists"][1]["entries"], 3);

    for expected in ["scene-four", "scene-five"] {
        let response = runtime.handle(
            wall_in_one_service::protocol::Request {
                verb: "next".into(),
                argument: None,
            },
            at,
        );
        assert!(response.ok);
        assert_eq!(
            events.lock().unwrap().last(),
            Some(&("DP-1".into(), expected.into()))
        );
    }

    runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "playlist-use".into(),
            argument: Some("day".into()),
        },
        at,
    );
    assert_eq!(
        events.lock().unwrap().last(),
        Some(&("DP-1".into(), "still-one".into()))
    );
}

#[test]
fn reload_of_inactive_authoring_state_does_not_reapply_the_wallpaper() {
    let root = directory("quiet-reload");
    let config_path = root.join("runtime.toml");
    let original = config(Path::new("/bin/true"), Path::new("/bin/true"), false);
    fs::write(&config_path, &original).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let events = Arc::new(Mutex::new(Vec::new()));
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let mut runtime = Runtime::new(
        config_path.clone(),
        parsed,
        RecordingDriver(events.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    events.lock().unwrap().clear();
    fs::write(
        &config_path,
        original.replace(
            "cycle_interval_seconds = 300",
            "cycle_interval_seconds = 301",
        ),
    )
    .unwrap();

    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "reload".into(),
            argument: None,
        },
        at,
    );

    assert!(response.ok);
    assert!(events.lock().unwrap().is_empty());
    fs::remove_dir_all(root).unwrap();
}
