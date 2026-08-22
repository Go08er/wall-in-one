use chrono::NaiveDate;
use std::collections::HashSet;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use wall_in_one_service::config::Config;
use wall_in_one_service::protocol::{write_response, Response, MAX_RESPONSE_BYTES};
use wall_in_one_service::renderer::{RendererFailure, SystemDriver, WallpaperDriver};
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
        r#"schema_version = 3
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
video_interpolation = "off"
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
        "shuffle on (manual)"
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
fn interpolation_uses_live_refresh_once_for_a_multi_output_handover() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("video-interpolation");
    let launches = root.join("launches");
    let queries = root.join("queries");
    let noctalia = root.join("noctalia");
    let niri = root.join("niri");
    let mpvpaper = root.join("mpvpaper");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::write(
        &niri,
        format!(
            "#!/bin/sh\nprintf x >> {:?}\nprintf '%s\\n' '{{\"eDP-1\":{{\"current_mode\":0,\"modes\":[{{\"refresh_rate\":165004}}]}},\"DP-1\":{{\"current_mode\":0,\"modes\":[{{\"refresh_rate\":165004}}]}}}}'\n",
            queries
        ),
    )
    .unwrap();
    fs::write(
        &mpvpaper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nsleep 30\n",
            launches
        ),
    )
    .unwrap();
    for executable in [&noctalia, &niri, &mpvpaper] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, &mpvpaper, false)
        .replace(
            "niri_program = \"/bin/true\"",
            &format!("niri_program = {niri:?}"),
        )
        .replace(
            "video_interpolation = \"off\"",
            "video_interpolation = \"oversample\"",
        )
        .replace(
            "video_hardware_decode = true",
            "video_hardware_decode = false",
        );
    let parsed: Config = toml::from_str(&document).unwrap();
    let video = parsed.playlists[0].entries[1].clone();
    let mut driver = SystemDriver::new(parsed.renderer.clone());

    driver.begin_apply();
    assert_eq!(
        driver.connected_outputs().unwrap(),
        vec!["DP-1".to_string(), "eDP-1".to_string()]
    );
    without_text_file_busy(|| driver.apply(&video, "eDP-1", &parsed.settings)).unwrap();
    without_text_file_busy(|| driver.apply(&video, "DP-1", &parsed.settings)).unwrap();
    driver.end_apply();
    thread::sleep(Duration::from_millis(100));

    assert_eq!(fs::read_to_string(&queries).unwrap(), "x");
    let arguments = fs::read_to_string(&launches).unwrap();
    assert_eq!(arguments.lines().count(), 2);
    for expected in [
        "video-sync=display-resample",
        "interpolation=yes",
        "display-fps-override=165.004",
        "tscale=oversample",
        "hwdec=no",
    ] {
        assert_eq!(arguments.matches(expected).count(), 2, "missing {expected}");
    }
    driver.stop();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn interpolation_off_never_queries_niri() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("video-interpolation-off");
    let queried = root.join("queried");
    let launches = root.join("launches");
    let noctalia = root.join("noctalia");
    let niri = root.join("niri");
    let mpvpaper = root.join("mpvpaper");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::write(&niri, format!("#!/bin/sh\ntouch {:?}\nexit 1\n", queried)).unwrap();
    fs::write(
        &mpvpaper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nsleep 30\n",
            launches
        ),
    )
    .unwrap();
    for executable in [&noctalia, &niri, &mpvpaper] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, &mpvpaper, false).replace(
        "niri_program = \"/bin/true\"",
        &format!("niri_program = {niri:?}"),
    );
    let parsed: Config = toml::from_str(&document).unwrap();
    let video = parsed.playlists[0].entries[1].clone();
    let mut driver = SystemDriver::new(parsed.renderer.clone());

    without_text_file_busy(|| driver.apply(&video, "eDP-1", &parsed.settings)).unwrap();
    thread::sleep(Duration::from_millis(100));
    assert!(!queried.exists());
    let arguments = fs::read_to_string(&launches).unwrap();
    for absent in [
        "video-sync=",
        "interpolation=",
        "display-fps-override=",
        "tscale=",
    ] {
        assert!(!arguments.contains(absent), "unexpected {absent}");
    }
    driver.stop();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn mixed_all_output_refresh_launches_unsmoothed() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("video-interpolation-mixed");
    let launches = root.join("launches");
    let noctalia = root.join("noctalia");
    let niri = root.join("niri");
    let mpvpaper = root.join("mpvpaper");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::write(
        &niri,
        "#!/bin/sh\nprintf '%s\\n' '{\"eDP-1\":{\"current_mode\":0,\"modes\":[{\"refresh_rate\":165004}]},\"DP-1\":{\"current_mode\":0,\"modes\":[{\"refresh_rate\":60000}]}}'\n",
    )
    .unwrap();
    fs::write(
        &mpvpaper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nsleep 30\n",
            launches
        ),
    )
    .unwrap();
    for executable in [&noctalia, &niri, &mpvpaper] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, &mpvpaper, false)
        .replace(
            "niri_program = \"/bin/true\"",
            &format!("niri_program = {niri:?}"),
        )
        .replace(
            "video_interpolation = \"off\"",
            "video_interpolation = \"linear\"",
        );
    let parsed: Config = toml::from_str(&document).unwrap();
    let video = parsed.playlists[0].entries[1].clone();
    let mut driver = SystemDriver::new(parsed.renderer.clone());

    without_text_file_busy(|| driver.apply(&video, "", &parsed.settings)).unwrap();
    thread::sleep(Duration::from_millis(100));
    let arguments = fs::read_to_string(&launches).unwrap();
    assert!(arguments.contains(" ALL /tmp/two.mp4"));
    for absent in [
        "video-sync=",
        "interpolation=",
        "display-fps-override=",
        "tscale=",
    ] {
        assert!(!arguments.contains(absent), "unexpected {absent}");
    }
    driver.stop();
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
        .replace("scene_fps = 30", "scene_fps = 75")
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
    assert!(launched.contains("--fps 75"));
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
        .write_all(
            b"#!/bin/sh\nif [ \"$2\" = color-scheme-set ]; then printf '%s\\n' 'palette was rejected' >&2; exit 1; fi\n",
        )
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
    assert!(
        error.contains("palette was rejected"),
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
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nprintf '%s\\n' 'unsupported scene shader' >&2\nexit 42\n",
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
    assert!(status["last_error"]
        .as_str()
        .unwrap()
        .contains("unsupported scene shader"));
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
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nprintf '%s\\n' 'video EGL startup failed' >&2\nexit 23\n",
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
        assert!(failures[0].message.contains("video entry \"video-two\""));
        assert!(failures[0].message.contains("video EGL startup failed"));
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
    fn set_paused(&mut self, _paused: bool) -> Result<(), String> {
        Ok(())
    }
    fn reconfigure(&mut self, _settings: wall_in_one_service::config::RendererSettings) {}
    fn stop(&mut self) {}
}

#[derive(Default)]
struct RuntimeDriverState {
    applies: Vec<(String, bool)>,
    applied_outputs: Vec<String>,
    retained_outputs: Vec<Vec<String>>,
    pauses: Vec<bool>,
    stops: usize,
    motion_active: bool,
    active_outputs: HashSet<String>,
    fail_apply: bool,
    fail_applies_remaining: usize,
    fail_entry_id: Option<String>,
    fail_entry_attempts_remaining: usize,
    fail_pause: bool,
    failures: Vec<String>,
    renderer_failures: Vec<RendererFailure>,
    connected_outputs: Option<Vec<String>>,
    output_probes: usize,
    video_audio: Vec<(bool, u8)>,
    reconfigures: usize,
}

#[derive(Clone)]
struct RuntimeDriver(Arc<Mutex<RuntimeDriverState>>);

impl WallpaperDriver for RuntimeDriver {
    fn connected_outputs(&mut self) -> Result<Vec<String>, String> {
        let mut state = self.0.lock().unwrap();
        state.output_probes += 1;
        state
            .connected_outputs
            .clone()
            .ok_or_else(|| "test compositor output discovery unavailable".into())
    }

    fn apply(
        &mut self,
        entry: &wall_in_one_service::config::Entry,
        output: &str,
        settings: &wall_in_one_service::config::Settings,
    ) -> Result<(), String> {
        let mut state = self.0.lock().unwrap();
        state
            .applies
            .push((entry.id.clone(), settings.dynamics_enabled));
        state.applied_outputs.push(output.to_string());
        if state.fail_apply {
            return Err("renderer refused resume".into());
        }
        if state.fail_entry_id.as_deref() == Some(&entry.id)
            && state.fail_entry_attempts_remaining > 0
        {
            state.fail_entry_attempts_remaining -= 1;
            return Err(format!("entry {} is borked", entry.id));
        }
        if state.fail_applies_remaining > 0 {
            state.fail_applies_remaining -= 1;
            return Err("renderer refused candidate".into());
        }
        state.motion_active = settings.dynamics_enabled
            && entry.kind != wall_in_one_service::config::EntryKind::Still;
        if state.motion_active {
            state.active_outputs.insert(output.to_string());
        } else {
            state.active_outputs.remove(output);
        }
        Ok(())
    }

    fn retain_outputs(&mut self, outputs: &[String]) {
        let mut state = self.0.lock().unwrap();
        state.retained_outputs.push(outputs.to_vec());
        let wanted: HashSet<&str> = outputs.iter().map(String::as_str).collect();
        state
            .active_outputs
            .retain(|output| wanted.contains(output.as_str()));
        state.motion_active = !state.active_outputs.is_empty();
    }

    fn set_paused(&mut self, paused: bool) -> Result<(), String> {
        let mut state = self.0.lock().unwrap();
        state.pauses.push(paused);
        if state.fail_pause {
            Err("pause transport failed".into())
        } else {
            Ok(())
        }
    }

    fn set_video_audio(&mut self, muted: bool, volume: u8) -> Result<(), String> {
        self.0.lock().unwrap().video_audio.push((muted, volume));
        Ok(())
    }

    fn reconfigure(&mut self, _settings: wall_in_one_service::config::RendererSettings) {
        self.0.lock().unwrap().reconfigures += 1;
    }

    fn motion_active(&self, output: &str) -> bool {
        self.0.lock().unwrap().active_outputs.contains(output)
    }

    fn poll_failures(&mut self) -> Vec<RendererFailure> {
        let mut state = self.0.lock().unwrap();
        let mut structured = std::mem::take(&mut state.renderer_failures);
        structured.extend(
            std::mem::take(&mut state.failures)
                .into_iter()
                .map(|message| RendererFailure {
                    entry_id: String::new(),
                    kind: wall_in_one_service::config::EntryKind::Video,
                    scene_id: None,
                    output: String::new(),
                    message,
                    permanent_for_session: false,
                })
                .collect::<Vec<_>>(),
        );
        structured
    }

    fn stop(&mut self) {
        let mut state = self.0.lock().unwrap();
        state.stops += 1;
        state.motion_active = false;
        state.active_outputs.clear();
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
fn thirty_two_renderer_failures_keep_attribution_and_fit_the_status_wire() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        failures: (0..32)
            .map(|index| {
                format!(
                    "scene wallpaper-{index} crashed; stderr: {} TAIL-{index}\n",
                    "diagnostic".repeat(1200)
                )
            })
            .collect(),
        ..RuntimeDriverState::default()
    }));
    let parsed: Config = toml::from_str(&config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        false,
    ))
    .unwrap();
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();

    runtime.tick(at, Instant::now());
    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "status".into(),
            argument: None,
        },
        at,
    );
    assert!(response.ok, "{}", response.message);
    let snapshot: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    let error = snapshot["last_error"].as_str().unwrap();
    assert!(error.len() <= 12 * 1024, "{} bytes", error.len());
    assert!(!error.contains('\n'));
    for index in [0, 31] {
        assert!(
            error.contains(&format!("scene wallpaper-{index}")),
            "{error}"
        );
        assert!(error.contains(&format!("TAIL-{index}")), "{error}");
    }
    assert!(error.contains("[truncated]"), "{error}");
    let mut encoded = Vec::new();
    write_response(&mut encoded, &response).unwrap();
    assert!(encoded.len() <= MAX_RESPONSE_BYTES);
}

#[test]
fn oversized_discovered_connector_is_never_a_renderer_target_or_status_field() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let document = format!(
        "{}\n[[displays]]\nconnector = \"eDP-1\"\nplaylist = \"day\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    );
    let parsed: Config = toml::from_str(&document).unwrap();
    let oversized = "x".repeat(257);
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec![oversized.clone(), "eDP-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();

    runtime.apply_current().unwrap();
    assert_eq!(state.lock().unwrap().applied_outputs, ["eDP-1"]);
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["displays"][0]["connector"], "eDP-1");
    assert!(!snapshot.to_string().contains(&oversized));
}

#[test]
fn no_argument_runtime_verbs_reject_junk_without_side_effects() {
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

    for verb in [
        "status",
        "next",
        "previous",
        "random",
        "schedule-follow",
        "play",
        "pause",
        "stop",
        "toggle",
        "reload",
        "quit",
    ] {
        let response = runtime.handle(
            wall_in_one_service::protocol::Request {
                verb: verb.into(),
                argument: Some("junk".into()),
            },
            at,
        );
        assert!(!response.ok, "{verb} accepted an ignored argument");
        assert_eq!(response.message, format!("usage: {verb}"));
    }
    assert!(
        !runtime.should_quit(),
        "rejected quit must not stop the service"
    );
    let recorded = state.lock().unwrap();
    assert!(recorded.applies.is_empty());
    assert!(recorded.pauses.is_empty());
    assert_eq!(recorded.stops, 0);
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
fn scheduled_playlist_gets_a_full_residency_interval_before_cycling() {
    let document = config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        .replace("cycle_interval_seconds = 300", "cycle_interval_seconds = 5")
        .replace("cycle_enabled = false", "cycle_enabled = true");
    let mut parsed: Config = toml::from_str(&document).unwrap();
    let mut second = parsed.playlists[1].entries[0].clone();
    second.id = "scene-four".into();
    second.scene_id = Some("12346".into());
    parsed.playlists[1].entries.push(second);
    let summer = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(23, 0, 0)
        .unwrap();
    let winter = NaiveDate::from_ymd_opt(2026, 12, 3)
        .unwrap()
        .and_hms_opt(23, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        summer,
    )
    .unwrap();
    runtime.apply_current().unwrap();

    let transition = Instant::now() + Duration::from_secs(600);
    runtime.tick(winter, transition);
    assert_eq!(status(&mut runtime, winter)["entry_id"], "scene-three");
    runtime.tick(winter, transition + Duration::from_secs(4));
    assert_eq!(status(&mut runtime, winter)["entry_id"], "scene-three");
    runtime.tick(winter, transition + Duration::from_secs(5));
    assert_eq!(status(&mut runtime, winter)["entry_id"], "scene-four");
}

#[test]
fn automatic_cycle_retries_three_times_then_marks_and_skips_the_borked_entry() {
    let document = config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        .replace("cycle_interval_seconds = 300", "cycle_interval_seconds = 5")
        .replace("cycle_enabled = false", "cycle_enabled = true");
    let mut parsed: Config = toml::from_str(&document).unwrap();
    parsed.schedules.clear();
    let mut third = parsed.playlists[0].entries[0].clone();
    third.id = "still-four".into();
    third.still = "/tmp/four.png".into();
    parsed.playlists[0].entries.push(third);
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
    {
        let mut recorded = state.lock().unwrap();
        recorded.fail_entry_id = Some("video-two".into());
        recorded.fail_entry_attempts_remaining = 3;
    }

    let due = Instant::now() + Duration::from_secs(600);
    runtime.tick(at, due);
    let after_first = status(&mut runtime, at);
    assert_eq!(after_first["entry_id"], "still-one");
    assert_eq!(after_first["automatic_retry"]["attempt"], 1);
    assert_eq!(after_first["automatic_retry"]["maximum_attempts"], 3);
    assert_eq!(after_first["automatic_retry"]["reason"], "cycle");
    assert_eq!(
        state
            .lock()
            .unwrap()
            .applies
            .iter()
            .filter(|apply| apply.0 == "video-two")
            .count(),
        1
    );

    runtime.tick(at, due + Duration::from_secs(1));
    assert_eq!(
        state
            .lock()
            .unwrap()
            .applies
            .iter()
            .filter(|apply| apply.0 == "video-two")
            .count(),
        1,
        "a retry must not happen before its delay"
    );
    runtime.tick(at, due + Duration::from_secs(2));
    assert_eq!(status(&mut runtime, at)["automatic_retry"]["attempt"], 2);
    runtime.tick(at, due + Duration::from_secs(4));

    let taboo = status(&mut runtime, at);
    assert_eq!(taboo["entry_id"], "still-one");
    assert!(taboo["automatic_retry"].is_null());
    assert_eq!(taboo["taboo_entries"].as_array().unwrap().len(), 1);
    assert_eq!(taboo["taboo_entries"][0]["playlist_id"], "day");
    assert_eq!(taboo["taboo_entries"][0]["entry_id"], "video-two");
    assert_eq!(taboo["taboo_entries"][0]["source"], "automatic-apply");
    assert!(taboo["taboo_entries"][0]["reason"]
        .as_str()
        .unwrap()
        .contains("borked"));
    assert_eq!(taboo["taboo_entries_omitted"], 0);

    runtime.tick(at, due + Duration::from_secs(5));
    assert_eq!(status(&mut runtime, at)["entry_id"], "still-four");
    assert_eq!(
        state
            .lock()
            .unwrap()
            .applies
            .iter()
            .filter(|apply| apply.0 == "video-two")
            .count(),
        3,
        "the taboo entry must never be selected automatically again"
    );
}

#[test]
fn renderer_crash_is_attributed_and_never_enters_the_apply_retry_machine() {
    let parsed: Config = toml::from_str(&config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        true,
    ))
    .unwrap();
    let winter = NaiveDate::from_ymd_opt(2026, 12, 3)
        .unwrap()
        .and_hms_opt(23, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        winter,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let applies_before = state.lock().unwrap().applies.len();
    state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
        entry_id: "scene-three".into(),
        kind: wall_in_one_service::config::EntryKind::Scene,
        scene_id: Some("12345".into()),
        output: String::new(),
        message:
            "scene 12345 (entry scene-three) crashed linux-wallpaperengine; paired still is active"
                .into(),
        permanent_for_session: true,
    });

    runtime.tick(winter, Instant::now());
    runtime.tick(winter, Instant::now() + Duration::from_secs(1));
    let snapshot = status(&mut runtime, winter);
    assert_eq!(state.lock().unwrap().applies.len(), applies_before);
    assert!(snapshot["automatic_retry"].is_null());
    assert_eq!(snapshot["taboo_entries"][0]["entry_id"], "scene-three");
    assert_eq!(snapshot["taboo_entries"][0]["scene_id"], "12345");
    assert_eq!(snapshot["taboo_entries"][0]["source"], "renderer-crash");
    assert!(snapshot["last_error"]
        .as_str()
        .unwrap()
        .contains("linux-wallpaperengine"));
}

#[test]
fn persisted_taboo_is_loaded_before_startup_selects_or_launches_motion() {
    let document = config(Path::new("/bin/true"), Path::new("/bin/true"), false).replace(
        "still = \"/tmp/one.png\"",
        "still = \"/tmp/one.png\"\n\
         taboo = { reason = \"known decoder failure\", source = \"automatic-apply\" }",
    );
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
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["entry_id"], "video-two");
    assert_eq!(snapshot["taboo_entries"][0]["entry_id"], "still-one");
    assert_eq!(
        snapshot["taboo_entries"][0]["reason"],
        "known decoder failure"
    );
    assert_eq!(state.lock().unwrap().applies.last().unwrap().0, "video-two");
}

#[test]
fn reload_preserves_session_findings_but_app_clear_removes_durable_taboo_before_retry() {
    let root = directory("durable-taboo-clear");
    let config_path = root.join("runtime.toml");
    let original = config(Path::new("/bin/true"), Path::new("/bin/true"), false);
    fs::write(&config_path, &original).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        config_path.clone(),
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
    state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
            entry_id: "video-two".into(),
            kind: wall_in_one_service::config::EntryKind::Video,
            scene_id: None,
            output: String::new(),
            message: "video-two crashed and its still is active".into(),
            permanent_for_session: true,
        });
    runtime.tick(at, Instant::now());
    assert_eq!(
        status(&mut runtime, at)["taboo_entries"][0]["entry_id"],
        "video-two"
    );

    // An unrelated compiler reload before the app has consumed status must
    // not mistake absence for an author-owned clear.
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "reload".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    assert_eq!(
        status(&mut runtime, at)["taboo_entries"][0]["entry_id"],
        "video-two"
    );

    let persisted = original.replace(
        "motion = \"/tmp/two.mp4\"",
        "motion = \"/tmp/two.mp4\"\n\
         taboo = { reason = \"video-two crashed and its still is active\", source = \"renderer-crash\" }",
    );
    fs::write(&config_path, persisted).unwrap();
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "reload".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    assert_eq!(
        status(&mut runtime, at)["taboo_entries"][0]["source"],
        "renderer-crash"
    );
    assert_eq!(
        state.lock().unwrap().applies.last().unwrap(),
        &("video-two".into(), false)
    );

    // The app is the sole writer. Removing metadata is its explicit Retry:
    // Rust drops the durable key before applying the changed entry.
    fs::write(&config_path, &original).unwrap();
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "reload".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    let cleared = status(&mut runtime, at);
    assert!(cleared["taboo_entries"].as_array().unwrap().is_empty());
    assert_eq!(
        state.lock().unwrap().applies.last().unwrap(),
        &("video-two".into(), true)
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn shuffle_bag_covers_each_entry_then_reshuffles_without_a_seam_repeat() {
    let document = config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        .replace("shuffle = false", "shuffle = true");
    let mut parsed: Config = toml::from_str(&document).unwrap();
    parsed.schedules.clear();
    for (id, path) in [
        ("still-four", "/tmp/four.png"),
        ("still-five", "/tmp/five.png"),
    ] {
        let mut entry = parsed.playlists[0].entries[0].clone();
        entry.id = id.into();
        entry.still = path.into();
        parsed.playlists[0].entries.push(entry);
    }
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();

    let mut first_round = vec![status(&mut runtime, at)["entry_id"]
        .as_str()
        .unwrap()
        .to_string()];
    for _ in 1..4 {
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
        first_round.push(
            status(&mut runtime, at)["entry_id"]
                .as_str()
                .unwrap()
                .to_string(),
        );
    }
    assert_eq!(first_round.iter().collect::<HashSet<_>>().len(), 4);
    let seam_previous = first_round.last().unwrap().clone();
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
    let seam_current = status(&mut runtime, at)["entry_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert_ne!(seam_current, seam_previous);

    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "previous".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    assert_eq!(status(&mut runtime, at)["entry_id"], seam_previous);
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
    assert_eq!(status(&mut runtime, at)["entry_id"], seam_current);

    let mut second_round = vec![seam_current];
    for _ in 1..4 {
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
        second_round.push(
            status(&mut runtime, at)["entry_id"]
                .as_str()
                .unwrap()
                .to_string(),
        );
    }
    assert_eq!(second_round.iter().collect::<HashSet<_>>().len(), 4);
}

#[test]
fn manual_playlist_and_resume_each_start_a_fresh_cycle_interval() {
    let document = config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        .replace("cycle_interval_seconds = 300", "cycle_interval_seconds = 5")
        .replace("cycle_enabled = false", "cycle_enabled = true");
    let mut parsed: Config = toml::from_str(&document).unwrap();
    parsed.schedules.clear();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();

    let selected_at = Instant::now();
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "playlist-use".into(),
                    argument: Some("day".into()),
                },
                at,
            )
            .ok
    );
    assert_eq!(status(&mut runtime, at)["entry_id"], "still-one");
    runtime.tick(at, selected_at + Duration::from_secs(4));
    assert_eq!(status(&mut runtime, at)["entry_id"], "still-one");
    runtime.tick(at, selected_at + Duration::from_secs(6));
    assert_eq!(status(&mut runtime, at)["entry_id"], "video-two");

    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "pause".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    let resumed_at = Instant::now();
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "play".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    runtime.tick(at, resumed_at + Duration::from_secs(4));
    assert_eq!(status(&mut runtime, at)["entry_id"], "video-two");
    runtime.tick(at, resumed_at + Duration::from_secs(6));
    assert_eq!(status(&mut runtime, at)["entry_id"], "still-one");
}

#[test]
fn shuffle_runtime_override_survives_reload_and_can_follow_config_again() {
    let root = directory("shuffle-reload");
    let config_path = root.join("runtime.toml");
    let original = config(Path::new("/bin/true"), Path::new("/bin/true"), false);
    fs::write(&config_path, &original).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(config_path.clone(), parsed, RuntimeDriver(state), at).unwrap();

    let enabled = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "shuffle".into(),
            argument: Some("on".into()),
        },
        at,
    );
    assert_eq!(enabled.message, "shuffle on (manual)");
    fs::write(
        &config_path,
        original.replace(
            "cycle_interval_seconds = 300",
            "cycle_interval_seconds = 301",
        ),
    )
    .unwrap();
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "reload".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    let reloaded = status(&mut runtime, at);
    assert_eq!(reloaded["shuffle"], true);
    assert_eq!(reloaded["shuffle_default"], false);
    assert_eq!(reloaded["shuffle_source"], "manual");

    let following = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "shuffle".into(),
            argument: Some("default".into()),
        },
        at,
    );
    assert_eq!(following.message, "shuffle off (config)");
    let following = status(&mut runtime, at);
    assert_eq!(following["shuffle"], false);
    assert_eq!(following["shuffle_source"], "config");
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn target_reconciliation_removes_disconnected_all_and_empty_renderers() {
    let root = directory("target-reconciliation");
    let config_path = root.join("runtime.toml");
    let base = config(Path::new("/bin/true"), Path::new("/bin/true"), false);
    let two_displays = format!(
        "{base}\n[[displays]]\nconnector = \"eDP-1\"\nplaylist = \"day\"\n\
         [[displays]]\nconnector = \"DP-2\"\nplaylist = \"day\"\n"
    );
    fs::write(&config_path, &two_displays).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        config_path.clone(),
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
    assert_eq!(
        state.lock().unwrap().active_outputs,
        HashSet::from(["eDP-1".into(), "DP-2".into()])
    );

    let one_display = format!("{base}\n[[displays]]\nconnector = \"eDP-1\"\nplaylist = \"day\"\n");
    fs::write(&config_path, one_display).unwrap();
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "reload".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    assert_eq!(
        state.lock().unwrap().active_outputs,
        HashSet::from(["eDP-1".into()])
    );

    fs::write(&config_path, &base).unwrap();
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "reload".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    assert_eq!(
        state.lock().unwrap().active_outputs,
        HashSet::from([String::new()])
    );

    let entries_start = base.find("[[playlists.entries]]").unwrap();
    let night_start = base.find("[[playlists]]\nid = \"night\"").unwrap();
    assert!(entries_start < night_start);
    let empty_day = format!(
        "{}entries = []\n{}",
        &base[..entries_start],
        &base[night_start..]
    );
    fs::write(&config_path, empty_day).unwrap();
    let emptied = Config::load(&config_path).unwrap();
    assert!(emptied.playlist("day").unwrap().entries.is_empty());
    let before_rejected_reload = status(&mut runtime, at);
    let reply = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "reload".into(),
            argument: None,
        },
        at,
    );
    assert!(!reply.ok);
    assert!(reply.message.contains("previous configuration restored"));
    let after_rejected_reload = status(&mut runtime, at);
    assert_eq!(after_rejected_reload["playlist_id"], "day");
    assert_eq!(
        after_rejected_reload["entry_id"],
        before_rejected_reload["entry_id"]
    );
    assert_eq!(
        after_rejected_reload["playlists"][0]["entries"],
        before_rejected_reload["playlists"][0]["entries"]
    );
    assert_eq!(
        state.lock().unwrap().active_outputs,
        HashSet::from([String::new()])
    );
    assert_eq!(
        state.lock().unwrap().retained_outputs.last(),
        Some(&vec![String::new()])
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn failed_reload_apply_restores_config_cursor_renderer_settings_and_wallpaper() {
    let root = directory("transactional-reload");
    let config_path = root.join("runtime.toml");
    let original = config(Path::new("/bin/true"), Path::new("/bin/true"), false);
    fs::write(&config_path, &original).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        config_path.clone(),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let before = status(&mut runtime, at);

    fs::write(
        &config_path,
        original
            .replace("default_playlist = \"day\"", "default_playlist = \"night\"")
            .replace("layer = \"background\"", "layer = \"bottom\""),
    )
    .unwrap();
    state.lock().unwrap().fail_applies_remaining = 1;
    let reply = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "reload".into(),
            argument: None,
        },
        at,
    );

    assert!(!reply.ok);
    assert!(reply.message.contains("renderer refused candidate"));
    assert!(reply.message.contains("previous configuration restored"));
    let after = status(&mut runtime, at);
    assert_eq!(after["playlist_id"], before["playlist_id"]);
    assert_eq!(after["entry_id"], before["entry_id"]);
    let recorded = state.lock().unwrap();
    assert_eq!(recorded.reconfigures, 2, "candidate then rollback settings");
    assert_eq!(
        recorded.applies.last().map(|apply| apply.0.as_str()),
        Some("still-one"),
        "the prior entry must be reapplied after the candidate fails"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn pause_and_resume_failures_do_not_lie_about_playback_state() {
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
    state.lock().unwrap().fail_pause = true;
    let refused = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "pause".into(),
            argument: None,
        },
        at,
    );
    assert!(!refused.ok);
    assert!(refused.message.contains("pause transport failed"));
    assert_eq!(status(&mut runtime, at)["playback_state"], "playing");

    state.lock().unwrap().fail_pause = false;
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "pause".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    state.lock().unwrap().fail_pause = true;
    let refused = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "play".into(),
            argument: None,
        },
        at,
    );
    assert!(!refused.ok);
    assert!(refused.message.contains("pause transport failed"));
    assert_eq!(status(&mut runtime, at)["playback_state"], "paused");
}

#[test]
fn play_retries_a_crashed_video_renderer() {
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
    let applies_before = state.lock().unwrap().applies.len();
    {
        let mut recorded = state.lock().unwrap();
        recorded.active_outputs.clear();
        recorded.motion_active = false;
        recorded.failures.push(
            "video entry \"video-two\" stopped because mpvpaper exited; paired still is active"
                .into(),
        );
    }
    runtime.tick(at, Instant::now());
    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "play".into(),
            argument: None,
        },
        at,
    );
    assert!(response.ok, "{}", response.message);
    assert!(state.lock().unwrap().applies.len() > applies_before);
    assert_eq!(status(&mut runtime, at)["motion_active"], true);
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

    let moved = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "previous".into(),
            argument: None,
        },
        at,
    );
    assert_eq!(moved.message, "paused still-one");
    let moved = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "next".into(),
            argument: None,
        },
        at,
    );
    assert_eq!(moved.message, "paused video-two");

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
        assert_eq!(recorded.pauses, vec![true, true, true, false]);
        assert_eq!(recorded.stops, 1);
    }

    // Moving while stopped changes the still but never launches its motion.
    let moved = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "previous".into(),
            argument: None,
        },
        at,
    );
    assert_eq!(moved.message, "showing still-one; motion stopped");
    let last = state.lock().unwrap().applies.last().cloned().unwrap();
    assert_eq!(last, ("still-one".into(), false));
    let moved = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "next".into(),
            argument: None,
        },
        at,
    );
    assert_eq!(moved.message, "showing video-two; motion stopped");
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
fn only_a_successful_apply_or_quit_supersedes_startup_readiness() {
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
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    let initial = runtime.authoritative_generation();
    for (verb, argument) in [
        ("status", None),
        ("shuffle", Some("on")),
        ("cycle", Some("off")),
    ] {
        assert!(
            runtime
                .handle(
                    wall_in_one_service::protocol::Request {
                        verb: verb.into(),
                        argument: argument.map(str::to_string),
                    },
                    at,
                )
                .ok
        );
        assert_eq!(runtime.authoritative_generation(), initial);
    }

    runtime.apply_current().unwrap();
    let applied = runtime.authoritative_generation();
    assert_ne!(applied, initial);
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "pause".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    let paused = runtime.authoritative_generation();
    assert_eq!(paused, applied);

    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "play".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    assert_eq!(runtime.authoritative_generation(), paused);
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "toggle".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    let toggle_paused = runtime.authoritative_generation();
    assert_eq!(toggle_paused, paused);
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "stop".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    assert_eq!(runtime.authoritative_generation(), toggle_paused);
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "quit".into(),
                    argument: None,
                },
                at,
            )
            .ok
    );
    assert_ne!(runtime.authoritative_generation(), toggle_paused);
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
fn unassigned_live_output_follows_default_and_global_overrides_remain_global() {
    let mut parsed: Config = toml::from_str(&format!(
        "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"night\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    ))
    .unwrap();
    parsed.schedules[0].months.clear();
    parsed.schedules[0].start = None;
    parsed.schedules[0].end = None;
    parsed.validate().unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    // Keep the schedule out of force for the baseline apply, then inject a
    // matching time when returning from a manual override below.
    parsed.schedules[0].months = vec![12];
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-2".into(), "DP-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();

    runtime.apply_current().unwrap();
    {
        let recorded = state.lock().unwrap();
        assert_eq!(recorded.output_probes, 1);
        assert_eq!(recorded.applied_outputs, vec!["DP-1", "DP-2"]);
        assert_eq!(recorded.applies[0].0, "scene-three");
        assert_eq!(recorded.applies[1].0, "still-one");
    }
    let baseline = status(&mut runtime, at);
    assert_eq!(baseline["playlist"], "Multiple displays");
    assert_eq!(baseline["displays"][0]["connector"], "DP-1");
    assert_eq!(baseline["displays"][0]["assignment_source"], "explicit");
    assert_eq!(baseline["displays"][0]["playlist_id"], "night");
    assert_eq!(baseline["displays"][1]["connector"], "DP-2");
    assert_eq!(baseline["displays"][1]["assignment_source"], "default");
    assert_eq!(baseline["displays"][1]["assigned_playlist_id"], "day");
    assert_eq!(baseline["displays"][1]["playlist_id"], "day");

    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "playlist-use".into(),
                    argument: Some("day".into()),
                },
                at,
            )
            .ok
    );
    let manual = status(&mut runtime, at);
    assert_eq!(manual["source"], "manual");
    assert!(manual["displays"]
        .as_array()
        .unwrap()
        .iter()
        .all(|display| display["playlist_id"] == "day"));

    let scheduled_at = NaiveDate::from_ymd_opt(2026, 12, 3)
        .unwrap()
        .and_hms_opt(23, 0, 0)
        .unwrap();
    assert!(
        runtime
            .handle(
                wall_in_one_service::protocol::Request {
                    verb: "schedule-follow".into(),
                    argument: None,
                },
                scheduled_at,
            )
            .ok
    );
    let scheduled = status(&mut runtime, scheduled_at);
    assert_eq!(scheduled["source"], "schedule");
    assert!(scheduled["displays"]
        .as_array()
        .unwrap()
        .iter()
        .all(|display| display["playlist_id"] == "night"));
}

#[test]
fn output_hotplug_reconciles_targets_without_a_second_probe() {
    let mut parsed: Config = toml::from_str(&format!(
        "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"night\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    ))
    .unwrap();
    parsed.schedules.clear();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "DP-2".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    let before = Instant::now();
    runtime.apply_current().unwrap();
    {
        let mut recorded = state.lock().unwrap();
        recorded.connected_outputs = Some(vec!["DP-3".into(), "DP-2".into()]);
        recorded.applies.clear();
        recorded.applied_outputs.clear();
    }

    runtime.tick(at, before + Duration::from_secs(6));
    {
        let recorded = state.lock().unwrap();
        assert_eq!(recorded.output_probes, 2, "one probe per apply batch");
        assert_eq!(recorded.applied_outputs, vec!["DP-2", "DP-3"]);
        assert!(recorded.applies.iter().all(|apply| apply.0 == "still-one"));
        assert_eq!(
            recorded.retained_outputs.last(),
            Some(&vec!["DP-2".into(), "DP-3".into()])
        );
    }
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["displays"][0]["connector"], "DP-2");
    assert_eq!(snapshot["displays"][1]["connector"], "DP-3");
    assert_eq!(snapshot["displays"][1]["assignment_source"], "default");
}

#[test]
fn schedule_transition_replaces_and_then_restores_per_display_baselines() {
    let parsed: Config = toml::from_str(&format!(
        "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"night\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    ))
    .unwrap();
    let summer = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(23, 0, 0)
        .unwrap();
    let winter = NaiveDate::from_ymd_opt(2026, 12, 3)
        .unwrap()
        .and_hms_opt(23, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "DP-2".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        summer,
    )
    .unwrap();
    runtime.apply_current().unwrap();

    runtime.tick(winter, Instant::now());
    let scheduled = status(&mut runtime, winter);
    assert!(scheduled["displays"]
        .as_array()
        .unwrap()
        .iter()
        .all(|display| display["playlist_id"] == "night"));

    runtime.tick(summer, Instant::now());
    let baseline = status(&mut runtime, summer);
    assert_eq!(baseline["displays"][0]["playlist_id"], "night");
    assert_eq!(baseline["displays"][1]["playlist_id"], "day");
}

#[test]
fn missing_output_snapshot_degrades_to_known_targets_without_window_mode() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let partial: Config = toml::from_str(&format!(
        "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"night\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    ))
    .unwrap();
    let partial_state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(Vec::new()),
        ..RuntimeDriverState::default()
    }));
    let mut partial_runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        partial,
        RuntimeDriver(partial_state.clone()),
        at,
    )
    .unwrap();
    partial_runtime.apply_current().unwrap();
    assert_eq!(partial_state.lock().unwrap().applied_outputs, vec!["DP-1"]);
    let partial_status = status(&mut partial_runtime, at);
    assert_eq!(partial_status["displays"][0]["connector"], "DP-1");
    assert!(partial_status["output_discovery_error"]
        .as_str()
        .unwrap()
        .contains("no usable connectors"));

    let all: Config = toml::from_str(&config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        false,
    ))
    .unwrap();
    let all_state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(Vec::new()),
        ..RuntimeDriverState::default()
    }));
    let mut all_runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        all,
        RuntimeDriver(all_state.clone()),
        at,
    )
    .unwrap();
    all_runtime.apply_current().unwrap();
    assert_eq!(all_state.lock().unwrap().applied_outputs, vec![""]);
    assert_eq!(
        status(&mut all_runtime, at)["displays"][0]["connector"],
        "ALL"
    );
}

#[test]
fn transient_output_discovery_failure_keeps_the_last_live_targets() {
    let mut parsed: Config = toml::from_str(&format!(
        "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"night\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    ))
    .unwrap();
    parsed.schedules.clear();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "DP-2".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    {
        let mut recorded = state.lock().unwrap();
        recorded.connected_outputs = Some(Vec::new());
        recorded.applied_outputs.clear();
    }

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
    assert_eq!(state.lock().unwrap().applied_outputs, vec!["DP-1", "DP-2"]);
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["displays"][1]["connector"], "DP-2");
    assert!(snapshot["output_discovery_error"]
        .as_str()
        .unwrap()
        .contains("no usable connectors"));
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

#[test]
fn reload_applies_video_audio_live_without_restarting_the_renderer() {
    let root = directory("live-audio-reload");
    let config_path = root.join("runtime.toml");
    let original = config(Path::new("/bin/true"), Path::new("/bin/true"), false);
    fs::write(&config_path, &original).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let mut runtime = Runtime::new(
        config_path.clone(),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    fs::write(
        &config_path,
        original
            .replace("video_muted = true", "video_muted = false")
            .replace("video_volume = 50", "video_volume = 27"),
    )
    .unwrap();

    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "reload".into(),
            argument: None,
        },
        at,
    );

    assert!(response.ok, "{}", response.message);
    let recorded = state.lock().unwrap();
    assert_eq!(recorded.video_audio, vec![(false, 27)]);
    assert_eq!(recorded.reconfigures, 0);
    assert!(recorded.applies.is_empty());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn losing_singleton_has_no_wallpaper_side_effects() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Stdio;

    let root = directory("singleton-before-apply");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let events = root.join("events");
    let recorder = root.join("record");
    fs::write(
        &recorder,
        format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\n", events),
    )
    .unwrap();
    fs::set_permissions(&recorder, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(&config_path, config(&recorder, &recorder, false)).unwrap();

    let mut owner = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    assert_eq!(request(&socket, "status", None)["ok"], true);
    let before = fs::read_to_string(&events).unwrap();

    let contender = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .output()
        .unwrap();

    assert!(!contender.status.success());
    assert!(String::from_utf8_lossy(&contender.stderr).contains("another service"));
    assert_eq!(
        fs::read_to_string(&events).unwrap(),
        before,
        "the process that loses singleton ownership must not invoke Noctalia or a renderer"
    );
    stop(&mut owner, &socket);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn initial_desktop_readiness_failure_is_visible_and_recovers_within_the_window() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("initial-readiness");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let ready = root.join("ready");
    let events = root.join("events");
    let noctalia = root.join("noctalia");
    fs::write(
        &noctalia,
        format!(
            "#!/bin/sh\nif [ ! -f {:?} ]; then printf '%s\\n' 'desktop bus is not ready yet' >&2; exit 75; fi\nprintf '%s\\n' \"$*\" >> {:?}\n",
            ready, events
        ),
    )
    .unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(
        &config_path,
        config(&noctalia, Path::new("/bin/true"), false),
    )
    .unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .spawn()
        .unwrap();

    let failed = request(&socket, "status", None);
    let failed: serde_json::Value =
        serde_json::from_str(failed["message"].as_str().unwrap()).unwrap();
    assert!(failed["last_error"]
        .as_str()
        .unwrap()
        .contains("desktop bus is not ready yet"));
    assert_eq!(request(&socket, "shuffle", Some("on"))["ok"], true);
    assert_eq!(request(&socket, "cycle", Some("off"))["ok"], true);
    assert_eq!(request(&socket, "status", None)["ok"], true);

    fs::write(&ready, "ready\n").unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let response = request(&socket, "status", None);
        let status: serde_json::Value =
            serde_json::from_str(response["message"].as_str().unwrap()).unwrap();
        if status["last_error"] == "" {
            break;
        }
        assert!(Instant::now() < deadline, "initial apply did not recover");
        thread::sleep(Duration::from_millis(50));
    }
    let applied = fs::read_to_string(&events).unwrap();
    assert!(applied.contains("msg wallpaper-set /tmp/one.png"));

    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn pause_during_startup_readiness_applies_once_then_pauses_motion() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("pause-during-readiness");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let ready = root.join("ready");
    let events = root.join("events");
    let launches = root.join("video-launches");
    let noctalia = root.join("noctalia");
    let mpvpaper = root.join("mpvpaper");
    fs::write(
        &noctalia,
        format!(
            "#!/bin/sh\nif [ ! -f {:?} ]; then printf '%s\\n' 'desktop unavailable' >&2; exit 75; fi\nprintf '%s\\n' \"$*\" >> {:?}\n",
            ready, events
        ),
    )
    .unwrap();
    fs::write(
        &mpvpaper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nsleep 30\n",
            launches
        ),
    )
    .unwrap();
    for executable in [&noctalia, &mpvpaper] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, &mpvpaper, false).replace(
        "id = \"still-one\"\nkind = \"still\"\nstill = \"/tmp/one.png\"",
        "id = \"still-one\"\nkind = \"video\"\nstill = \"/tmp/one.png\"\nmotion = \"/tmp/one.mp4\"",
    );
    fs::write(&config_path, document).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .spawn()
        .unwrap();
    assert_eq!(request(&socket, "status", None)["ok"], true);
    assert_eq!(request(&socket, "pause", None)["message"], "paused");
    fs::write(&ready, "ready\n").unwrap();

    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let response = request(&socket, "status", None);
        let status: serde_json::Value =
            serde_json::from_str(response["message"].as_str().unwrap()).unwrap();
        if status["last_error"] == ""
            && status["playback_state"] == "paused"
            && status["motion_active"] == true
        {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "paused startup apply did not recover"
        );
        thread::sleep(Duration::from_millis(25));
    }
    let events_after_apply = fs::read_to_string(&events).unwrap();
    // A real pause may SIGSTOP the just-spawned process before its shell has
    // written the launch record. Resume it long enough to prove exactly one
    // child was started, then pause it again for the stability assertion.
    assert_eq!(request(&socket, "play", None)["message"], "playing");
    let launch_deadline = Instant::now() + Duration::from_secs(3);
    let launches_after_apply = loop {
        if let Ok(contents) = fs::read_to_string(&launches) {
            let count = contents.lines().count();
            assert!(
                count <= 1,
                "startup recovery launched mpvpaper {count} times"
            );
            if count == 1 {
                break contents;
            }
        }
        assert!(
            Instant::now() < launch_deadline,
            "mpvpaper was tracked active but its launch record never appeared"
        );
        thread::sleep(Duration::from_millis(25));
    };
    assert_eq!(request(&socket, "pause", None)["message"], "paused");
    thread::sleep(Duration::from_millis(700));
    assert_eq!(fs::read_to_string(&events).unwrap(), events_after_apply);
    assert_eq!(fs::read_to_string(&launches).unwrap(), launches_after_apply);

    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn stop_during_startup_readiness_applies_the_still_without_starting_motion() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("stop-during-readiness");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let ready = root.join("ready");
    let events = root.join("events");
    let launches = root.join("video-launches");
    let noctalia = root.join("noctalia");
    let mpvpaper = root.join("mpvpaper");
    fs::write(
        &noctalia,
        format!(
            "#!/bin/sh\nif [ ! -f {:?} ]; then printf '%s\\n' 'desktop unavailable' >&2; exit 75; fi\nprintf '%s\\n' \"$*\" >> {:?}\n",
            ready, events
        ),
    )
    .unwrap();
    fs::write(
        &mpvpaper,
        format!("#!/bin/sh\nprintf '%s\\n' launch >> {:?}\n", launches),
    )
    .unwrap();
    for executable in [&noctalia, &mpvpaper] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, &mpvpaper, false).replace(
        "id = \"still-one\"\nkind = \"still\"\nstill = \"/tmp/one.png\"",
        "id = \"still-one\"\nkind = \"video\"\nstill = \"/tmp/one.png\"\nmotion = \"/tmp/one.mp4\"",
    );
    fs::write(&config_path, document).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .spawn()
        .unwrap();
    assert_eq!(request(&socket, "status", None)["ok"], true);
    assert_eq!(
        request(&socket, "stop", None)["message"],
        "stopped; paired still remains active"
    );
    fs::write(&ready, "ready\n").unwrap();

    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let response = request(&socket, "status", None);
        let status: serde_json::Value =
            serde_json::from_str(response["message"].as_str().unwrap()).unwrap();
        if status["last_error"] == "" && status["playback_state"] == "stopped" {
            assert_eq!(status["motion_active"], false);
            break;
        }
        assert!(
            Instant::now() < deadline,
            "stopped startup apply did not recover"
        );
        thread::sleep(Duration::from_millis(25));
    }
    assert!(fs::read_to_string(&events)
        .unwrap()
        .contains("msg wallpaper-set /tmp/one.png"));
    assert!(
        !launches.exists(),
        "stop must not start the video renderer while recovering the paired still"
    );
    let events_after_apply = fs::read_to_string(&events).unwrap();
    thread::sleep(Duration::from_millis(700));
    assert_eq!(fs::read_to_string(&events).unwrap(), events_after_apply);
    assert!(!launches.exists());

    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn initial_all_output_scene_waits_for_niri_without_retrying_a_crashed_renderer() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("initial-niri-readiness");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let ready = root.join("ready");
    let launches = root.join("launches");
    let noctalia = root.join("noctalia");
    let niri = root.join("niri");
    let engine = root.join("linux-wallpaperengine");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::write(
        &niri,
        format!(
            "#!/bin/sh\nif [ ! -f {:?} ]; then printf '%s\\n' 'niri IPC is not ready yet' >&2; exit 75; fi\nprintf '%s\\n' '{{\"eDP-1\":{{\"name\":\"eDP-1\"}}}}'\n",
            ready
        ),
    )
    .unwrap();
    fs::write(
        &engine,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nprintf '%s\\n' 'scene renderer crashed once' >&2\nexit 42\n",
            launches
        ),
    )
    .unwrap();
    for executable in [&noctalia, &niri, &engine] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, &noctalia, true)
        .replace("default_playlist = \"day\"", "default_playlist = \"night\"")
        .replace(
            "niri_program = \"/bin/true\"",
            &format!("niri_program = {niri:?}"),
        )
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!("linux_wallpaperengine_program = {engine:?}"),
        );
    fs::write(&config_path, document).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .spawn()
        .unwrap();

    let failed = request(&socket, "status", None);
    let failed: serde_json::Value =
        serde_json::from_str(failed["message"].as_str().unwrap()).unwrap();
    assert!(failed["last_error"]
        .as_str()
        .unwrap()
        .contains("niri IPC is not ready yet"));

    fs::write(&ready, "ready\n").unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if launches.exists() {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "scene was not launched after niri recovered"
        );
        thread::sleep(Duration::from_millis(50));
    }
    let failure_deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let status = request(&socket, "status", None);
        let status: serde_json::Value =
            serde_json::from_str(status["message"].as_str().unwrap()).unwrap();
        if status["last_error"]
            .as_str()
            .unwrap()
            .contains("scene renderer crashed once")
        {
            break;
        }
        assert!(
            Instant::now() < failure_deadline,
            "renderer crash was not surfaced in status"
        );
        thread::sleep(Duration::from_millis(25));
    }
    thread::sleep(Duration::from_millis(500));
    assert_eq!(
        fs::read_to_string(&launches).unwrap().lines().count(),
        1,
        "a renderer that launched and crashed must never enter the readiness retry loop"
    );

    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn successful_reload_apply_supersedes_the_pending_startup_retry() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("reload-cancels-readiness");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let failed = root.join("not-ready");
    let working = root.join("working");
    let events = root.join("events");
    fs::write(
        &failed,
        "#!/bin/sh\nprintf '%s\\n' 'desktop unavailable' >&2\nexit 75\n",
    )
    .unwrap();
    fs::write(
        &working,
        format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\n", events),
    )
    .unwrap();
    for executable in [&failed, &working] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    fs::write(&config_path, config(&failed, &failed, false)).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .spawn()
        .unwrap();
    assert_eq!(request(&socket, "status", None)["ok"], true);

    fs::write(&config_path, config(&working, &working, false)).unwrap();
    assert_eq!(request(&socket, "reload", None)["ok"], true);
    let after_reload = fs::read_to_string(&events).unwrap();
    thread::sleep(Duration::from_millis(700));
    assert_eq!(
        fs::read_to_string(&events).unwrap(),
        after_reload,
        "the obsolete startup retry reapplied after an authoritative reload"
    );

    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn reload_without_an_apply_keeps_the_startup_readiness_retry() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("quiet-reload-keeps-readiness");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let ready = root.join("ready");
    let events = root.join("events");
    let helper = root.join("helper");
    fs::write(
        &helper,
        format!(
            "#!/bin/sh\nif [ ! -f {:?} ]; then printf '%s\\n' 'desktop unavailable' >&2; exit 75; fi\nprintf '%s\\n' \"$*\" >> {:?}\n",
            ready, events
        ),
    )
    .unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();
    let original = config(&helper, &helper, false);
    fs::write(&config_path, &original).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .spawn()
        .unwrap();
    assert_eq!(request(&socket, "status", None)["ok"], true);

    fs::write(
        &config_path,
        original.replace(
            "cycle_interval_seconds = 300",
            "cycle_interval_seconds = 301",
        ),
    )
    .unwrap();
    assert_eq!(request(&socket, "reload", None)["ok"], true);
    fs::write(&ready, "ready\n").unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    while !events.exists() {
        assert!(
            Instant::now() < deadline,
            "non-applying reload incorrectly canceled readiness recovery"
        );
        thread::sleep(Duration::from_millis(25));
    }

    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn quit_during_a_pending_readiness_attempt_never_applies_again() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("quit-cancels-readiness");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let attempts = root.join("attempts");
    let helper = root.join("helper");
    fs::write(
        &helper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' attempt >> {0:?}\ncount=$(wc -l < {0:?})\nif [ \"$count\" -ge 2 ]; then sleep 1; fi\nprintf '%s\\n' 'desktop unavailable' >&2\nexit 75\n",
            attempts
        ),
    )
    .unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(&config_path, config(&helper, &helper, false)).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .spawn()
        .unwrap();
    assert_eq!(request(&socket, "status", None)["ok"], true);
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let count = fs::read_to_string(&attempts)
            .map(|contents| contents.lines().count())
            .unwrap_or(0);
        if count >= 2 {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "second readiness attempt did not start"
        );
        thread::sleep(Duration::from_millis(20));
    }

    assert_eq!(request(&socket, "quit", None)["ok"], true);
    let deadline = Instant::now() + Duration::from_secs(3);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        assert!(Instant::now() < deadline, "service did not exit after quit");
        thread::sleep(Duration::from_millis(20));
    };
    assert!(status.success());
    assert_eq!(
        fs::read_to_string(&attempts).unwrap().lines().count(),
        2,
        "service retried an apply after acknowledging quit"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn exhausted_startup_readiness_is_fatal_for_systemd_recovery() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Stdio;

    let root = directory("readiness-deadline");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let helper = root.join("helper");
    let stderr_path = root.join("stderr");
    fs::write(
        &helper,
        "#!/bin/sh\nprintf '%s\\n' 'desktop stayed unavailable' >&2\nexit 75\n",
    )
    .unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(&config_path, config(&helper, &helper, false)).unwrap();
    let stderr = fs::File::create(&stderr_path).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .stderr(Stdio::from(stderr))
        .spawn()
        .unwrap();
    assert_eq!(request(&socket, "status", None)["ok"], true);

    let deadline = Instant::now() + Duration::from_secs(11);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        assert!(
            Instant::now() < deadline,
            "service did not fail after its bounded readiness window"
        );
        thread::sleep(Duration::from_millis(25));
    };
    assert!(!status.success());
    let stderr = fs::read_to_string(&stderr_path).unwrap();
    assert!(stderr.contains("readiness deadline expired after 8 seconds"));
    assert!(stderr.contains("desktop stayed unavailable"));
    assert!(!socket.exists());
    fs::remove_dir_all(root).unwrap();
}
