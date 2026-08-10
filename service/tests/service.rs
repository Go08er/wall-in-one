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

/// Run `attempt` until the kernel stops calling the freshly written program busy.
///
/// These tests write a small shell script and then have the service exec it.
/// `fs::write` closes its own handle, so the file is quiet by the time we ask --
/// but a *different* test thread forking at that instant hands its child a copy
/// of the still-open write descriptor, and Linux refuses to exec a file anybody
/// holds open for writing. Some of these tests spawn the real service binary,
/// which lives for seconds, so an inherited descriptor is not a momentary
/// window: it lasts as long as that child does.
///
/// The tests are also run one at a time (`RUST_TEST_THREADS` in the flake),
/// which removes the concurrent fork and therefore the cause. This is the
/// second line of defence, kept because the failure it guards against was
/// reproducible on a user's machine and never once here, so the diagnosis
/// deserves less confidence than the fix.
fn without_text_file_busy<T>(mut attempt: impl FnMut() -> Result<T, String>) -> Result<T, String> {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match attempt() {
            Err(error) if error.contains("Text file busy") && Instant::now() < deadline => {
                thread::sleep(Duration::from_millis(20));
            }
            outcome => return outcome,
        }
    }
}

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
        r#"schema_version = 2
default_playlist = "day"
[settings]
cycle_interval_seconds = 300
cycle_enabled = false
shuffle = false
dynamics_enabled = true
[renderer]
noctalia_program = {noctalia:?}
niri_program = "/bin/true"
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
    assert_eq!(status["playlists"].as_array().unwrap().len(), 2);
    assert_eq!(status["playlists"][0]["id"], "day");
    assert_eq!(status["playlists"][0]["entries"], 2);
    assert_eq!(status["playlists"][0]["active"], true);
    assert_eq!(status["schedule"]["following"], true);
    assert_eq!(status["schedule"]["playlist_id"], "day");
    assert_eq!(status["schedule"]["rule_id"], serde_json::Value::Null);
    assert_eq!(status["schedules"][0]["id"], "night-rule");
    assert_eq!(status["schedules"][0]["selected"], false);
    assert_eq!(status["displays"][0]["assigned_playlist_id"], "day");

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
fn systemd_mode_waits_quietly_for_the_first_config() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Stdio;
    let root = directory("wait-for-config");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let stderr_path = root.join("stderr");
    let harmless = root.join("harmless");
    fs::write(&harmless, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&harmless, fs::Permissions::from_mode(0o755)).unwrap();
    let stderr = fs::File::create(&stderr_path).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .arg("--wait-for-config")
        .stderr(Stdio::from(stderr))
        .spawn()
        .unwrap();

    thread::sleep(Duration::from_millis(500));
    assert!(child.try_wait().unwrap().is_none());
    assert!(!socket.exists());
    assert_eq!(fs::read_to_string(&stderr_path).unwrap(), "");

    fs::write(&config_path, config(&harmless, &harmless, false)).unwrap();
    let reply = request(&socket, "status", None);
    assert_eq!(reply["ok"], true);
    stop(&mut child, &socket);
    assert_eq!(fs::read_to_string(&stderr_path).unwrap(), "");
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
    without_text_file_busy(|| driver.apply(&video, "eDP-1", &parsed.settings)).unwrap();
    thread::sleep(Duration::from_millis(100));
    let error =
        without_text_file_busy(|| driver.apply(&scene, "eDP-1", &parsed.settings)).unwrap_err();
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
fn all_output_scene_uses_background_targets_for_every_live_connector() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("scene-all-outputs");
    let events = root.join("events");
    let noctalia = root.join("noctalia");
    let niri = root.join("niri");
    let engine = root.join("linux-wallpaperengine");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::write(
        &niri,
        "#!/bin/sh\nprintf '%s\\n' '{\"eDP-1\":{\"name\":\"eDP-1\"},\"DP-1\":{\"name\":\"DP-1\"}}'\n",
    )
    .unwrap();
    fs::write(
        &engine,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nsleep 30\n",
            events
        ),
    )
    .unwrap();
    for executable in [&noctalia, &niri, &engine] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, Path::new("/bin/true"), true)
        .replace(
            "niri_program = \"/bin/true\"",
            &format!("niri_program = {niri:?}"),
        )
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!("linux_wallpaperengine_program = {engine:?}"),
        );
    let parsed: Config = toml::from_str(&document).unwrap();
    let scene = parsed.playlists[1].entries[0].clone();
    let mut driver = SystemDriver::new(parsed.renderer.clone());

    without_text_file_busy(|| driver.apply(&scene, "", &parsed.settings)).unwrap();
    thread::sleep(Duration::from_millis(100));

    let launched = fs::read_to_string(&events).unwrap();
    assert!(launched.contains("--screen-root DP-1 --bg 12345"));
    assert!(launched.contains("--screen-root eDP-1 --bg 12345"));
    assert_eq!(launched.matches("--screen-root").count(), 2);
    assert_eq!(launched.matches("--bg 12345").count(), 2);
    let words: Vec<_> = launched.split_whitespace().collect();
    for (index, word) in words.iter().enumerate() {
        if *word == "12345" {
            assert_eq!(
                words[index - 1],
                "--bg",
                "scene id used as positional preview"
            );
        }
    }
    driver.stop();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn palette_failure_is_reported_instead_of_claiming_the_entry_applied() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("palette-failure");
    let script = root.join("selective-failure");
    let mut executable = fs::File::create(&script).unwrap();
    executable
        .write_all(b"#!/bin/sh\n[ \"$2\" != color-scheme-set ]\n")
        .unwrap();
    executable.sync_all().unwrap();
    drop(executable);
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
    let parsed: Config = toml::from_str(&config(&script, Path::new("/bin/true"), false)).unwrap();
    let entry = parsed.playlists[0].entries[0].clone();
    let mut driver = SystemDriver::new(parsed.renderer.clone());

    let error = without_text_file_busy(|| driver.apply(&entry, "", &parsed.settings)).unwrap_err();

    assert!(
        error.contains("noctalia exited"),
        "unexpected error: {error}"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn crashed_scene_falls_back_once_and_is_suppressed_for_the_session() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("scene-crash");
    let events = root.join("events");
    let launches = root.join("scene-launches");
    let noctalia = root.join("noctalia");
    let niri = root.join("niri");
    let engine = root.join("linux-wallpaperengine");
    fs::write(
        &noctalia,
        format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\n", events),
    )
    .unwrap();
    fs::write(
        &engine,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nexit 42\n",
            launches
        ),
    )
    .unwrap();
    fs::write(
        &niri,
        "#!/bin/sh\nprintf '%s\\n' '{\"eDP-1\":{\"name\":\"eDP-1\"}}'\n",
    )
    .unwrap();
    for executable in [&noctalia, &niri, &engine] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, Path::new("/bin/true"), true)
        .replace(
            "niri_program = \"/bin/true\"",
            &format!("niri_program = {niri:?}"),
        )
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!("linux_wallpaperengine_program = {engine:?}"),
        );
    let parsed: Config = toml::from_str(&document).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let mut runtime = Runtime::new(
        root.join("runtime.toml"),
        parsed,
        SystemDriver::new(toml::from_str::<Config>(&document).unwrap().renderer),
        at,
    )
    .unwrap();

    let started = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "playlist-use".into(),
            argument: Some("night".into()),
        },
        at,
    );
    assert!(started.ok);
    thread::sleep(Duration::from_millis(100));
    runtime.tick(at, Instant::now());

    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "status".into(),
            argument: None,
        },
        at,
    );
    let status: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    assert_eq!(status["motion_active"], false);
    assert!(status["last_error"]
        .as_str()
        .unwrap()
        .contains("scene 12345"));
    assert!(status["last_error"]
        .as_str()
        .unwrap()
        .contains("linux-wallpaperengine"));
    assert_eq!(
        fs::read_to_string(&events)
            .unwrap()
            .lines()
            .filter(|line| line.contains("wallpaper-set /tmp/three.png"))
            .count(),
        2,
        "the initial still is explicitly reaffirmed after the renderer exits"
    );

    let refused = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "playlist-use".into(),
            argument: Some("night".into()),
        },
        at,
    );
    assert!(!refused.ok);
    assert!(refused.message.contains("previously crashed"));
    assert_eq!(fs::read_to_string(&launches).unwrap().lines().count(), 1);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn crashed_video_falls_back_but_can_be_attempted_on_a_later_visit() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("video-crash");
    let events = root.join("events");
    let launches = root.join("video-launches");
    let noctalia = root.join("noctalia");
    let mpvpaper = root.join("mpvpaper");
    fs::write(
        &noctalia,
        format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\n", events),
    )
    .unwrap();
    fs::write(
        &mpvpaper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nexit 23\n",
            launches
        ),
    )
    .unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    fs::set_permissions(&mpvpaper, fs::Permissions::from_mode(0o755)).unwrap();
    let parsed: Config = toml::from_str(&config(&noctalia, &mpvpaper, false)).unwrap();
    let video = parsed.playlists[0].entries[1].clone();
    let mut driver = SystemDriver::new(parsed.renderer.clone());

    for _visit in 0..2 {
        without_text_file_busy(|| driver.apply(&video, "eDP-1", &parsed.settings)).unwrap();
        thread::sleep(Duration::from_millis(100));
        let failures = driver.poll_failures();
        assert_eq!(failures.len(), 1);
        assert!(failures[0].contains("video entry \"video-two\""));
        assert!(!driver.motion_active("eDP-1"));
    }

    assert_eq!(fs::read_to_string(&launches).unwrap().lines().count(), 2);
    assert_eq!(
        fs::read_to_string(&events)
            .unwrap()
            .lines()
            .filter(|line| line.contains("wallpaper-set eDP-1 /tmp/two.png"))
            .count(),
        4
    );
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

#[derive(Default)]
struct RuntimeDriverState {
    applies: Vec<(String, bool)>,
    pauses: Vec<bool>,
    stops: usize,
    motion_active: bool,
    fail_apply: bool,
}

#[derive(Clone)]
struct RuntimeDriver(Arc<Mutex<RuntimeDriverState>>);

impl WallpaperDriver for RuntimeDriver {
    fn apply(
        &mut self,
        entry: &wall_in_one_service::config::Entry,
        _output: &str,
        settings: &wall_in_one_service::config::Settings,
    ) -> Result<(), String> {
        let mut state = self.0.lock().unwrap();
        state
            .applies
            .push((entry.id.clone(), settings.dynamics_enabled));
        if state.fail_apply {
            return Err("renderer refused resume".into());
        }
        state.motion_active = settings.dynamics_enabled
            && entry.kind != wall_in_one_service::config::EntryKind::Still;
        Ok(())
    }

    fn set_paused(&mut self, paused: bool) {
        self.0.lock().unwrap().pauses.push(paused);
    }

    fn reconfigure(&mut self, _settings: wall_in_one_service::config::RendererSettings) {}

    fn motion_active(&self, _output: &str) -> bool {
        self.0.lock().unwrap().motion_active
    }

    fn stop(&mut self) {
        let mut state = self.0.lock().unwrap();
        state.stops += 1;
        state.motion_active = false;
    }
}

fn status(runtime: &mut Runtime<RuntimeDriver>, at: chrono::NaiveDateTime) -> serde_json::Value {
    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "status".into(),
            argument: None,
        },
        at,
    );
    assert!(response.ok, "{}", response.message);
    serde_json::from_str(&response.message).unwrap()
}

#[test]
fn cycle_runtime_override_stops_advancement_without_stopping_motion() {
    let document = config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        .replace("cycle_enabled = false", "cycle_enabled = true");
    let parsed: Config = toml::from_str(&document).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "next".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    assert!(state.lock().unwrap().motion_active);
    let entry_before = status(&mut runtime, at)["entry_id"].clone();

    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "cycle".into(),
            argument: Some("off".into()),
        },
        at,
    );
    assert_eq!(response.message, "cycle off (manual)");
    runtime.tick(at, Instant::now() + Duration::from_secs(600));
    let stopped_cycle = status(&mut runtime, at);
    assert_eq!(stopped_cycle["entry_id"], entry_before);
    assert_eq!(stopped_cycle["motion_active"], true);
    assert_eq!(stopped_cycle["cycle_enabled"], false);
    assert_eq!(stopped_cycle["cycle_default"], true);
    assert_eq!(stopped_cycle["cycle_source"], "manual");

    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "cycle".into(),
            argument: Some("default".into()),
        },
        at,
    );
    assert_eq!(response.message, "cycle on (config)");
    let following = status(&mut runtime, at);
    assert_eq!(following["cycle_enabled"], true);
    assert_eq!(following["cycle_source"], "config");
    runtime.tick(at, Instant::now() + Duration::from_secs(600));
    assert_ne!(status(&mut runtime, at)["entry_id"], entry_before);
}

#[test]
fn stop_releases_motion_while_pause_keeps_it_resident_and_play_resumes() {
    let parsed: Config = toml::from_str(&config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        false,
    ))
    .unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "next".into(),
            argument: None,
        },
        at,
    );
    assert!(state.lock().unwrap().motion_active);

    assert_eq!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "pause".into(),
                    argument: None,
                },
                at,
            )
            .message,
        "paused"
    );
    let paused = status(&mut runtime, at);
    assert_eq!(paused["playback_state"], "paused");
    assert_eq!(paused["paused"], true);
    assert_eq!(paused["stopped"], false);
    assert!(state.lock().unwrap().motion_active);

    assert_eq!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "stop".into(),
                    argument: None,
                },
                at,
            )
            .message,
        "stopped; paired still remains active"
    );
    let stopped = status(&mut runtime, at);
    assert_eq!(stopped["playback_state"], "stopped");
    assert_eq!(stopped["paused"], false);
    assert_eq!(stopped["stopped"], true);
    assert_eq!(stopped["motion_active"], false);
    {
        let recorded = state.lock().unwrap();
        assert_eq!(recorded.pauses, vec![true, false]);
        assert_eq!(recorded.stops, 1);
    }

    // Moving while stopped changes the still but never launches its motion.
    runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "previous".into(),
            argument: None,
        },
        at,
    );
    let last = state.lock().unwrap().applies.last().cloned().unwrap();
    assert_eq!(last, ("still-one".into(), false));
    runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "next".into(),
            argument: None,
        },
        at,
    );
    let last = state.lock().unwrap().applies.last().cloned().unwrap();
    assert_eq!(last, ("video-two".into(), false));

    assert_eq!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "toggle".into(),
                    argument: None,
                },
                at,
            )
            .message,
        "playing"
    );
    let resumed = status(&mut runtime, at);
    assert_eq!(resumed["playback_state"], "playing");
    assert_eq!(resumed["motion_active"], true);
    assert_eq!(
        state.lock().unwrap().applies.last(),
        Some(&("video-two".into(), true))
    );
}

#[test]
fn failed_resume_stays_stopped_and_reports_the_renderer_error() {
    let parsed: Config = toml::from_str(&config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        false,
    ))
    .unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "stop".into(),
            argument: None,
        },
        at,
    );
    state.lock().unwrap().fail_apply = true;

    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "play".into(),
            argument: None,
        },
        at,
    );
    assert!(!response.ok);
    assert_eq!(response.message, "renderer refused resume");
    let stopped = status(&mut runtime, at);
    assert_eq!(stopped["playback_state"], "stopped");
    assert_eq!(stopped["stopped"], true);
    assert_eq!(stopped["motion_active"], false);
    assert_eq!(stopped["last_error"], "renderer refused resume");
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
    assert_eq!(status["displays"][0]["assigned_playlist_id"], "night");
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
