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
        r#"schema_version = 4
config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
default_playlist = "day"
[settings]
cycle_interval_seconds = 300
cycle_enabled = false
shuffle = false
dynamics_enabled = true
display_mode = "mirrored"
theme_source_connector = ""
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

fn independent_config() -> String {
    format!(
        "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"day\"\n\
         [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"day\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
            .replace(
                "display_mode = \"mirrored\"",
                "display_mode = \"independent\""
            )
            .replace(
                "theme_source_connector = \"\"",
                "theme_source_connector = \"DP-1\""
            )
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
fn compositor_output_inventory_over_the_route_ceiling_is_rejected_not_truncated() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("too-many-outputs");
    let niri = root.join("niri");
    let outputs = serde_json::Value::Object(
        (0..65)
            .map(|index| {
                (
                    format!("DP-{index}"),
                    serde_json::json!({"name": format!("DP-{index}")}),
                )
            })
            .collect(),
    );
    fs::write(&niri, format!("#!/bin/sh\nprintf '%s\\n' '{}'\n", outputs)).unwrap();
    fs::set_permissions(&niri, fs::Permissions::from_mode(0o755)).unwrap();
    let document = config(Path::new("/bin/true"), Path::new("/bin/true"), false).replace(
        "niri_program = \"/bin/true\"",
        &format!("niri_program = {niri:?}"),
    );
    let parsed: Config = toml::from_str(&document).unwrap();
    let mut driver = SystemDriver::new(parsed.renderer);
    let error = driver.connected_outputs().unwrap_err();
    assert!(
        error.contains("more than the supported 64 outputs"),
        "{error}"
    );
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
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, &document).unwrap();
    let mut runtime = Runtime::new(
        config_path.clone(),
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
    let crash_status: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    assert_eq!(
        crash_status["config_generation"],
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    );
    assert_eq!(
        crash_status["config_path"],
        root.join("runtime.toml").to_str().unwrap()
    );
    assert_eq!(crash_status["motion_active"], false);
    assert!(crash_status["last_error"]
        .as_str()
        .unwrap()
        .contains("scene 12345"));
    assert!(crash_status["last_error"]
        .as_str()
        .unwrap()
        .contains("linux-wallpaperengine"));
    assert!(crash_status["last_error"]
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

    let suppressed = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "playlist-use".into(),
            argument: Some("night".into()),
        },
        at,
    );
    assert!(suppressed.ok);
    assert_eq!(fs::read_to_string(&launches).unwrap().lines().count(), 1);

    let durable = document.replace(
        "scene_id = \"12345\"",
        "scene_id = \"12345\"\ntaboo = { reason = \"known scene crash\", source = \"renderer-crash\" }",
    );
    fs::write(&config_path, durable).unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    assert_eq!(
        status(&mut runtime, at)["taboo_entries"][0]["durable"],
        true
    );
    assert_eq!(fs::read_to_string(&launches).unwrap().lines().count(), 1);

    fs::write(&config_path, &document).unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    assert!(status(&mut runtime, at)["taboo_entries"]
        .as_array()
        .unwrap()
        .is_empty());
    assert_eq!(
        fs::read_to_string(&launches).unwrap().lines().count(),
        1,
        "clearing compiler-owned taboo metadata must wait for the explicit retry"
    );
    let retried = runtime_command(&mut runtime, at, "playlist-use", Some("night"));
    assert!(retried.ok);
    let deadline = Instant::now() + Duration::from_secs(1);
    while fs::read_to_string(&launches).unwrap().lines().count() < 2 {
        assert!(
            Instant::now() < deadline,
            "explicit scene retry did not launch"
        );
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(fs::read_to_string(&launches).unwrap().lines().count(), 2);
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
    fail_stage_entry_id: Option<String>,
    fail_stage_output: Option<String>,
    fail_stage_attempts_remaining: usize,
    fail_palette_attempts_remaining: usize,
    fail_pause: bool,
    failures: Vec<String>,
    renderer_failures: Vec<RendererFailure>,
    connected_outputs: Option<Vec<String>>,
    output_probes: usize,
    video_audio: Vec<(bool, u8)>,
    reconfigures: usize,
    stage_events: Vec<String>,
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

    fn stop_output_renderer(&mut self, output: &str) {
        let mut state = self.0.lock().unwrap();
        state.stage_events.push(format!("stop {output}"));
        state.active_outputs.remove(output);
        state.motion_active = !state.active_outputs.is_empty();
    }

    fn apply_still_only(
        &mut self,
        entry: &wall_in_one_service::config::Entry,
        output: &str,
        settings: &wall_in_one_service::config::Settings,
    ) -> Result<(), String> {
        {
            let mut state = self.0.lock().unwrap();
            state
                .stage_events
                .push(format!("still {output} {}", entry.id));
            if state.fail_stage_entry_id.as_deref() == Some(&entry.id)
                && state.fail_stage_output.as_deref() == Some(output)
                && state.fail_stage_attempts_remaining > 0
            {
                state.fail_stage_attempts_remaining -= 1;
                return Err(format!("entry {} is borked on {output}", entry.id));
            }
        }
        let mut still = settings.clone();
        still.dynamics_enabled = false;
        self.apply(entry, output, &still)
    }

    fn apply_palette_only(
        &mut self,
        entry: &wall_in_one_service::config::Entry,
    ) -> Result<(), String> {
        let mut state = self.0.lock().unwrap();
        state.stage_events.push(format!("palette {}", entry.id));
        if state.fail_palette_attempts_remaining > 0 {
            state.fail_palette_attempts_remaining -= 1;
            Err("palette application failed".into())
        } else {
            Ok(())
        }
    }

    fn start_motion_only(
        &mut self,
        entry: &wall_in_one_service::config::Entry,
        output: &str,
        settings: &wall_in_one_service::config::Settings,
    ) -> Result<(), String> {
        self.0
            .lock()
            .unwrap()
            .stage_events
            .push(format!("motion {output} {}", entry.id));
        if settings.dynamics_enabled && entry.kind != wall_in_one_service::config::EntryKind::Still
        {
            self.apply(entry, output, settings)?;
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

    fn set_output_paused(&mut self, output: &str, paused: bool) -> Result<(), String> {
        self.0
            .lock()
            .unwrap()
            .stage_events
            .push(format!("pause {output} {paused}"));
        self.set_paused(paused)
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
        for failure in &structured {
            state.active_outputs.remove(&failure.output);
        }
        state.motion_active = !state.active_outputs.is_empty();
        structured
    }

    fn stop(&mut self) {
        let mut state = self.0.lock().unwrap();
        state.stops += 1;
        state.motion_active = false;
        state.active_outputs.clear();
    }
}

fn status<D: WallpaperDriver>(
    runtime: &mut Runtime<D>,
    at: chrono::NaiveDateTime,
) -> serde_json::Value {
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

fn display_status<D: WallpaperDriver>(
    runtime: &mut Runtime<D>,
    at: chrono::NaiveDateTime,
    connector: &str,
) -> serde_json::Value {
    status(runtime, at)["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == connector)
        .unwrap()
        .clone()
}

fn runtime_command<D: WallpaperDriver>(
    runtime: &mut Runtime<D>,
    at: chrono::NaiveDateTime,
    verb: &str,
    argument: Option<&str>,
) -> Response {
    runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: verb.into(),
            argument: argument.map(str::to_owned),
        },
        at,
    )
}

#[test]
fn independent_routes_keep_cursor_and_manual_state_per_connector() {
    let parsed: Config = toml::from_str(&independent_config()).unwrap();
    parsed.validate().unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["HDMI-A-1".into(), "DP-1".into()]),
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

    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 next")).ok);
    let snapshot = status(&mut runtime, at);
    let dp = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "DP-1")
        .unwrap();
    let hdmi = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "HDMI-A-1")
        .unwrap();
    assert_eq!(dp["entry_id"], "video-two");
    assert_eq!(hdmi["entry_id"], "still-one");
    assert_eq!(snapshot["source"], "schedule");

    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 playlist-use Night")).ok);
    let snapshot = status(&mut runtime, at);
    let dp = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "DP-1")
        .unwrap();
    let hdmi = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "HDMI-A-1")
        .unwrap();
    assert_eq!(dp["playlist_id"], "night");
    assert_eq!(dp["route_source"], "manual");
    assert_eq!(hdmi["playlist_id"], "day");
    assert_eq!(snapshot["source"], "mixed");
}

#[test]
fn independent_schedule_precedence_is_targeted_then_global_then_assignment() {
    let document = format!(
        "{}\n[[schedules]]\nid = \"global-now\"\nplaylist = \"night\"\n\
         [[schedules]]\nid = \"dp-now\"\nplaylist = \"day\"\nconnector = \"DP-1\"\n",
        independent_config()
    );
    let parsed: Config = toml::from_str(&document).unwrap();
    parsed.validate().unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let snapshot = status(&mut runtime, at);
    let dp = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "DP-1")
        .unwrap();
    let hdmi = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "HDMI-A-1")
        .unwrap();
    assert_eq!(dp["playlist_id"], "day");
    assert_eq!(dp["schedule_rule_id"], "dp-now");
    assert_eq!(hdmi["playlist_id"], "night");
    assert_eq!(hdmi["schedule_rule_id"], "global-now");
    assert!(snapshot["schedules"]
        .as_array()
        .unwrap()
        .iter()
        .any(|rule| rule["id"] == "global-now" && rule["connector"].is_null()));
    assert!(snapshot["schedules"]
        .as_array()
        .unwrap()
        .iter()
        .any(|rule| rule["id"] == "dp-now" && rule["connector"] == "DP-1"));

    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 playlist-use night")).ok);
    assert_eq!(
        status(&mut runtime, at)["displays"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["connector"] == "DP-1")
            .unwrap()["route_source"],
        "manual"
    );
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 schedule-follow")).ok);
    let dp = status(&mut runtime, at)["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "DP-1")
        .unwrap()
        .clone();
    assert_eq!(dp["playlist_id"], "day");
    assert_eq!(dp["route_source"], "schedule");
}

#[test]
fn independent_apply_stages_stills_theme_palette_then_renderers() {
    let parsed: Config = toml::from_str(&independent_config()).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
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
    let events = state.lock().unwrap().stage_events.clone();
    let still_hdmi = events
        .iter()
        .position(|event| event.starts_with("still HDMI-A-1"))
        .unwrap();
    let still_dp = events
        .iter()
        .position(|event| event.starts_with("still DP-1"))
        .unwrap();
    let palette = events
        .iter()
        .position(|event| event.starts_with("palette "))
        .unwrap();
    let first_motion = events
        .iter()
        .position(|event| event.starts_with("motion "))
        .unwrap();
    assert!(
        still_hdmi < still_dp,
        "designated DP-1 still must land last: {events:?}"
    );
    assert!(still_dp < palette && palette < first_motion, "{events:?}");
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["status_version"], 2);
    assert_eq!(snapshot["display_mode"], "independent");
    assert_eq!(snapshot["theme_source"]["configured"], "DP-1");
    assert_eq!(snapshot["theme_source"]["effective"], "DP-1");
    assert_eq!(snapshot["theme_source"]["fallback"], false);
}

#[test]
fn targeted_handover_never_restarts_or_recolours_an_unselected_display_and_rolls_back() {
    let parsed: Config = toml::from_str(&independent_config()).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
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
    state.lock().unwrap().stage_events.clear();

    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 next")).ok);
    let events = state.lock().unwrap().stage_events.clone();
    assert!(events.iter().any(|event| event == "stop HDMI-A-1"));
    assert!(events
        .iter()
        .any(|event| event == "still HDMI-A-1 video-two"));
    assert!(events
        .iter()
        .any(|event| event == "motion HDMI-A-1 video-two"));
    assert!(
        events
            .iter()
            .all(|event| !event.contains("DP-1") && !event.starts_with("palette ")),
        "a targeted non-theme hand-over touched the designated display: {events:?}"
    );

    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 previous")).ok);
    {
        let mut recorded = state.lock().unwrap();
        recorded.stage_events.clear();
        recorded.fail_stage_entry_id = Some("video-two".into());
        recorded.fail_stage_output = Some("HDMI-A-1".into());
        recorded.fail_stage_attempts_remaining = 1;
    }
    let failed = runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 next"));
    assert!(!failed.ok);
    assert!(failed.message.contains("previous wallpaper restored"));
    let snapshot = status(&mut runtime, at);
    let hdmi = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "HDMI-A-1")
        .unwrap();
    let dp = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "DP-1")
        .unwrap();
    assert_eq!(hdmi["entry_id"], "still-one");
    assert_eq!(hdmi["renderer_failed"], false);
    assert!(hdmi["last_error"]
        .as_str()
        .unwrap()
        .contains("previous wallpaper restored"));
    assert_eq!(dp["entry_id"], "still-one");
    assert_eq!(dp["last_error"], "");
    let events = state.lock().unwrap().stage_events.clone();
    assert!(
        events
            .iter()
            .all(|event| !event.contains("DP-1") && !event.starts_with("palette ")),
        "candidate or rollback touched the unselected route: {events:?}"
    );
    assert!(
        !events
            .iter()
            .any(|event| event == "motion HDMI-A-1 video-two"),
        "motion leaked after its still failed: {events:?}"
    );

    {
        let mut recorded = state.lock().unwrap();
        recorded.stage_events.clear();
        recorded.fail_palette_attempts_remaining = 1;
    }
    let failed = runtime_command(&mut runtime, at, "on", Some("DP-1 next"));
    assert!(!failed.ok);
    let events = state.lock().unwrap().stage_events.clone();
    assert!(events.iter().any(|event| event == "palette video-two"));
    assert!(
        !events.iter().any(|event| event == "motion DP-1 video-two"),
        "motion leaked after its designated palette failed: {events:?}"
    );
    let snapshot = status(&mut runtime, at);
    assert_eq!(
        snapshot["displays"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["connector"] == "DP-1")
            .unwrap()["entry_id"],
        "still-one"
    );
}

#[test]
fn independent_theme_source_falls_back_stably_without_forgetting_configuration() {
    let parsed: Config = toml::from_str(&independent_config().replace(
        "theme_source_connector = \"DP-1\"",
        "theme_source_connector = \"eDP-1\"",
    ))
    .unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["HDMI-A-1".into(), "DP-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["theme_source"]["configured"], "eDP-1");
    assert_eq!(snapshot["theme_source"]["effective"], "DP-1");
    assert_eq!(snapshot["theme_source"]["fallback"], true);
}

#[test]
fn independent_automatic_failure_retries_and_marks_only_the_borked_route_candidate() {
    let parsed: Config = toml::from_str(
        &independent_config().replace("cycle_enabled = false", "cycle_enabled = true"),
    )
    .unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
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
        recorded.fail_stage_entry_id = Some("video-two".into());
        recorded.fail_stage_output = Some("DP-1".into());
        recorded.fail_stage_attempts_remaining = 3;
    }

    let base = Instant::now();
    runtime.tick(at, base + Duration::from_secs(301));
    let first = status(&mut runtime, at);
    let dp = first["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "DP-1")
        .unwrap();
    let hdmi = first["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "HDMI-A-1")
        .unwrap();
    assert_eq!(dp["entry_id"], "still-one");
    assert_eq!(dp["automatic_retry"]["attempt"], 1);
    assert_eq!(dp["automatic_retry"]["maximum_attempts"], 3);
    assert_eq!(hdmi["entry_id"], "video-two");
    assert!(hdmi["automatic_retry"].is_null());
    assert_eq!(hdmi["last_error"], "");

    let rejected = runtime_command(&mut runtime, at, "on", Some("DP-1 cycle maybe"));
    assert!(!rejected.ok);
    assert_eq!(
        status(&mut runtime, at)["displays"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["connector"] == "DP-1")
            .unwrap()["automatic_retry"]["attempt"],
        1,
        "a rejected targeted command canceled the pending retry"
    );

    runtime.tick(at, base + Duration::from_secs(304));
    assert_eq!(
        status(&mut runtime, at)["displays"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["connector"] == "DP-1")
            .unwrap()["automatic_retry"]["attempt"],
        2
    );
    runtime.tick(at, base + Duration::from_secs(307));
    let exhausted = status(&mut runtime, at);
    let dp = exhausted["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "DP-1")
        .unwrap();
    let hdmi = exhausted["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "HDMI-A-1")
        .unwrap();
    assert_eq!(dp["entry_id"], "still-one");
    assert!(dp["automatic_retry"].is_null());
    assert!(dp["last_error"].as_str().unwrap().contains("marked taboo"));
    assert_eq!(hdmi["entry_id"], "video-two");
    assert_eq!(hdmi["last_error"], "");
    assert_eq!(exhausted["taboo_entries"][0]["entry_id"], "video-two");
    assert_eq!(exhausted["taboo_entries"][0]["durable"], false);
}

#[test]
fn one_route_retry_never_rewinds_or_reapplies_another_routes_random_choice() {
    let document = independent_config()
        .replace("cycle_enabled = false", "cycle_enabled = true")
        .replace(
            "connector = \"HDMI-A-1\"\nplaylist = \"day\"",
            "connector = \"HDMI-A-1\"\nplaylist = \"night\"",
        );
    let parsed: Config = toml::from_str(&document).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
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
        recorded.fail_stage_entry_id = Some("video-two".into());
        recorded.fail_stage_output = Some("DP-1".into());
        recorded.fail_stage_attempts_remaining = 3;
    }
    let base = Instant::now();
    runtime.tick(at, base + Duration::from_secs(301));
    assert_eq!(
        display_status(&mut runtime, at, "DP-1")["automatic_retry"]["attempt"],
        1
    );

    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 playlist-use day")).ok);
    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 random")).ok);
    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 cycle off")).ok);
    let chosen = display_status(&mut runtime, at, "HDMI-A-1")["entry_id"]
        .as_str()
        .unwrap()
        .to_string();
    state.lock().unwrap().stage_events.clear();
    runtime.tick(at, base + Duration::from_secs(304));
    assert_eq!(
        display_status(&mut runtime, at, "HDMI-A-1")["entry_id"],
        chosen
    );
    assert!(state
        .lock()
        .unwrap()
        .stage_events
        .iter()
        .all(|event| !event.contains("HDMI-A-1")));
}

#[test]
fn disconnected_pending_route_is_canceled_and_rejoins_current_schedule() {
    let parsed: Config = toml::from_str(
        &independent_config().replace("cycle_enabled = false", "cycle_enabled = true"),
    )
    .unwrap();
    let summer = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let winter = NaiveDate::from_ymd_opt(2026, 12, 3)
        .unwrap()
        .and_hms_opt(23, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        summer,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    {
        let mut recorded = state.lock().unwrap();
        recorded.fail_stage_entry_id = Some("video-two".into());
        recorded.fail_stage_output = Some("DP-1".into());
        recorded.fail_stage_attempts_remaining = 3;
    }
    let base = Instant::now();
    runtime.tick(summer, base + Duration::from_secs(301));
    assert_eq!(
        display_status(&mut runtime, summer, "DP-1")["automatic_retry"]["attempt"],
        1
    );

    state.lock().unwrap().connected_outputs = Some(vec!["HDMI-A-1".into()]);
    runtime.tick(winter, base + Duration::from_secs(307));
    let detached = display_status(&mut runtime, winter, "DP-1");
    assert_eq!(detached["connected"], false);
    assert!(detached["automatic_retry"].is_null());

    state.lock().unwrap().connected_outputs = Some(vec!["DP-1".into(), "HDMI-A-1".into()]);
    runtime.tick(winter, base + Duration::from_secs(313));
    let reconnected = display_status(&mut runtime, winter, "DP-1");
    assert_eq!(reconnected["connected"], true);
    assert_eq!(reconnected["playlist_id"], "night");
    assert_eq!(reconnected["schedule_rule_id"], "night-rule");
    assert!(reconnected["automatic_retry"].is_null());
}

#[test]
fn display_mode_switch_clears_hidden_session_overrides_in_both_directions() {
    let root = directory("display-mode-switch");
    let config_path = root.join("runtime.toml");
    let mirrored_document = config(Path::new("/bin/true"), Path::new("/bin/true"), false);
    let independent_document = independent_config();
    fs::write(&config_path, &mirrored_document).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        config_path.clone(),
        Config::load(&config_path).unwrap(),
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "playlist-use", Some("night")).ok);
    assert_eq!(status(&mut runtime, at)["source"], "manual");

    fs::write(&config_path, &independent_document).unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    let independent = status(&mut runtime, at);
    assert_ne!(independent["source"], "manual");
    assert!(independent["displays"]
        .as_array()
        .unwrap()
        .iter()
        .all(|row| row["manual_override"] == false));
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 playlist-use night")).ok);
    assert_eq!(
        display_status(&mut runtime, at, "DP-1")["route_source"],
        "manual"
    );

    fs::write(&config_path, &mirrored_document).unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    let mirrored = status(&mut runtime, at);
    assert_eq!(mirrored["source"], "schedule");
    assert_eq!(mirrored["playlist_id"], "day");

    fs::write(&config_path, &independent_document).unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    let independent_again = status(&mut runtime, at);
    assert!(independent_again["displays"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["connected"] == true)
        .all(|row| row["manual_override"] == false));
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn independent_renderer_crash_is_attributed_without_poisoning_a_healthy_route() {
    let parsed: Config = toml::from_str(&independent_config()).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
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
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    {
        let mut recorded = state.lock().unwrap();
        recorded.stage_events.clear();
        recorded.renderer_failures.push(RendererFailure {
            entry_id: "video-two".into(),
            kind: wall_in_one_service::config::EntryKind::Video,
            scene_id: None,
            output: "HDMI-A-1".into(),
            message: "mpvpaper crashed on HDMI-A-1; paired still restored".into(),
            permanent_for_session: false,
        });
    }
    runtime.tick(at, Instant::now());
    let snapshot = status(&mut runtime, at);
    let dp = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "DP-1")
        .unwrap();
    let hdmi = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "HDMI-A-1")
        .unwrap();
    assert_eq!(dp["playback_state"], "playing");
    assert_eq!(dp["motion_active"], true);
    assert_eq!(dp["renderer_failed"], false);
    assert_eq!(dp["last_error"], "");
    assert_eq!(hdmi["playback_state"], "stopped");
    assert_eq!(hdmi["motion_active"], false);
    assert_eq!(hdmi["renderer_failed"], true);
    assert!(hdmi["last_error"]
        .as_str()
        .unwrap()
        .contains("mpvpaper crashed"));

    state.lock().unwrap().stage_events.clear();
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 play")).ok);
    assert!(
        state.lock().unwrap().stage_events.is_empty(),
        "Play on the healthy route must not reapply either wallpaper"
    );
}

#[test]
fn renderer_crash_reasserts_only_the_shell_global_palette_owner() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();

    let independent_state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut independent = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        toml::from_str::<Config>(&independent_config()).unwrap(),
        RuntimeDriver(independent_state.clone()),
        at,
    )
    .unwrap();
    independent.apply_current().unwrap();
    independent_state.lock().unwrap().stage_events.clear();
    independent_state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
            entry_id: "still-one".into(),
            kind: wall_in_one_service::config::EntryKind::Still,
            scene_id: None,
            output: "HDMI-A-1".into(),
            message: "non-theme renderer exited".into(),
            permanent_for_session: false,
        });
    independent.tick(at, Instant::now());
    assert!(independent_state
        .lock()
        .unwrap()
        .stage_events
        .iter()
        .all(|event| !event.starts_with("palette ")));

    independent_state.lock().unwrap().stage_events.clear();
    independent_state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
            entry_id: "still-one".into(),
            kind: wall_in_one_service::config::EntryKind::Still,
            scene_id: None,
            output: "DP-1".into(),
            message: "theme renderer exited".into(),
            permanent_for_session: false,
        });
    independent.tick(at, Instant::now());
    assert_eq!(
        independent_state
            .lock()
            .unwrap()
            .stage_events
            .iter()
            .filter(|event| event.starts_with("palette "))
            .count(),
        1
    );

    let mirrored_state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut mirrored = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        toml::from_str::<Config>(&config(
            Path::new("/bin/true"),
            Path::new("/bin/true"),
            false,
        ))
        .unwrap(),
        RuntimeDriver(mirrored_state.clone()),
        at,
    )
    .unwrap();
    mirrored.apply_current().unwrap();
    mirrored_state.lock().unwrap().stage_events.clear();
    mirrored_state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
            entry_id: "still-one".into(),
            kind: wall_in_one_service::config::EntryKind::Still,
            scene_id: None,
            output: String::new(),
            message: "mirrored renderer exited".into(),
            permanent_for_session: false,
        });
    mirrored.tick(at, Instant::now());
    assert_eq!(
        mirrored_state
            .lock()
            .unwrap()
            .stage_events
            .iter()
            .filter(|event| event.starts_with("palette "))
            .count(),
        1
    );
}

#[test]
fn targeted_command_applies_a_connector_first_seen_by_its_output_probe_once() {
    let parsed: Config = toml::from_str(&independent_config()).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into()]),
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
        recorded.connected_outputs = Some(vec!["DP-1".into(), "HDMI-A-1".into()]);
        recorded.stage_events.clear();
    }

    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 next")).ok);
    let events = state.lock().unwrap().stage_events.clone();
    assert!(
        events
            .iter()
            .any(|event| event == "still HDMI-A-1 still-one"),
        "the newly connected route was discovered but left unapplied: {events:?}"
    );
    assert!(events.iter().any(|event| event == "stop HDMI-A-1"));

    state.lock().unwrap().stage_events.clear();
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 next")).ok);
    let events = state.lock().unwrap().stage_events.clone();
    assert!(
        events.iter().all(|event| !event.contains("HDMI-A-1")),
        "a connector already known from the prior probe was reapplied: {events:?}"
    );
}

#[test]
fn broken_hotplug_route_cannot_fail_or_rollback_an_explicit_target_command() {
    let parsed: Config = toml::from_str(&independent_config()).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into()]),
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
        recorded.connected_outputs = Some(vec!["DP-1".into(), "HDMI-A-1".into()]);
        recorded.fail_stage_entry_id = Some("still-one".into());
        recorded.fail_stage_output = Some("HDMI-A-1".into());
        recorded.fail_stage_attempts_remaining = 3;
        recorded.stage_events.clear();
    }

    let command = runtime_command(&mut runtime, at, "on", Some("DP-1 next"));
    assert!(command.ok, "{}", command.message);
    let dp = display_status(&mut runtime, at, "DP-1");
    let hdmi = display_status(&mut runtime, at, "HDMI-A-1");
    assert_eq!(dp["entry_id"], "video-two");
    assert_eq!(dp["last_error"], "");
    assert_eq!(hdmi["entry_id"], "still-one");
    assert_eq!(hdmi["automatic_retry"]["attempt"], 1);
    assert!(hdmi["last_error"].as_str().unwrap().contains("borked"));
    assert_eq!(
        state
            .lock()
            .unwrap()
            .stage_events
            .iter()
            .filter(|event| event.as_str() == "stop DP-1")
            .count(),
        1,
        "the explicit route was rolled back or applied twice"
    );
}

#[test]
fn status_inventory_keeps_configured_assignments_when_the_connector_is_detached() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    for document in [
        independent_config(),
        format!(
            "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"day\"\n\
             [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"night\"\n",
            config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        ),
    ] {
        let parsed: Config = toml::from_str(&document).unwrap();
        let state = Arc::new(Mutex::new(RuntimeDriverState {
            connected_outputs: Some(vec!["DP-1".into()]),
            ..RuntimeDriverState::default()
        }));
        let mut runtime = Runtime::new(
            PathBuf::from("/tmp/runtime.toml"),
            parsed,
            RuntimeDriver(state),
            at,
        )
        .unwrap();
        runtime.apply_current().unwrap();
        let snapshot = status(&mut runtime, at);
        let detached = snapshot["displays"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["connector"] == "HDMI-A-1")
            .expect("configured detached assignment vanished from status");
        assert_eq!(detached["connected"], false);
        assert_eq!(detached["motion_active"], false);
        assert_eq!(detached["assignment_source"], "explicit");
        assert!(!detached["assigned_playlist_id"]
            .as_str()
            .unwrap()
            .is_empty());
    }
}

#[test]
fn global_independent_commands_apply_only_routes_that_change() {
    let document = independent_config().replace(
        "connector = \"HDMI-A-1\"\nplaylist = \"day\"",
        "connector = \"HDMI-A-1\"\nplaylist = \"night\"",
    );
    let parsed: Config = toml::from_str(&document).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
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

    state.lock().unwrap().stage_events.clear();
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    let events = state.lock().unwrap().stage_events.clone();
    assert!(events.iter().any(|event| event.contains("DP-1")));
    assert!(
        events.iter().all(|event| !event.contains("HDMI-A-1")),
        "singleton route was restarted by a global Next: {events:?}"
    );

    state.lock().unwrap().stage_events.clear();
    let following = runtime_command(&mut runtime, at, "schedule-follow", None);
    assert!(following.ok);
    assert!(following.message.contains("already following"));
    assert!(state.lock().unwrap().stage_events.is_empty());

    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 stop")).ok);
    state.lock().unwrap().stage_events.clear();
    assert!(runtime_command(&mut runtime, at, "stop", None).ok);
    let events = state.lock().unwrap().stage_events.clone();
    assert!(events.iter().any(|event| event.contains("DP-1")));
    assert!(
        events.iter().all(|event| !event.contains("HDMI-A-1")),
        "already-stopped route was reapplied by global Stop: {events:?}"
    );
    state.lock().unwrap().stage_events.clear();
    let stopped = runtime_command(&mut runtime, at, "stop", None);
    assert!(stopped.ok);
    assert!(stopped.message.contains("already stopped"));
    assert!(state.lock().unwrap().stage_events.is_empty());
}

#[test]
fn independent_target_grammar_is_strict_and_controls_are_per_display() {
    let parsed: Config = toml::from_str(&independent_config()).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();

    for command in [
        "missing next",
        " DP-1 next",
        "DP-1 next ",
        "DP-1\tnext",
        "DP-1 status",
        "DP-1 reload",
        "DP-1 quit",
        "DP-1 on HDMI-A-1 next",
        "DP-1 config",
        "DP-1 next junk",
        "DP-1 shuffle",
    ] {
        assert!(
            !runtime_command(&mut runtime, at, "on", Some(command)).ok,
            "accepted {command:?}"
        );
    }
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 pause")).ok);
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 shuffle on")).ok);
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 cycle off")).ok);
    let snapshot = status(&mut runtime, at);
    let dp = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "DP-1")
        .unwrap();
    let hdmi = snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "HDMI-A-1")
        .unwrap();
    assert_eq!(dp["playback_state"], "paused");
    assert_eq!(dp["shuffle_source"], "manual");
    assert_eq!(dp["cycle_source"], "manual");
    assert_eq!(hdmi["playback_state"], "playing");
    assert_eq!(hdmi["shuffle_source"], "config");
    assert_eq!(hdmi["cycle_source"], "config");
    assert_eq!(snapshot["playback_state"], "mixed");

    assert!(runtime_command(&mut runtime, at, "toggle", None).ok);
    assert!(status(&mut runtime, at)["displays"]
        .as_array()
        .unwrap()
        .iter()
        .all(|row| row["playback_state"] == "paused"));
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 stop")).ok);
    let mixed = status(&mut runtime, at);
    assert!(mixed["displays"]
        .as_array()
        .unwrap()
        .iter()
        .any(|row| row["playback_state"] == "stopped"));
    assert!(mixed["displays"]
        .as_array()
        .unwrap()
        .iter()
        .any(|row| row["playback_state"] == "paused"));
    assert!(runtime_command(&mut runtime, at, "toggle", None).ok);
    assert!(status(&mut runtime, at)["displays"]
        .as_array()
        .unwrap()
        .iter()
        .all(|row| row["playback_state"] == "playing"));

    assert!(runtime_command(&mut runtime, at, "stop", None).ok);
    assert!(status(&mut runtime, at)["displays"]
        .as_array()
        .unwrap()
        .iter()
        .all(|row| row["playback_state"] == "stopped"));
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 play")).ok);
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["playback_state"], "mixed");
}

#[test]
fn mirrored_mode_rejects_targeted_on_without_changing_legacy_commands() {
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
    let rejected = runtime_command(&mut runtime, at, "on", Some("DP-1 next"));
    assert!(!rejected.ok);
    assert!(rejected.message.contains("independent display mode"));
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
}

#[test]
fn sixty_four_display_failures_and_detached_inventory_fit_the_status_wire() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some((0..64).map(|index| format!("LIVE-{index:02}")).collect()),
        renderer_failures: (0..64)
            .map(|index| RendererFailure {
                entry_id: "still-one".into(),
                kind: wall_in_one_service::config::EntryKind::Still,
                scene_id: None,
                output: format!("LIVE-{index:02}"),
                message: format!(
                    "display LIVE-{index:02} failed; stderr: {} TAIL-{index:02}\n",
                    "diagnostic".repeat(1200),
                ),
                permanent_for_session: false,
            })
            .collect(),
        ..RuntimeDriverState::default()
    }));
    let mut parsed: Config = toml::from_str(&independent_config()).unwrap();
    parsed.displays = (0..64)
        .map(|index| wall_in_one_service::config::DisplayAssignment {
            connector: format!("DETACHED-{index:02}"),
            playlist: "day".into(),
        })
        .collect();
    parsed.validate().unwrap();
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();

    runtime.apply_current().unwrap();
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
    assert!(error.contains("LIVE-00"), "{error}");
    assert!(error.contains("LIVE-63"), "{error}");
    assert!(error.contains("[truncated]"), "{error}");
    assert_eq!(snapshot["displays"].as_array().unwrap().len(), 128);
    assert_eq!(
        snapshot["schedules"][0]["connector"],
        serde_json::Value::Null
    );
    assert_eq!(
        snapshot["displays"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["connected"] == false)
            .count(),
        64
    );
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

    for (verb, argument) in [
        ("quit", Some("junk")),
        ("next", Some("junk")),
        ("shuffle", Some("maybe")),
        ("cycle", Some("maybe")),
        ("not-a-verb", None),
    ] {
        let rejected = runtime_command(&mut runtime, at, verb, argument);
        assert!(!rejected.ok, "{verb} {argument:?} unexpectedly succeeded");
        assert_eq!(
            status(&mut runtime, at)["automatic_retry"]["attempt"],
            1,
            "rejected {verb} {argument:?} canceled the pending retry"
        );
    }

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
    assert_eq!(taboo["taboo_entries"][0]["durable"], false);
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
fn mirrored_startup_failure_retries_three_times_then_quarantines_and_advances() {
    let mut parsed: Config = toml::from_str(&config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        false,
    ))
    .unwrap();
    parsed.schedules.clear();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        fail_entry_id: Some("still-one".into()),
        fail_entry_attempts_remaining: 3,
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    let started = Instant::now();
    assert!(runtime.apply_current().is_err());
    assert!(runtime.schedule_initial_apply_retry(started));
    runtime.tick(at, started + Duration::from_secs(2));
    runtime.tick(at, started + Duration::from_secs(4));
    let retrying = status(&mut runtime, at);
    assert_eq!(retrying["taboo_entries"][0]["entry_id"], "still-one");
    assert_eq!(retrying["automatic_retry"]["attempt"], 0);
    runtime.tick(at, started + Duration::from_secs(6));
    let recovered = status(&mut runtime, at);
    assert_eq!(recovered["entry_id"], "video-two");
    assert_eq!(recovered["motion_active"], true);
    assert_eq!(state.lock().unwrap().fail_entry_attempts_remaining, 0);
}

#[test]
fn independent_startup_failure_quarantines_only_the_failed_route_and_advances() {
    let parsed: Config = toml::from_str(&independent_config()).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        fail_stage_entry_id: Some("still-one".into()),
        fail_stage_output: Some("DP-1".into()),
        fail_stage_attempts_remaining: 3,
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    let started = Instant::now();
    assert!(runtime.apply_current().is_err());
    state.lock().unwrap().stage_events.clear();
    assert!(runtime.schedule_initial_apply_retry(started));
    assert!(display_status(&mut runtime, at, "HDMI-A-1")["automatic_retry"].is_null());
    runtime.tick(at, started + Duration::from_secs(2));
    runtime.tick(at, started + Duration::from_secs(4));
    let quarantined = status(&mut runtime, at);
    assert_eq!(quarantined["taboo_entries"][0]["entry_id"], "still-one");
    assert_eq!(quarantined["displays"][0]["automatic_retry"]["attempt"], 0);
    runtime.tick(at, started + Duration::from_secs(6));
    let recovered = status(&mut runtime, at);
    assert_eq!(recovered["displays"][0]["entry_id"], "video-two");
    assert_eq!(recovered["displays"][0]["motion_active"], true);
    assert!(state
        .lock()
        .unwrap()
        .stage_events
        .iter()
        .all(|event| !event.contains("HDMI-A-1")));
}

#[test]
fn failed_reload_preserves_mirrored_and_independent_automatic_retries() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();

    let mirrored_root = directory("mirrored-reload-retry");
    let mirrored_path = mirrored_root.join("runtime.toml");
    let mirrored_document = config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        .replace("cycle_interval_seconds = 300", "cycle_interval_seconds = 5")
        .replace("cycle_enabled = false", "cycle_enabled = true");
    fs::write(&mirrored_path, &mirrored_document).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut mirrored = Runtime::new(
        mirrored_path.clone(),
        Config::load(&mirrored_path).unwrap(),
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    mirrored.apply_current().unwrap();
    {
        let mut recorded = state.lock().unwrap();
        recorded.fail_entry_id = Some("video-two".into());
        recorded.fail_entry_attempts_remaining = 3;
    }
    let mirrored_due = Instant::now() + Duration::from_secs(600);
    mirrored.tick(at, mirrored_due);
    assert_eq!(status(&mut mirrored, at)["automatic_retry"]["attempt"], 1);
    fs::write(&mirrored_path, "not valid toml = [").unwrap();
    assert!(!runtime_command(&mut mirrored, at, "reload", None).ok);
    assert_eq!(status(&mut mirrored, at)["automatic_retry"]["attempt"], 1);
    fs::remove_dir_all(mirrored_root).unwrap();

    let independent_root = directory("independent-reload-retry");
    let independent_path = independent_root.join("runtime.toml");
    fs::write(
        &independent_path,
        independent_config().replace("cycle_enabled = false", "cycle_enabled = true"),
    )
    .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut independent = Runtime::new(
        independent_path.clone(),
        Config::load(&independent_path).unwrap(),
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    independent.apply_current().unwrap();
    {
        let mut recorded = state.lock().unwrap();
        recorded.fail_stage_entry_id = Some("video-two".into());
        recorded.fail_stage_output = Some("DP-1".into());
        recorded.fail_stage_attempts_remaining = 3;
    }
    let independent_due = Instant::now() + Duration::from_secs(600);
    independent.tick(at, independent_due);
    assert_eq!(
        display_status(&mut independent, at, "DP-1")["automatic_retry"]["attempt"],
        1
    );
    fs::write(&independent_path, "not valid toml = [").unwrap();
    assert!(!runtime_command(&mut independent, at, "reload", None).ok);
    assert_eq!(
        display_status(&mut independent, at, "DP-1")["automatic_retry"]["attempt"],
        1
    );
    fs::remove_dir_all(independent_root).unwrap();
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
    let first = status(&mut runtime, at);
    assert_eq!(first["config_epoch"], 1);
    assert_eq!(first["taboo_entries"][0]["entry_id"], "video-two");
    assert_eq!(first["taboo_entries"][0]["observed_config_epoch"], 1);
    let instance = first["runtime_instance"].as_str().unwrap();
    assert_eq!(instance.len(), 32);
    assert!(instance
        .bytes()
        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)));

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
    let after_reload = status(&mut runtime, at);
    assert_eq!(after_reload["runtime_instance"], instance);
    assert_eq!(after_reload["config_epoch"], 2);
    assert_eq!(after_reload["taboo_entries"][0]["entry_id"], "video-two");
    assert_eq!(
        after_reload["taboo_entries"][0]["observed_config_epoch"], 1,
        "a retained session finding must not be relabelled as the reloaded config"
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
            message: "video-two crashed again under the new config epoch".into(),
            permanent_for_session: true,
        });
    runtime.tick(at, Instant::now());
    assert_eq!(
        status(&mut runtime, at)["taboo_entries"][0]["observed_config_epoch"],
        2,
        "a new failure must be attributable to the current config epoch"
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
        status(&mut runtime, at)["taboo_entries"][0]["durable"],
        true
    );
    assert_eq!(
        status(&mut runtime, at)["taboo_entries"][0]["observed_config_epoch"],
        3
    );
    let applies_before_clear = state.lock().unwrap().applies.len();
    assert_eq!(status(&mut runtime, at)["motion_active"], false);

    // The app is the sole writer. Removing metadata makes one explicit retry
    // possible, but the metadata-only reload itself does not restart motion.
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
    assert_eq!(state.lock().unwrap().applies.len(), applies_before_clear);
    assert!(runtime_command(&mut runtime, at, "play", None).ok);
    assert_eq!(
        state.lock().unwrap().applies.last().unwrap(),
        &("video-two".into(), true)
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn taboo_propagates_across_equivalent_resolved_scene_and_video_occurrences() {
    let mut parsed: Config = toml::from_str(&config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        false,
    ))
    .unwrap();
    parsed.schedules.clear();
    let mut scene_copy = parsed.playlists[1].clone();
    scene_copy.id = "scene-copy-list".into();
    scene_copy.name = "Scene copy".into();
    scene_copy.entries[0].id = "scene-copy".into();
    let mut video_copy = parsed.playlists[0].clone();
    video_copy.id = "video-copy-list".into();
    video_copy.name = "Video copy".into();
    video_copy.entries = vec![video_copy.entries[1].clone()];
    video_copy.entries[0].id = "video-copy".into();
    parsed.playlists.push(scene_copy);
    parsed.playlists.push(video_copy);
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

    assert!(runtime_command(&mut runtime, at, "playlist-use", Some("night")).ok);
    state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
            entry_id: "scene-three".into(),
            kind: wall_in_one_service::config::EntryKind::Scene,
            scene_id: Some("12345".into()),
            output: String::new(),
            message: "scene 12345 crashed linux-wallpaperengine".into(),
            permanent_for_session: true,
        });
    runtime.tick(at, Instant::now());

    assert!(runtime_command(&mut runtime, at, "playlist-use", Some("day")).ok);
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
            entry_id: "video-two".into(),
            kind: wall_in_one_service::config::EntryKind::Video,
            scene_id: None,
            output: String::new(),
            message: "video decoder rejected /tmp/two.mp4".into(),
            permanent_for_session: true,
        });
    runtime.tick(at, Instant::now());

    let taboo = status(&mut runtime, at)["taboo_entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            (
                row["playlist_id"].as_str().unwrap().to_string(),
                row["entry_id"].as_str().unwrap().to_string(),
            )
        })
        .collect::<HashSet<_>>();
    for expected in [
        ("night".to_string(), "scene-three".to_string()),
        ("scene-copy-list".to_string(), "scene-copy".to_string()),
        ("day".to_string(), "video-two".to_string()),
        ("video-copy-list".to_string(), "video-copy".to_string()),
    ] {
        assert!(
            taboo.contains(&expected),
            "missing equivalent taboo {expected:?}"
        );
    }

    assert!(runtime_command(&mut runtime, at, "playlist-use", Some("scene-copy-list")).ok);
    assert!(!state.lock().unwrap().applies.last().unwrap().1);
    assert!(runtime_command(&mut runtime, at, "playlist-use", Some("video-copy-list")).ok);
    assert!(!state.lock().unwrap().applies.last().unwrap().1);
}

#[test]
fn active_taboo_entry_reports_attributable_static_fallback_in_both_display_modes() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    for independent in [false, true] {
        let document = (if independent {
            independent_config()
        } else {
            config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        })
        .replace(
            "motion = \"/tmp/two.mp4\"",
            "motion = \"/tmp/two.mp4\"\n\
             taboo = { reason = \"known decoder failure\", source = \"authoring\" }",
        );
        let parsed: Config = toml::from_str(&document).unwrap();
        let state = Arc::new(Mutex::new(RuntimeDriverState {
            connected_outputs: independent.then(|| vec!["DP-1".into()]),
            ..RuntimeDriverState::default()
        }));
        let mut runtime = Runtime::new(
            PathBuf::from("/tmp/runtime.toml"),
            parsed,
            RuntimeDriver(state),
            at,
        )
        .unwrap();
        runtime.apply_current().unwrap();
        let response = if independent {
            runtime_command(&mut runtime, at, "on", Some("DP-1 next"))
        } else {
            runtime_command(&mut runtime, at, "next", None)
        };
        assert!(response.ok, "{}", response.message);
        let row = if independent {
            display_status(&mut runtime, at, "DP-1")
        } else {
            status(&mut runtime, at)["displays"][0].clone()
        };
        assert_eq!(row["entry_id"], "video-two");
        assert_eq!(row["playback_state"], "playing");
        assert_eq!(row["motion_active"], false);
        assert_eq!(row["renderer_failed"], true);
        let diagnostic = row["last_error"].as_str().unwrap();
        assert!(diagnostic.contains("video-two"), "{diagnostic}");
        assert!(diagnostic.contains("known decoder failure"), "{diagnostic}");
        assert!(diagnostic.contains("paired still"), "{diagnostic}");
    }
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
    assert_eq!(snapshot["displays"][0]["connector"], "DP-1");
    assert_eq!(snapshot["displays"][0]["connected"], false);
    assert_eq!(snapshot["displays"][0]["assignment_source"], "explicit");
    assert_eq!(snapshot["displays"][1]["connector"], "DP-2");
    assert_eq!(snapshot["displays"][2]["connector"], "DP-3");
    assert_eq!(snapshot["displays"][2]["assignment_source"], "default");
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
fn schedule_provenance_change_with_identical_target_never_restarts_motion() {
    let summer = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let winter = NaiveDate::from_ymd_opt(2026, 12, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();

    for independent in [false, true] {
        let mut document = if independent {
            independent_config()
        } else {
            config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        };
        document.push_str(
            if independent {
                "\n[[schedules]]\nid = \"same-dp\"\nplaylist = \"day\"\nconnector = \"DP-1\"\nmonths = [12]\n"
            } else {
                "\n[[schedules]]\nid = \"same-global\"\nplaylist = \"day\"\nmonths = [12]\n"
            },
        );
        let parsed: Config = toml::from_str(&document).unwrap();
        let state = Arc::new(Mutex::new(RuntimeDriverState {
            connected_outputs: independent.then(|| vec!["DP-1".into()]),
            ..RuntimeDriverState::default()
        }));
        let mut runtime = Runtime::new(
            PathBuf::from("/tmp/runtime.toml"),
            parsed,
            RuntimeDriver(state.clone()),
            summer,
        )
        .unwrap();
        runtime.apply_current().unwrap();
        state.lock().unwrap().stage_events.clear();
        state.lock().unwrap().applies.clear();
        runtime.tick(winter, Instant::now());
        assert!(state.lock().unwrap().stage_events.is_empty());
        assert!(state.lock().unwrap().applies.is_empty());
        let snapshot = status(&mut runtime, winter);
        if independent {
            let dp = snapshot["displays"]
                .as_array()
                .unwrap()
                .iter()
                .find(|row| row["connector"] == "DP-1")
                .unwrap();
            assert_eq!(dp["route_source"], "schedule");
            assert_eq!(dp["schedule_rule_id"], "same-dp");
        } else {
            assert_eq!(snapshot["schedule"]["rule_id"], "same-global");
            assert_eq!(snapshot["schedule"]["in_force"], serde_json::Value::Null);
            assert!(snapshot["schedules"]
                .as_array()
                .unwrap()
                .iter()
                .any(|rule| rule["id"] == "same-global" && rule["in_force"] == true));
        }
    }
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
fn independent_same_playlist_override_after_reload_is_idempotent() {
    let root = directory("independent-idempotent-override");
    let config_path = root.join("runtime.toml");
    let original = independent_config();
    fs::write(&config_path, &original).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        config_path.clone(),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 playlist-use day")).ok);

    let updated = original
        .replace("/tmp/one.png", "/tmp/one-updated.png")
        .replace(
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        );
    fs::write(&config_path, updated).unwrap();
    state.lock().unwrap().stage_events.clear();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    let after_reload = state.lock().unwrap().stage_events.clone();
    assert_eq!(
        after_reload
            .iter()
            .filter(|event| event.as_str() == "stop DP-1")
            .count(),
        1,
        "reload itself must perform one DP-1 hand-over: {after_reload:?}"
    );

    let repeated = runtime_command(&mut runtime, at, "on", Some("DP-1 playlist-use day"));
    assert!(repeated.ok);
    assert!(repeated.message.contains("already active"));
    assert_eq!(
        state.lock().unwrap().stage_events,
        after_reload,
        "the same-id override reapplied a route already updated by reload"
    );
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
