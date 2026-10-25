use chrono::NaiveDate;
use std::collections::HashSet;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use support::compositor::{FakeCompositor, FakeHead, FakeOutput, Layout, WedgedCompositor};
use wall_in_one_service::config::{Config, Overrides, RendererSettings};
use wall_in_one_service::outputs::{Output, OutputSource};
use wall_in_one_service::protocol::{MAX_RESPONSE_BYTES, Response, escaped_len, write_response};
use wall_in_one_service::renderer::{
    MissingReason, MissingRenderer, PathIdentity, RendererFailure, SystemDriver, WallpaperDriver,
};
use wall_in_one_service::runtime::{Runtime, wall_clock_after};

mod support;

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

/// The Workshop ids these tests start as scenes.
const TEST_SCENE_IDS: &[&str] = &[
    "12",
    "777",
    "12345",
    "12346",
    "12347",
    "67890",
    "2777242023",
    "2910000004",
    "2946165131",
];

/// A home folder where linux-wallpaperengine finds each of `TEST_SCENE_IDS`,
/// each a `scene`. While a document doesn't allow web wallpapers (every
/// document here but the web tests'), the service reads an item's type there
/// before it starts the engine (`workshop::refusal_without_web`), so these
/// tests' scenes start as a real scene does.
fn scene_home() -> PathBuf {
    static HOME: OnceLock<PathBuf> = OnceLock::new();
    HOME.get_or_init(|| {
        let home = directory("scene-home");
        for id in TEST_SCENE_IDS {
            let item = home
                .join(".local/share/Steam/steamapps/workshop/content/431960")
                .join(id);
            fs::create_dir_all(&item).unwrap();
            fs::write(
                item.join("project.json"),
                r#"{"type":"scene","title":"Test scene","file":"scene.json"}"#,
            )
            .unwrap();
        }
        home
    })
    .clone()
}

/// The service binary, unable to reach a desktop's compositor: a test that
/// wants one gives it `WAYLAND_DISPLAY` itself. Its home is `scene_home`.
fn service_command() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"));
    command
        .env_remove("WAYLAND_DISPLAY")
        .env_remove("WAYLAND_SOCKET")
        .env("HOME", scene_home());
    command
}

/// A display source that answers from a list, counting the questions.
struct FixedOutputs {
    outputs: Result<Vec<Output>, String>,
    asked: Arc<AtomicUsize>,
}

impl OutputSource for FixedOutputs {
    fn outputs(&mut self) -> Result<Vec<Output>, String> {
        self.asked.fetch_add(1, Ordering::SeqCst);
        self.outputs.clone()
    }
}

/// A display source a test changes while it is in use, as a compositor does
/// when a monitor is switched off, unplugged or plugged in.
#[derive(Clone, Default)]
struct SwitchableOutputs(Arc<Mutex<Vec<Output>>>);

impl SwitchableOutputs {
    fn set(&self, outputs: Vec<Output>) {
        *self.0.lock().unwrap() = outputs;
    }
}

impl OutputSource for SwitchableOutputs {
    fn outputs(&mut self) -> Result<Vec<Output>, String> {
        Ok(self.0.lock().unwrap().clone())
    }
}

fn switched_off(name: &str) -> Output {
    Output {
        name: name.into(),
        enabled: false,
        ..Output::default()
    }
}

fn live(name: &str, refresh_millihz: Option<u64>) -> Output {
    Output {
        name: name.into(),
        enabled: true,
        refresh_millihz,
        ..Output::default()
    }
}

/// A driver whose compositor reports `outputs`, and how often it was asked.
fn driver_with(
    settings: RendererSettings,
    outputs: Vec<Output>,
) -> (SystemDriver, Arc<AtomicUsize>) {
    let asked = Arc::new(AtomicUsize::new(0));
    let source = FixedOutputs {
        outputs: Ok(outputs),
        asked: Arc::clone(&asked),
    };
    let driver = SystemDriver::with_output_source(
        settings,
        Path::new("/wall-in-one-runtime.sock"),
        Box::new(source),
    )
    .with_workshop_home(Some(scene_home()));
    (driver, asked)
}

/// A driver with no compositor to ask, never the desktop's.
fn offline_driver(settings: RendererSettings) -> SystemDriver {
    SystemDriver::with_output_source(
        settings,
        Path::new("/wall-in-one-runtime.sock"),
        Box::new(FixedOutputs {
            outputs: Err("cannot connect to the Wayland compositor: this test has none".into()),
            asked: Arc::new(AtomicUsize::new(0)),
        }),
    )
    .with_workshop_home(Some(scene_home()))
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

/// The service a test started, ended if the test fails first: otherwise it outlives the
/// test, holding cargo's output pipe open. Only that one process is signalled. Every
/// spawn of the service binary is wrapped in one; it reads as the `Child` it holds.
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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );

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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .arg("--wait-for-config")
            .stderr(Stdio::from(stderr))
            .spawn()
            .unwrap(),
    );

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
fn startup_config_load_failures_use_ex_config_without_claiming_the_socket() {
    let root = directory("startup-config-exit");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let old_schema = config(Path::new("/bin/true"), Path::new("/bin/true"), false).replacen(
        "schema_version = 4",
        "schema_version = 2",
        1,
    );

    let missing = service_command()
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .output()
        .unwrap();
    assert_eq!(missing.status.code(), Some(78));
    assert!(
        !socket.exists(),
        "missing config claimed the runtime socket"
    );
    assert!(
        String::from_utf8_lossy(&missing.stderr).contains("cannot read config"),
        "{}",
        String::from_utf8_lossy(&missing.stderr)
    );

    for (label, document, expected) in [
        (
            "old-schema",
            old_schema.as_str(),
            "schema_version 2 is unsupported; expected 4",
        ),
        (
            "malformed",
            "schema_version = [\n",
            "config is not valid TOML",
        ),
    ] {
        fs::write(&config_path, document).unwrap();
        let output = service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .arg("--wait-for-config")
            .output()
            .unwrap();

        assert_eq!(output.status.code(), Some(78), "{label}");
        assert!(!socket.exists(), "{label} claimed the runtime socket");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains(expected), "{label}: {stderr}");
    }

    fs::remove_dir_all(root).unwrap();
}

#[test]
fn config_check_uses_the_production_loader_without_claiming_runtime() {
    let root = directory("config-check");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    fs::write(
        &config_path,
        config(Path::new("/bin/false"), Path::new("/bin/false"), false),
    )
    .unwrap();

    let valid = service_command()
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .arg("--check-config")
        .output()
        .unwrap();
    assert!(
        valid.status.success(),
        "{}",
        String::from_utf8_lossy(&valid.stderr)
    );
    assert!(!socket.exists(), "config check claimed the runtime socket");

    let old_schema = fs::read_to_string(&config_path).unwrap().replacen(
        "schema_version = 4",
        "schema_version = 2",
        1,
    );
    fs::write(&config_path, old_schema).unwrap();
    let invalid = service_command()
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket)
        .arg("--check-config")
        .output()
        .unwrap();
    assert_eq!(invalid.status.code(), Some(78));
    assert!(String::from_utf8_lossy(&invalid.stderr).contains("expected 4"));
    assert!(
        !socket.exists(),
        "failed config check claimed the runtime socket"
    );

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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );
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

// -- r13d L2: a document installed while the service starts is loaded by its poll --------------

/// The generation the service at `socket` says it holds.
fn held_generation(socket: &Path) -> String {
    let reply = request(socket, "status", None);
    assert_eq!(reply["ok"], true, "{reply}");
    let status: serde_json::Value =
        serde_json::from_str(reply["message"].as_str().unwrap()).unwrap();
    status["config_generation"].as_str().unwrap().to_string()
}

const INSTALLED_GENERATION: &str =
    "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";

/// A service started on one document, and a second installed over it (a rename, as the app
/// installs one, with no `reload`): by its noctalia the first time that runs, which is in the
/// first apply, after the read and the bind (`in_first_apply`), or else once the service has
/// answered. `--wait-for-config` is the companion's direct start; without it, the unit's.
/// Whether the rename was done by the time the service first answered, and the generation it
/// holds once its poll has had five chances to load the second.
fn generation_after_an_install(
    label: &str,
    wait_for_config: bool,
    in_first_apply: bool,
) -> (bool, String) {
    use std::os::unix::fs::PermissionsExt;
    let root = directory(label);
    let config_path = root.join("runtime.toml");
    let next = root.join("next.toml");
    let marker = root.join("installed");
    let socket = root.join("runtime.sock");
    let stub = root.join("noctalia");
    fs::write(
        &stub,
        if in_first_apply {
            format!(
                "#!/bin/sh\nif [ -e {next:?} ]; then mv -f {next:?} {config_path:?} && : > {marker:?}; fi\n"
            )
        } else {
            "#!/bin/sh\nexit 0\n".into()
        },
    )
    .unwrap();
    fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();
    let first = config(&stub, &stub, false);
    fs::write(&config_path, &first).unwrap();
    fs::write(
        &next,
        first
            .replace(
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                INSTALLED_GENERATION,
            )
            .replace("/tmp/one.png", "/tmp/one-installed.png"),
    )
    .unwrap();
    let mut command = service_command();
    command
        .arg("--config")
        .arg(&config_path)
        .arg("--socket")
        .arg(&socket);
    if wait_for_config {
        command.arg("--wait-for-config");
    }
    let mut child = ServiceChild(command.spawn().unwrap());
    let _ = held_generation(&socket);
    let installed_in_the_start = marker.exists();
    if !in_first_apply {
        fs::rename(&next, &config_path).unwrap();
    }
    // The poll looks once a second.
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut held = held_generation(&socket);
    while held != INSTALLED_GENERATION && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(100));
        held = held_generation(&socket);
    }
    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
    (installed_in_the_start, held)
}

#[test]
fn a_document_installed_while_the_service_starts_is_loaded_by_its_poll() {
    // Review r13d L2: the service took its file's fingerprint only after its read, its bind and
    // its first apply, so a document installed in between counted as loaded, and nothing loaded
    // it until the next publication. The app's web-off guard installs one there with no reload
    // when no service answers, as does a toggle whose reload finds no socket.
    for wait_for_config in [false, true] {
        let (in_the_start, held) =
            generation_after_an_install("start-window-install", wait_for_config, true);
        assert!(
            in_the_start,
            "{wait_for_config}: the install wasn't in the start"
        );
        assert_eq!(
            held, INSTALLED_GENERATION,
            "--wait-for-config {wait_for_config}: the document installed during the first apply \
             was never loaded"
        );
    }
}

#[test]
fn a_document_installed_once_the_service_answers_is_loaded_by_its_poll() {
    // The control: the same install once the service has answered.
    for wait_for_config in [false, true] {
        let (in_the_start, held) =
            generation_after_an_install("answered-install", wait_for_config, false);
        assert!(!in_the_start);
        assert_eq!(held, INSTALLED_GENERATION, "{wait_for_config}");
    }
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
    let mut driver = offline_driver(parsed.renderer.clone());
    without_text_file_busy(|| driver.apply(&video, "eDP-1", &parsed.settings)).unwrap();
    // Noctalia's three calls, then mpvpaper's launch, before the scene's still.
    recorded_lines(&log, 4);
    let error =
        without_text_file_busy(|| driver.apply(&scene, "eDP-1", &parsed.settings)).unwrap_err();
    assert!(error.contains("paired still remains applied"));
    let events = recorded_lines(&log, 5);
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
    let noctalia = root.join("noctalia");
    let mpvpaper = root.join("mpvpaper");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
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
    let document = config(&noctalia, &mpvpaper, false)
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
    let (mut driver, asked) = driver_with(
        parsed.renderer.clone(),
        vec![live("DP-1", Some(165_004)), live("eDP-1", Some(165_004))],
    );

    driver.begin_apply();
    assert_eq!(
        driver.connected_outputs().unwrap(),
        vec!["DP-1".to_string(), "eDP-1".to_string()]
    );
    without_text_file_busy(|| driver.apply(&video, "eDP-1", &parsed.settings)).unwrap();
    without_text_file_busy(|| driver.apply(&video, "DP-1", &parsed.settings)).unwrap();
    driver.end_apply();

    assert_eq!(
        asked.load(Ordering::SeqCst),
        1,
        "one question to the compositor for the whole hand-over"
    );
    let arguments = recorded_lines(&launches, 2);
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
/// How long a test waits for what a renderer stand-in records. The driver starts a stand-in
/// and doesn't wait for it, so the stand-in writes in its own time, after `apply` returns, and a
/// loaded machine can hold a fresh shell back for seconds; a record that is there returns at
/// once. (A fixed sleep of 100 ms read records half-written, or not yet there, under load.)
const RECORD_DEADLINE: Duration = Duration::from_secs(20);

/// What a stand-in has written to `path` once it holds `count` whole lines, each ended by its
/// newline (a stand-in writes `printf '%s\n' "$*"`, one line a launch), waited for up to
/// `RECORD_DEADLINE`; what is there by then otherwise.
fn recorded_lines(path: &Path, count: usize) -> String {
    let deadline = Instant::now() + RECORD_DEADLINE;
    loop {
        let text = fs::read_to_string(path).unwrap_or_default();
        if text.matches('\n').count() >= count || Instant::now() >= deadline {
            return text;
        }
        thread::sleep(Duration::from_millis(10));
    }
}

/// A stand-in renderer that writes each argument on a line of its own, then marks the launch
/// recorded whole with a line of `launches_marked`.
fn argument_recorder(root: &Path, name: &str) -> (PathBuf, PathBuf) {
    use std::os::unix::fs::PermissionsExt;
    let arguments = root.join(format!("{name}-arguments"));
    let program = root.join(name);
    fs::write(
        &program,
        format!(
            "#!/bin/sh\nfor argument in \"$@\"; do printf '%s\\n' \"$argument\"; done >> {:?}\n\
             printf '\\n' >> {:?}\nsleep 30\n",
            arguments,
            launches_marked(&arguments),
        ),
    )
    .unwrap();
    fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
    (program, arguments)
}

/// Where `argument_recorder` marks each launch it has recorded whole: a line each.
fn launches_marked(arguments: &Path) -> PathBuf {
    arguments.with_extension("launched")
}

/// What an `argument_recorder` has recorded once `launches` launches are whole, counted since
/// it was made (removing `arguments` between launches leaves the count), waited for as
/// `recorded_lines` waits.
fn recorded_arguments(arguments: &Path, launches: usize) -> String {
    recorded_lines(&launches_marked(arguments), launches);
    fs::read_to_string(arguments).unwrap_or_default()
}

#[test]
fn a_videos_dials_reach_mpvpaper_each_as_one_option() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("video-dials");
    let noctalia = root.join("noctalia");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    let (mpvpaper, arguments) = argument_recorder(&root, "mpvpaper");
    let document = config(&noctalia, &mpvpaper, false)
        .replace("schema_version = 4", "schema_version = 7")
        .replace(
            "scene_clamp = \"\"\n",
            "scene_clamp = \"\"\nvideo_speed = 2.0\nvideo_contrast = -20\nvideo_scale = \"lanczos\"\nvideo_hidden_trigger = \"max\"\n",
        )
        .replace(
            "motion = \"/tmp/two.mp4\"",
            "motion = \"/tmp/two.mp4\"\nvideo = { fit = \"stretch\", speed = 0.5, gamma = 15, rotate = 90, deband = true }",
        );
    let parsed: Config = toml::from_str(&document).unwrap();
    parsed.validate().unwrap();
    let video = parsed.playlists[0].entries[1].clone();
    let (mut driver, _) = driver_with(parsed.renderer.clone(), vec![live("DP-1", None)]);
    without_text_file_busy(|| driver.apply(&video, "DP-1", &parsed.settings)).unwrap();

    let words: Vec<String> = recorded_arguments(&arguments, 1)
        .lines()
        .map(str::to_string)
        .collect();
    let position = |word: &str| words.iter().position(|w| w == word);
    let auto = position("--auto-mode").expect("--auto-mode");
    assert_eq!(words[auto + 1], "max");
    let options = &words[position("-o").expect("-o") + 1];
    let options: Vec<&str> = options.split(' ').collect();
    for expected in [
        "loop-file=inf",
        "panscan=0.0",
        "keepaspect=no",
        "speed=0.50",
        "contrast=-20",
        "gamma=15",
        "video-rotate=90",
        "deband=yes",
        "scale=lanczos",
    ] {
        assert!(
            options.contains(&expected),
            "missing {expected}: {options:?}"
        );
    }
    assert!(
        !options
            .iter()
            .any(|option| option.starts_with("brightness"))
    );
    driver.stop();

    // A video with no dials of its own, under default dials, gets today's options.
    fs::remove_file(&arguments).unwrap();
    let plain: Config = toml::from_str(&config(&noctalia, &mpvpaper, false)).unwrap();
    let video = plain.playlists[0].entries[1].clone();
    let (mut driver, _) = driver_with(plain.renderer.clone(), vec![live("DP-1", None)]);
    without_text_file_busy(|| driver.apply(&video, "DP-1", &plain.settings)).unwrap();
    let words: Vec<String> = recorded_arguments(&arguments, 2)
        .lines()
        .map(str::to_string)
        .collect();
    assert!(!words.iter().any(|word| word == "--auto-mode"));
    let options = &words[words.iter().position(|w| w == "-o").unwrap() + 1];
    assert!(
        options.starts_with("loop-file=inf panscan=1.0 terminal=no mute=yes volume=50 hwdec=auto"),
        "{options}"
    );
    for option in options.split(' ') {
        assert!(
            ![
                "keepaspect",
                "speed",
                "contrast",
                "gamma",
                "video-rotate",
                "deband",
                "scale",
                "start"
            ]
            .iter()
            .any(|name| option.starts_with(&format!("{name}="))),
            "{options}"
        );
    }
    driver.stop();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_scenes_dials_reach_linux_wallpaperengine() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("scene-dials");
    let noctalia = root.join("noctalia");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    let (engine, arguments) = argument_recorder(&root, "linux-wallpaperengine");
    let document = config(&noctalia, Path::new("/bin/true"), true)
        .replace("schema_version = 4", "schema_version = 7")
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!("linux_wallpaperengine_program = {engine:?}"),
        )
        .replace(
            "scene_clamp = \"\"\n",
            "scene_clamp = \"\"\nscene_pause_only_active = true\nscene_automute = false\nscene_mouse = false\n",
        )
        .replace(
            "scene_id = \"12345\"",
            "scene_id = \"12345\"\nscene = { fps = 60, scaling = \"fill\", clamp = \"border\", muted = false, volume = 40, audio_processing = false, particles = false, parallax = false }",
        );
    let parsed: Config = toml::from_str(&document).unwrap();
    parsed.validate().unwrap();
    let scene = parsed.playlists[1].entries[0].clone();
    let (mut driver, _) = driver_with(parsed.renderer.clone(), vec![live("DP-1", None)]);
    without_text_file_busy(|| driver.apply(&scene, "DP-1", &parsed.settings)).unwrap();

    let words: Vec<String> = recorded_arguments(&arguments, 1)
        .lines()
        .map(str::to_string)
        .collect();
    let after = |flag: &str| {
        let at = words
            .iter()
            .position(|w| w == flag)
            .unwrap_or_else(|| panic!("{flag}"));
        words[at + 1].clone()
    };
    assert_eq!(after("--fps"), "60");
    assert_eq!(after("--volume"), "40");
    assert_eq!(after("--scaling"), "fill");
    assert_eq!(after("--clamp"), "border");
    for flag in [
        "--fullscreen-pause-only-active",
        "--noautomute",
        "--no-audio-processing",
        "--disable-particles",
        "--disable-mouse",
        "--disable-parallax",
    ] {
        assert!(words.iter().any(|w| w == flag), "missing {flag}: {words:?}");
    }
    assert!(
        !words.iter().any(|w| w == "--silent"),
        "never both --silent and --volume"
    );
    driver.stop();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn interpolation_off_never_asks_the_compositor() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("video-interpolation-off");
    let launches = root.join("launches");
    let noctalia = root.join("noctalia");
    let mpvpaper = root.join("mpvpaper");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
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
    let document = config(&noctalia, &mpvpaper, false);
    let parsed: Config = toml::from_str(&document).unwrap();
    let video = parsed.playlists[0].entries[1].clone();
    let (mut driver, asked) =
        driver_with(parsed.renderer.clone(), vec![live("eDP-1", Some(60_000))]);

    without_text_file_busy(|| driver.apply(&video, "eDP-1", &parsed.settings)).unwrap();
    assert_eq!(asked.load(Ordering::SeqCst), 0);
    let arguments = recorded_lines(&launches, 1);
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
    let mpvpaper = root.join("mpvpaper");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
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
        "video_interpolation = \"off\"",
        "video_interpolation = \"linear\"",
    );
    let parsed: Config = toml::from_str(&document).unwrap();
    let video = parsed.playlists[0].entries[1].clone();
    let (mut driver, _) = driver_with(
        parsed.renderer.clone(),
        vec![live("DP-1", Some(60_000)), live("eDP-1", Some(165_004))],
    );

    without_text_file_busy(|| driver.apply(&video, "", &parsed.settings)).unwrap();
    let arguments = recorded_lines(&launches, 1);
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
    let engine = root.join("linux-wallpaperengine");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::write(
        &engine,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nsleep 30\n",
            events
        ),
    )
    .unwrap();
    for executable in [&noctalia, &engine] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, Path::new("/bin/true"), true)
        .replace("scene_fps = 30", "scene_fps = 75")
        .replace("scene_scaling = \"\"", "scene_scaling = \"fit\"")
        .replace("scene_clamp = \"\"", "scene_clamp = \"repeat\"")
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!("linux_wallpaperengine_program = {engine:?}"),
        );
    let parsed: Config = toml::from_str(&document).unwrap();
    let scene = parsed.playlists[1].entries[0].clone();
    let (mut driver, _) = driver_with(
        parsed.renderer.clone(),
        vec![live("DP-1", None), live("eDP-1", None)],
    );

    without_text_file_busy(|| driver.apply(&scene, "", &parsed.settings)).unwrap();

    // One engine for every connector: one launch.
    let launched = recorded_lines(&events, 1);
    assert!(launched.contains("--screen-root DP-1 --scaling fit --clamp repeat --bg 12345"));
    assert!(launched.contains("--screen-root eDP-1 --scaling fit --clamp repeat --bg 12345"));
    assert!(launched.contains("--layer background"));
    assert!(launched.contains("--fps 75"));
    assert!(launched.contains("--scaling fit"));
    assert!(launched.contains("--clamp repeat"));
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
fn a_preset_renders_its_base_with_each_property_as_arguments_of_its_own() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("scene-preset");
    let noctalia = root.join("noctalia");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    // One argument per line, so a value with spaces shows as one argument.
    let (engine, arguments) = argument_recorder(&root, "linux-wallpaperengine");
    let document = config(&noctalia, Path::new("/bin/true"), true)
        .replace("schema_version = 4", "schema_version = 6")
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!("linux_wallpaperengine_program = {engine:?}"),
        )
        .replace(
            "scene_id = \"12345\"",
            "scene_id = \"12345\"\nscene_base = \"67890\"\n\
             properties = { schemecolor = \"0.500000 0.200000 1.000000\", bloom = \"true\" }",
        );
    let parsed: Config = toml::from_str(&document).unwrap();
    parsed.validate().unwrap();
    let preset = parsed.playlists[1].entries[0].clone();
    let (mut driver, _) = driver_with(parsed.renderer.clone(), vec![live("DP-1", None)]);

    without_text_file_busy(|| driver.apply(&preset, "DP-1", &parsed.settings)).unwrap();

    let launched = recorded_arguments(&arguments, 1);
    let words: Vec<&str> = launched.lines().collect();
    let pairs: Vec<(&str, &str)> = words
        .windows(2)
        .filter(|pair| pair[0] == "--set-property")
        .map(|pair| (pair[0], pair[1]))
        .collect();
    assert_eq!(
        pairs,
        [
            ("--set-property", "bloom=true"),
            ("--set-property", "schemecolor=0.500000 0.200000 1.000000"),
        ],
        "{launched}"
    );
    let bg: Vec<&str> = words
        .windows(2)
        .filter(|pair| pair[0] == "--bg")
        .map(|pair| pair[1])
        .collect();
    assert_eq!(bg, ["67890"], "the base renders, never the preset's own id");
    assert!(!words.contains(&"12345"), "{launched}");
    driver.stop();

    // The same entry without them launches exactly as a scene always did.
    fs::remove_file(&arguments).unwrap();
    let plain = config(&noctalia, Path::new("/bin/true"), true).replace(
        "linux_wallpaperengine_program = \"/bin/true\"",
        &format!("linux_wallpaperengine_program = {engine:?}"),
    );
    let parsed: Config = toml::from_str(&plain).unwrap();
    let scene = parsed.playlists[1].entries[0].clone();
    let (mut driver, _) = driver_with(parsed.renderer.clone(), vec![live("DP-1", None)]);
    without_text_file_busy(|| driver.apply(&scene, "DP-1", &parsed.settings)).unwrap();
    let launched = recorded_arguments(&arguments, 2);
    assert!(!launched.contains("--set-property"), "{launched}");
    assert!(launched.ends_with("--bg\n12345\n"), "{launched}");
    driver.stop();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_switched_off_head_is_published_but_never_a_scene_target() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("scene-switched-off");
    let events = root.join("events");
    let noctalia = root.join("noctalia");
    let engine = root.join("linux-wallpaperengine");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::write(
        &engine,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nsleep 30\n",
            events
        ),
    )
    .unwrap();
    for executable in [&noctalia, &engine] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, Path::new("/bin/true"), true).replace(
        "linux_wallpaperengine_program = \"/bin/true\"",
        &format!("linux_wallpaperengine_program = {engine:?}"),
    );
    let parsed: Config = toml::from_str(&document).unwrap();
    let scene = parsed.playlists[1].entries[0].clone();
    let off = Output {
        name: "HDMI-A-1".into(),
        enabled: false,
        ..Output::default()
    };
    let (mut driver, _) = driver_with(
        parsed.renderer.clone(),
        vec![live("DP-1", None), off.clone(), live("eDP-1", None)],
    );

    assert_eq!(
        driver.connected_outputs().unwrap(),
        vec!["DP-1".to_string(), "eDP-1".to_string()]
    );
    assert!(driver.published_outputs().contains(&off));
    without_text_file_busy(|| driver.apply(&scene, "", &parsed.settings)).unwrap();

    let launched = recorded_lines(&events, 1);
    assert_eq!(launched.matches("--screen-root").count(), 2);
    assert!(!launched.contains("HDMI-A-1"), "{launched}");
    driver.stop();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_compositor_with_only_switched_off_heads_is_a_readiness_error() {
    let parsed: Config = toml::from_str(&config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        true,
    ))
    .unwrap();
    let off = Output {
        name: "eDP-1".into(),
        enabled: false,
        ..Output::default()
    };
    let (mut driver, _) = driver_with(parsed.renderer.clone(), vec![off]);

    let error = driver.connected_outputs().unwrap_err();

    assert!(
        error.contains("the compositor reported no enabled outputs"),
        "{error}"
    );
    assert!(
        driver.published_outputs().is_empty(),
        "nothing succeeded yet"
    );
}

/// One independent runtime over `source`, its noctalia recording to `log`.
fn switchable_runtime(
    root: &Path,
    label: &str,
    source: &SwitchableOutputs,
) -> (Runtime<SystemDriver>, PathBuf) {
    use std::os::unix::fs::PermissionsExt;
    let log = root.join(format!("{label}.log"));
    let noctalia = root.join(format!("{label}-noctalia"));
    fs::write(
        &noctalia,
        format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log:?}\n"),
    )
    .unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    let document = independent_config()
        .replace(
            "noctalia_program = \"/bin/true\"",
            &format!("noctalia_program = {noctalia:?}"),
        )
        .replace("dynamics_enabled = true", "dynamics_enabled = false");
    let config_path = root.join(format!("{label}.toml"));
    fs::write(&config_path, &document).unwrap();
    let parsed: Config = toml::from_str(&document).unwrap();
    let driver = SystemDriver::with_output_source(
        parsed.renderer.clone(),
        Path::new("/wall-in-one-runtime.sock"),
        Box::new(source.clone()),
    )
    .with_workshop_home(Some(scene_home()));
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    (Runtime::new(config_path, parsed, driver, at).unwrap(), log)
}

/// What a display row says about where the display stands.
fn route_summary(row: &serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "connected": row["connected"],
        "playlist_id": row["playlist_id"],
        "entry_id": row["entry_id"],
        "route_source": row["route_source"],
        "manual_override": row["manual_override"],
        "renderer_failed": row["renderer_failed"],
        "last_error": row["last_error"],
    })
}

fn applied_on(log: &Path, connector: &str) -> usize {
    fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter(|line| line.starts_with(&format!("msg wallpaper-set {connector} ")))
        .count()
}

#[test]
fn a_display_switched_off_is_treated_exactly_like_one_unplugged() {
    let root = directory("switched-off-like-unplugged");
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let both = vec![live("DP-1", None), live("HDMI-A-1", None)];
    let switched = SwitchableOutputs::default();
    let unplugged = SwitchableOutputs::default();
    switched.set(both.clone());
    unplugged.set(both.clone());
    let (mut off, off_log) = switchable_runtime(&root, "off", &switched);
    let (mut gone, gone_log) = switchable_runtime(&root, "gone", &unplugged);
    for runtime in [&mut off, &mut gone] {
        runtime.apply_current().unwrap();
        // Off the first entry, so a cursor reset would show.
        assert!(runtime_command(runtime, at, "on", Some("HDMI-A-1 next")).ok);
    }
    let base = Instant::now();
    let before = display_status(&mut off, at, "HDMI-A-1");
    let applied = applied_on(&off_log, "HDMI-A-1");
    assert_eq!(applied, applied_on(&gone_log, "HDMI-A-1"));

    // One monitor switched off, the other unplugged.
    switched.set(vec![live("DP-1", None), switched_off("HDMI-A-1")]);
    unplugged.set(vec![live("DP-1", None)]);
    off.tick(at, base + Duration::from_secs(6));
    gone.tick(at, base + Duration::from_secs(6));
    let off_row = display_status(&mut off, at, "HDMI-A-1");
    assert_eq!(off_row["connected"], false, "{off_row}");
    assert_eq!(
        route_summary(&off_row),
        route_summary(&display_status(&mut gone, at, "HDMI-A-1"))
    );

    // Both back.
    switched.set(both.clone());
    unplugged.set(both);
    off.tick(at, base + Duration::from_secs(12));
    gone.tick(at, base + Duration::from_secs(12));
    let back = display_status(&mut off, at, "HDMI-A-1");
    assert_eq!(back["connected"], true, "{back}");
    assert_eq!(
        route_summary(&back),
        route_summary(&display_status(&mut gone, at, "HDMI-A-1"))
    );
    // Its assignment is applied again, the same way in both.
    assert_eq!(applied_on(&off_log, "HDMI-A-1"), applied + 1);
    assert_eq!(applied_on(&gone_log, "HDMI-A-1"), applied + 1);
    // A retained route keeps its cursor across both, as it always did.
    assert_eq!(back["entry_id"], before["entry_id"]);
    off.shutdown();
    gone.shutdown();
    fs::remove_dir_all(root).unwrap();
}

/// Each display row's connector, whether it is connected, and where its
/// assignment comes from, by connector.
fn display_rows<D: WallpaperDriver>(
    runtime: &mut Runtime<D>,
    at: chrono::NaiveDateTime,
) -> Vec<(String, bool, String)> {
    let mut rows: Vec<(String, bool, String)> = status(runtime, at)["displays"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            (
                row["connector"].as_str().unwrap().to_string(),
                row["connected"].as_bool().unwrap(),
                row["assignment_source"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    rows.sort();
    rows
}

/// Each display row's connector, effective Cycle and its source.
fn cycle_rows<D: WallpaperDriver>(
    runtime: &mut Runtime<D>,
    at: chrono::NaiveDateTime,
) -> Vec<(String, bool, String)> {
    let mut rows: Vec<(String, bool, String)> = status(runtime, at)["displays"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            (
                row["connector"].as_str().unwrap().to_string(),
                row["cycle_enabled"].as_bool().unwrap(),
                row["cycle_source"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    rows.sort();
    rows
}

fn connected_only(connectors: &[&str]) -> Arc<Mutex<RuntimeDriverState>> {
    Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(connectors.iter().map(|name| name.to_string()).collect()),
        ..RuntimeDriverState::default()
    }))
}

#[test]
fn a_reload_into_independent_mode_lists_a_detached_assignment_as_a_start_does() {
    let root = directory("reload-detached-assignment");
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    // HDMI-A-1 is assigned but not connected (unplugged, or switched off).
    let started_path = root.join("started.toml");
    fs::write(&started_path, independent_config()).unwrap();
    let mut started = Runtime::new(
        started_path.clone(),
        Config::load(&started_path).unwrap(),
        RuntimeDriver(connected_only(&["DP-1"])),
        at,
    )
    .unwrap();
    started.apply_current().unwrap();
    let expected = vec![
        ("DP-1".to_string(), true, "explicit".to_string()),
        ("HDMI-A-1".to_string(), false, "explicit".to_string()),
    ];
    assert_eq!(display_rows(&mut started, at), expected);

    // The same assignments, turned on by a reload of a mirrored service.
    let reloaded_path = root.join("reloaded.toml");
    fs::write(
        &reloaded_path,
        config(Path::new("/bin/true"), Path::new("/bin/true"), false),
    )
    .unwrap();
    let mut reloaded = Runtime::new(
        reloaded_path.clone(),
        Config::load(&reloaded_path).unwrap(),
        RuntimeDriver(connected_only(&["DP-1"])),
        at,
    )
    .unwrap();
    reloaded.apply_current().unwrap();
    fs::write(&reloaded_path, independent_config()).unwrap();
    let response = runtime_command(&mut reloaded, at, "reload", None);
    assert!(response.ok, "{}", response.message);
    assert_eq!(display_rows(&mut reloaded, at), expected);
    assert_eq!(
        route_summary(&display_status(&mut reloaded, at, "HDMI-A-1")),
        route_summary(&display_status(&mut started, at, "HDMI-A-1"))
    );

    // An assignment added by a reload for a display never connected.
    fs::write(
        &reloaded_path,
        format!(
            "{}[[displays]]\nconnector = \"DP-2\"\nplaylist = \"night\"\n",
            independent_config()
        ),
    )
    .unwrap();
    let response = runtime_command(&mut reloaded, at, "reload", None);
    assert!(response.ok, "{}", response.message);
    let rows = display_rows(&mut reloaded, at);
    assert!(
        rows.contains(&("DP-2".to_string(), false, "explicit".to_string())),
        "{rows:?}"
    );
    let added = display_status(&mut reloaded, at, "DP-2");
    assert_eq!(added["playlist_id"], "night", "{added}");
    assert_eq!(added["motion_active"], false, "{added}");
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_session_cycle_override_survives_a_mode_change_and_holds_for_later_routes() {
    let root = directory("cycle-across-modes");
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let cycling =
        |document: String| document.replace("cycle_enabled = false", "cycle_enabled = true");
    let path = root.join("runtime.toml");
    fs::write(
        &path,
        cycling(config(
            Path::new("/bin/true"),
            Path::new("/bin/true"),
            false,
        )),
    )
    .unwrap();
    let state = connected_only(&["DP-1"]);
    let mut runtime = Runtime::new(
        path.clone(),
        Config::load(&path).unwrap(),
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let response = runtime_command(&mut runtime, at, "cycle", Some("off"));
    assert_eq!(response.message, "cycle off (manual)");

    // Into independent mode: `cycle off` holds until the service exits or
    // `cycle default`, so every route keeps it, the detached HDMI-A-1's too.
    fs::write(&path, cycling(independent_config())).unwrap();
    let response = runtime_command(&mut runtime, at, "reload", None);
    assert!(response.ok, "{}", response.message);
    let off = |connector: &str| (connector.to_string(), false, "manual".to_string());
    assert_eq!(cycle_rows(&mut runtime, at), [off("DP-1"), off("HDMI-A-1")]);

    // HDMI-A-1 connects, and DP-2, never seen before: theirs too.
    state.lock().unwrap().connected_outputs =
        Some(vec!["DP-1".into(), "HDMI-A-1".into(), "DP-2".into()]);
    let base = Instant::now();
    runtime.tick(at, base + Duration::from_secs(6));
    assert_eq!(
        cycle_rows(&mut runtime, at),
        [off("DP-1"), off("DP-2"), off("HDMI-A-1")]
    );
    let before = display_status(&mut runtime, at, "DP-2")["entry_id"].clone();
    runtime.tick(at, base + Duration::from_secs(400));
    assert_eq!(display_status(&mut runtime, at, "DP-2")["entry_id"], before);

    // `cycle default` drops it everywhere.
    assert!(runtime_command(&mut runtime, at, "cycle", Some("default")).ok);
    let following = |connector: &str| (connector.to_string(), true, "config".to_string());
    assert_eq!(
        cycle_rows(&mut runtime, at),
        [following("DP-1"), following("DP-2"), following("HDMI-A-1")]
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn an_independent_session_cycle_override_covers_detached_and_later_routes() {
    let root = directory("independent-session-cycle");
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let path = root.join("runtime.toml");
    fs::write(
        &path,
        independent_config().replace("cycle_enabled = false", "cycle_enabled = true"),
    )
    .unwrap();
    let state = connected_only(&["DP-1"]);
    let mut runtime = Runtime::new(
        path.clone(),
        Config::load(&path).unwrap(),
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    // HDMI-A-1 is detached; one connector's own override is not the session's.
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 cycle off")).ok);
    let on = |connector: &str| (connector.to_string(), true, "config".to_string());
    let off = |connector: &str| (connector.to_string(), false, "manual".to_string());
    assert_eq!(cycle_rows(&mut runtime, at), [off("DP-1"), on("HDMI-A-1")]);

    assert!(runtime_command(&mut runtime, at, "cycle", Some("off")).ok);
    assert_eq!(cycle_rows(&mut runtime, at), [off("DP-1"), off("HDMI-A-1")]);

    state.lock().unwrap().connected_outputs =
        Some(vec!["DP-1".into(), "HDMI-A-1".into(), "DP-2".into()]);
    runtime.tick(at, Instant::now() + Duration::from_secs(6));
    assert_eq!(
        cycle_rows(&mut runtime, at),
        [off("DP-1"), off("DP-2"), off("HDMI-A-1")]
    );

    // One route's own override still applies to that route alone.
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-2 cycle on")).ok);
    assert_eq!(
        cycle_rows(&mut runtime, at),
        [
            off("DP-1"),
            ("DP-2".to_string(), true, "manual".to_string()),
            off("HDMI-A-1")
        ]
    );
    assert!(runtime_command(&mut runtime, at, "cycle", Some("default")).ok);
    assert_eq!(
        cycle_rows(&mut runtime, at),
        [on("DP-1"), on("DP-2"), on("HDMI-A-1")]
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_failed_question_keeps_the_last_published_snapshot() {
    let parsed: Config = toml::from_str(&config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        true,
    ))
    .unwrap();
    let source = SwitchableOutputs::default();
    source.set(vec![
        live("DP-1", Some(60_000)),
        live("eDP-1", Some(60_000)),
    ]);
    let mut driver = SystemDriver::with_output_source(
        parsed.renderer.clone(),
        Path::new("/wall-in-one-runtime.sock"),
        Box::new(source.clone()),
    )
    .with_workshop_home(Some(scene_home()));
    driver.connected_outputs().unwrap();
    let published = driver.published_outputs().to_vec();
    assert_eq!(published.len(), 2);

    // Every head switched off: discovery fails, and status keeps saying
    // what the last successful answer was.
    source.set(vec![switched_off("DP-1"), switched_off("eDP-1")]);
    assert!(driver.connected_outputs().is_err());
    assert_eq!(driver.published_outputs(), published.as_slice());
}

#[test]
fn status_publishes_the_compositor_snapshot_with_switched_off_heads() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("status-outputs");
    let noctalia = root.join("noctalia");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    let document = config(&noctalia, Path::new("/bin/true"), false);
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, &document).unwrap();
    let parsed: Config = toml::from_str(&document).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let off = Output {
        name: "HDMI-A-1".into(),
        enabled: false,
        physical_width_mm: 700,
        ..Output::default()
    };
    let (driver, _) = driver_with(
        parsed.renderer.clone(),
        vec![live("eDP-1", Some(60_000)), off],
    );
    let mut runtime = Runtime::new(config_path, parsed, driver, at).unwrap();
    runtime.apply_current().unwrap();

    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "status".into(),
            argument: None,
        },
        at,
    );
    let status: serde_json::Value = serde_json::from_str(&response.message).unwrap();

    let outputs = status["outputs"].as_array().unwrap();
    assert_eq!(outputs.len(), 2, "{status}");
    assert_eq!(outputs[0]["name"], "eDP-1");
    assert_eq!(outputs[0]["enabled"], true);
    assert_eq!(outputs[0]["refresh_millihz"], 60_000);
    assert_eq!(outputs[1]["name"], "HDMI-A-1");
    assert_eq!(outputs[1]["enabled"], false);
    assert_eq!(outputs[1]["physical_width_mm"], 700);
    assert_eq!(outputs[1]["refresh_millihz"], serde_json::Value::Null);
    // What the companion reads is unchanged: the mirrored route, live.
    assert_eq!(status["output_discovery_error"], "");
    assert_eq!(status["displays"][0]["connector"], "ALL");
    assert_eq!(status["displays"][0]["connected"], true);
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
    let mut driver = offline_driver(parsed.renderer.clone());

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
    for executable in [&noctalia, &engine] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, Path::new("/bin/true"), true).replace(
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
        driver_with(
            toml::from_str::<Config>(&document).unwrap().renderer,
            vec![live("eDP-1", None)],
        )
        .0,
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
    // The engine exits on its own, in its own time: each tick looks for it (one before it has
    // exited changes nothing), until a tick has seen it go.
    let deadline = Instant::now() + RECORD_DEADLINE;
    let crash_status: serde_json::Value = loop {
        runtime.tick(at, Instant::now());
        let response = runtime.handle(
            wall_in_one_service::protocol::Request {
                verb: "status".into(),
                argument: None,
            },
            at,
        );
        let status: serde_json::Value = serde_json::from_str(&response.message).unwrap();
        if status["last_error"] != "" || Instant::now() >= deadline {
            break status;
        }
        thread::sleep(Duration::from_millis(10));
    };
    assert_eq!(
        crash_status["config_generation"],
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    );
    assert_eq!(
        crash_status["config_path"],
        root.join("runtime.toml").to_str().unwrap()
    );
    assert_eq!(crash_status["motion_active"], false);
    assert!(
        crash_status["last_error"]
            .as_str()
            .unwrap()
            .contains("scene 12345")
    );
    assert!(
        crash_status["last_error"]
            .as_str()
            .unwrap()
            .contains("linux-wallpaperengine")
    );
    assert!(
        crash_status["last_error"]
            .as_str()
            .unwrap()
            .contains("unsupported scene shader")
    );
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
    assert!(!suppressed.ok);
    assert!(suppressed.message.contains("every entry is marked taboo"));
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
    assert!(
        status(&mut runtime, at)["taboo_entries"]
            .as_array()
            .unwrap()
            .is_empty()
    );
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
    let mut driver = offline_driver(parsed.renderer.clone());

    for _visit in 0..2 {
        without_text_file_busy(|| driver.apply(&video, "eDP-1", &parsed.settings)).unwrap();
        // mpvpaper exits on its own, in its own time.
        let deadline = Instant::now() + RECORD_DEADLINE;
        let failures = loop {
            let failures = driver.poll_failures();
            if !failures.is_empty() || Instant::now() >= deadline {
                break failures;
            }
            thread::sleep(Duration::from_millis(10));
        };
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
    /// Refuse every apply on this one output, as a renderer that cannot
    /// start there would, while other outputs succeed.
    fail_apply_output: Option<String>,
    fail_applies_remaining: usize,
    fail_entry_id: Option<String>,
    fail_entry_attempts_remaining: usize,
    fail_stage_entry_id: Option<String>,
    fail_stage_output: Option<String>,
    fail_stage_attempts_remaining: usize,
    fail_palette_attempts_remaining: usize,
    fail_motion_attempts_remaining: usize,
    fail_pause: bool,
    failures: Vec<String>,
    renderer_failures: Vec<RendererFailure>,
    connected_outputs: Option<Vec<String>>,
    output_probes: usize,
    video_audio: Vec<(bool, u8)>,
    reconfigures: usize,
    stage_events: Vec<String>,
    /// What the driver says it could not start at all (`missing_renderers`).
    missing_renderers: Vec<MissingRenderer>,
    /// Entries whose motion this driver won't start, as a real one refuses
    /// a web page while web wallpapers aren't allowed: the still goes up, and
    /// `motion_refused` says why (`refused_outputs`).
    refuse_entry_ids: HashSet<String>,
    refused_outputs: std::collections::HashMap<String, String>,
    /// Whether what `refuse_entry_ids` refuses at the start is a web page.
    refuse_as_web_page: bool,
    /// Entries this driver says it won't start when asked first
    /// (`motion_refusal`), as a real one answers for a scene the engine won't
    /// find; and how often each entry was asked.
    refuse_when_asked: HashSet<String>,
    asked: std::collections::HashMap<String, usize>,
    /// How often the runtime set the animation policy, which it does once
    /// for each display each time it works out the settings motion starts
    /// with.
    inhibition_sets: usize,
}

#[derive(Clone)]
struct RuntimeDriver(Arc<Mutex<RuntimeDriverState>>);

thread_local! {
    /// What a `RuntimeDriver` on this test's thread publishes as `outputs`.
    static RUNTIME_DRIVER_PUBLISHED: std::cell::Cell<&'static [Output]> =
        const { std::cell::Cell::new(&[]) };
}

impl WallpaperDriver for RuntimeDriver {
    fn published_outputs(&self) -> &[Output] {
        RUNTIME_DRIVER_PUBLISHED.with(std::cell::Cell::get)
    }

    fn missing_renderers(&self) -> Vec<MissingRenderer> {
        self.0.lock().unwrap().missing_renderers.clone()
    }

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
        if state.fail_apply_output.as_deref() == Some(output) {
            return Err(format!("renderer refused {output}"));
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
        state.refused_outputs.remove(output);
        if settings.dynamics_enabled
            && entry.kind != wall_in_one_service::config::EntryKind::Still
            && state.refuse_entry_ids.contains(&entry.id)
        {
            state.refused_outputs.insert(
                output.to_string(),
                format!("the test driver refuses {}", entry.id),
            );
            state.active_outputs.remove(output);
            state.motion_active = !state.active_outputs.is_empty();
            return Ok(());
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
        state.refused_outputs.remove(output);
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
            {
                let mut state = self.0.lock().unwrap();
                if state.fail_motion_attempts_remaining > 0 {
                    state.fail_motion_attempts_remaining -= 1;
                    return Err("renderer refused motion launch".into());
                }
            }
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
        state
            .refused_outputs
            .retain(|output, _| wanted.contains(output.as_str()));
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

    fn motion_refused(&self, output: &str) -> Option<wall_in_one_service::workshop::Refusal> {
        let state = self.0.lock().unwrap();
        state
            .refused_outputs
            .get(output)
            .map(|reason| wall_in_one_service::workshop::Refusal {
                web_page: state.refuse_as_web_page,
                reason: reason.clone(),
            })
    }

    fn motion_refusal(
        &self,
        entry: &wall_in_one_service::config::Entry,
        settings: &wall_in_one_service::config::Settings,
    ) -> Option<wall_in_one_service::workshop::Refusal> {
        let mut state = self.0.lock().unwrap();
        *state.asked.entry(entry.id.clone()).or_default() += 1;
        (settings.dynamics_enabled
            && entry.kind == wall_in_one_service::config::EntryKind::Scene
            && state.refuse_when_asked.contains(&entry.id))
        .then(|| wall_in_one_service::workshop::Refusal {
            web_page: false,
            reason: format!("the test driver won't start {}", entry.id),
        })
    }

    fn set_animation_inhibited(&mut self, _inhibited: bool) {
        self.0.lock().unwrap().inhibition_sets += 1;
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
        state.refused_outputs.clear();
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

fn power_observation(
    source: wall_in_one_service::power::PowerSource,
) -> wall_in_one_service::power::PowerObservation {
    wall_in_one_service::power::PowerObservation {
        source,
        last_known: source,
    }
}

fn battery_runtime(
    independent: bool,
) -> (
    Runtime<RuntimeDriver>,
    Arc<Mutex<RuntimeDriverState>>,
    chrono::NaiveDateTime,
) {
    let document = if independent {
        independent_config()
    } else {
        config(Path::new("/bin/true"), Path::new("/bin/true"), true)
    };
    battery_runtime_document(&document)
}

fn battery_runtime_document(
    document: &str,
) -> (
    Runtime<RuntimeDriver>,
    Arc<Mutex<RuntimeDriverState>>,
    chrono::NaiveDateTime,
) {
    let mut parsed: Config = toml::from_str(document).unwrap();
    parsed.schema_version = 5;
    parsed.settings.stop_animations_on_battery = true;
    parsed.renderer.own_scene_renderer = true;
    parsed.schedules.clear();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    (runtime, state, at)
}

#[test]
fn battery_startup_and_manual_commands_never_launch_motion() {
    use wall_in_one_service::power::PowerSource::{Ac, Battery};
    for independent in [false, true] {
        let (mut runtime, state, at) = battery_runtime(independent);
        runtime.observe_power(power_observation(Battery));
        runtime.apply_current().unwrap();
        assert!(runtime_command(&mut runtime, at, "next", None).ok);
        assert!(
            runtime_command(&mut runtime, at, "play", None)
                .message
                .contains("battery")
        );
        assert!(runtime_command(&mut runtime, at, "playlist-use", Some("night")).ok);
        assert!(
            state
                .lock()
                .unwrap()
                .applies
                .iter()
                .all(|(_, motion)| !motion)
        );
        let before = status(&mut runtime, at);
        assert_eq!(before["animations_inhibited"], true);
        assert_eq!(before["motion_active"], false);
        assert_eq!(before["playback_state"], "playing");
        state.lock().unwrap().stage_events.clear();
        runtime.observe_power(power_observation(Ac));
        let after = status(&mut runtime, at);
        assert_eq!(after["motion_active"], true);
        assert_eq!(after["entry_id"], before["entry_id"]);
        assert_eq!(after["playlist_id"], before["playlist_id"]);
        assert!(
            state
                .lock()
                .unwrap()
                .stage_events
                .iter()
                .all(|event| event.starts_with("motion "))
        );
        let applies = state.lock().unwrap().applies.len();
        runtime.observe_power(power_observation(Ac));
        assert_eq!(state.lock().unwrap().applies.len(), applies);
    }
}

#[test]
fn battery_preserves_paused_and_stopped_intent_until_explicit_play() {
    use wall_in_one_service::power::PowerSource::{Ac, Battery, Unknown};
    let (mut runtime, state, at) = battery_runtime(true);
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 pause")).ok);
    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 stop")).ok);
    runtime.observe_power(power_observation(Battery));
    assert!(state.lock().unwrap().active_outputs.is_empty());
    runtime.observe_power(wall_in_one_service::power::PowerObservation {
        source: Unknown,
        last_known: Battery,
    });
    assert_eq!(
        status(&mut runtime, at)["animation_inhibition_reason"],
        "power-unavailable"
    );
    runtime.observe_power(power_observation(Ac));
    assert!(state.lock().unwrap().active_outputs.is_empty());
    assert_eq!(
        display_status(&mut runtime, at, "DP-1")["playback_state"],
        "paused"
    );
    assert_eq!(
        display_status(&mut runtime, at, "HDMI-A-1")["playback_state"],
        "stopped"
    );
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 play")).ok);
    assert_eq!(
        state.lock().unwrap().active_outputs,
        HashSet::from(["DP-1".into()])
    );
}

#[test]
fn battery_release_of_mirrored_pause_can_be_played_after_ac_returns() {
    use wall_in_one_service::power::PowerSource::{Ac, Battery};
    let (mut runtime, state, at) = battery_runtime(false);
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    assert!(runtime_command(&mut runtime, at, "pause", None).ok);
    runtime.observe_power(power_observation(Battery));
    runtime.observe_power(power_observation(Ac));
    assert!(!state.lock().unwrap().motion_active);
    assert_eq!(status(&mut runtime, at)["playback_state"], "paused");
    assert!(runtime_command(&mut runtime, at, "play", None).ok);
    assert!(state.lock().unwrap().motion_active);
}

#[test]
fn failed_mirrored_battery_pause_resume_releases_successful_outputs() {
    use wall_in_one_service::power::PowerSource::{Ac, Battery};
    let document = independent_config()
        .replace(
            "display_mode = \"independent\"",
            "display_mode = \"mirrored\"",
        )
        .replace(
            "theme_source_connector = \"DP-1\"",
            "theme_source_connector = \"\"",
        );
    let (mut runtime, state, at) = battery_runtime_document(&document);
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    assert!(runtime_command(&mut runtime, at, "pause", None).ok);
    runtime.observe_power(power_observation(Battery));
    runtime.observe_power(power_observation(Ac));
    assert!(state.lock().unwrap().active_outputs.is_empty());
    state.lock().unwrap().fail_applies_remaining = 1;

    let response = runtime_command(&mut runtime, at, "play", None);
    assert!(!response.ok, "the first output must fail");
    assert_eq!(status(&mut runtime, at)["playback_state"], "paused");
    assert!(
        state.lock().unwrap().active_outputs.is_empty(),
        "the successful second output must not animate under Paused status"
    );
    assert!(runtime_command(&mut runtime, at, "play", None).ok);
    assert_eq!(
        state.lock().unwrap().active_outputs,
        HashSet::from(["DP-1".into(), "HDMI-A-1".into()])
    );
}

#[test]
fn failed_independent_battery_pause_resume_restores_selected_still_residency() {
    use wall_in_one_service::power::PowerSource::{Ac, Battery};
    let (mut runtime, state, at) = battery_runtime(true);
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    assert!(runtime_command(&mut runtime, at, "pause", None).ok);
    runtime.observe_power(power_observation(Battery));
    runtime.observe_power(power_observation(Ac));
    assert!(state.lock().unwrap().active_outputs.is_empty());
    {
        let mut driver = state.lock().unwrap();
        driver.fail_motion_attempts_remaining = 1;
        driver.stage_events.clear();
    }

    let response = runtime_command(&mut runtime, at, "on", Some("DP-1 play"));
    assert!(!response.ok, "the motion stage must fail");
    assert!(response.message.contains("previous wallpaper restored"));
    assert_eq!(
        display_status(&mut runtime, at, "DP-1")["playback_state"],
        "paused"
    );
    assert!(
        state.lock().unwrap().active_outputs.is_empty(),
        "rollback must preserve the released still-only renderer state"
    );
    assert!(
        state
            .lock()
            .unwrap()
            .stage_events
            .iter()
            .all(|event| !event.contains("HDMI-A-1"))
    );
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 play")).ok);
    assert_eq!(
        state.lock().unwrap().active_outputs,
        HashSet::from(["DP-1".into()])
    );
    // The failed targeted command must also retain the other paused route's
    // released marker, so its later Play performs a real launch.
    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 play")).ok);
    assert_eq!(
        state.lock().unwrap().active_outputs,
        HashSet::from(["DP-1".into(), "HDMI-A-1".into()])
    );
}

#[test]
fn independent_resume_starts_a_fresh_cycle_interval() {
    let (mut runtime, _state, at) = battery_runtime(true);
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "cycle", Some("on")).ok);
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 pause")).ok);
    let before = display_status(&mut runtime, at, "DP-1")["entry_id"].clone();
    // Make the old and resumed deadlines observably different without waiting
    // a whole five-minute cycle. The synthetic tick falls between them.
    thread::sleep(Duration::from_millis(40));
    let resumed = Instant::now();
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 play")).ok);
    runtime.tick(
        at,
        resumed + Duration::from_secs(300) - Duration::from_millis(20),
    );
    assert_eq!(display_status(&mut runtime, at, "DP-1")["entry_id"], before);
    runtime.tick(at, resumed + Duration::from_secs(301));
    assert_ne!(display_status(&mut runtime, at, "DP-1")["entry_id"], before);
}

#[test]
fn battery_still_cycling_and_hotplug_keep_all_motion_released() {
    use wall_in_one_service::power::PowerSource::Battery;
    let (mut runtime, state, at) = battery_runtime(true);
    runtime.observe_power(power_observation(Battery));
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "cycle", Some("on")).ok);
    let before = display_status(&mut runtime, at, "DP-1")["entry_id"].clone();
    let now = Instant::now();
    runtime.tick(at, now + Duration::from_secs(301));
    assert_ne!(display_status(&mut runtime, at, "DP-1")["entry_id"], before);
    assert!(state.lock().unwrap().active_outputs.is_empty());
    state
        .lock()
        .unwrap()
        .connected_outputs
        .as_mut()
        .unwrap()
        .push("DP-2".into());
    runtime.tick(at, now + Duration::from_secs(307));
    assert_eq!(display_status(&mut runtime, at, "DP-2")["connected"], true);
    assert!(
        state
            .lock()
            .unwrap()
            .applies
            .iter()
            .all(|(_, motion)| !motion)
    );
    assert_eq!(status(&mut runtime, at)["renderer_failed"], false);
}

#[test]
fn battery_replug_respects_reloaded_dynamics_and_option_changes() {
    use wall_in_one_service::power::PowerSource::{Ac, Battery};
    let root = directory("battery-reload");
    let path = root.join("runtime.toml");
    let document = config(Path::new("/bin/true"), Path::new("/bin/true"), true)
        .replace("schema_version = 4", "schema_version = 5")
        .replace(
            "dynamics_enabled = true",
            "dynamics_enabled = true\nstop_animations_on_battery = true",
        );
    fs::write(&path, &document).unwrap();
    let parsed: Config = toml::from_str(&document).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(path.clone(), parsed, RuntimeDriver(state.clone()), at).unwrap();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    runtime.observe_power(power_observation(Battery));
    fs::write(
        &path,
        document.replace("dynamics_enabled = true", "dynamics_enabled = false"),
    )
    .unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    runtime.observe_power(power_observation(Ac));
    assert!(!state.lock().unwrap().motion_active);
    runtime.observe_power(power_observation(Battery));
    // Re-enable animations while disabling just the battery option: ordinary
    // intent resumes without an AC event, and no saved transport was rewritten.
    fs::write(
        &path,
        document.replace(
            "stop_animations_on_battery = true",
            "stop_animations_on_battery = false",
        ),
    )
    .unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    runtime.observe_power(power_observation(Battery));
    assert!(state.lock().unwrap().motion_active);
    assert_eq!(status(&mut runtime, at)["animations_inhibited"], false);
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn battery_ac_resume_failure_remains_retryable_without_quarantining() {
    use wall_in_one_service::power::PowerSource::{Ac, Battery};
    for independent in [false, true] {
        let (mut runtime, state, at) = battery_runtime(independent);
        runtime.apply_current().unwrap();
        assert!(runtime_command(&mut runtime, at, "next", None).ok);
        runtime.observe_power(power_observation(Battery));
        state.lock().unwrap().fail_apply = true;
        runtime.observe_power(power_observation(Ac));
        let failed = status(&mut runtime, at);
        assert_eq!(failed["renderer_failed"], true);
        assert!(failed["taboo_entries"].as_array().unwrap().is_empty());
        state.lock().unwrap().fail_apply = false;
        assert!(runtime_command(&mut runtime, at, "play", None).ok);
        assert!(state.lock().unwrap().motion_active);
    }
}

#[test]
fn battery_reload_samples_the_exact_candidate_before_enabling_motion() {
    use wall_in_one_service::power::PowerSource::Battery;
    let root = directory("battery-enable-preflight");
    let path = root.join("runtime.toml");
    let initial = config(Path::new("/bin/true"), Path::new("/bin/true"), true)
        .replace("dynamics_enabled = true", "dynamics_enabled = false");
    let parsed: Config = toml::from_str(&initial).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(path.clone(), parsed, RuntimeDriver(state.clone()), at).unwrap();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    let enabled = initial
        .replace("schema_version = 4", "schema_version = 5")
        .replace(
            "dynamics_enabled = false",
            "dynamics_enabled = true\nstop_animations_on_battery = true",
        );
    fs::write(&path, enabled).unwrap();
    let mut sampled = false;
    let response = runtime.handle_with_power(
        wall_in_one_service::protocol::Request {
            verb: "reload".into(),
            argument: None,
        },
        at,
        |enabled| {
            assert!(enabled);
            sampled = true;
            power_observation(Battery)
        },
    );
    assert!(response.ok, "{}", response.message);
    assert!(sampled);
    assert!(
        state
            .lock()
            .unwrap()
            .applies
            .iter()
            .all(|(_, motion)| !motion)
    );
    assert_eq!(status(&mut runtime, at)["animations_inhibited"], true);
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn failed_battery_enable_restores_paused_renderer_residency() {
    use wall_in_one_service::power::PowerSource::Battery;
    let root = directory("battery-rollback");
    let path = root.join("runtime.toml");
    let initial = config(Path::new("/bin/true"), Path::new("/bin/true"), true);
    let parsed: Config = toml::from_str(&initial).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(path.clone(), parsed, RuntimeDriver(state.clone()), at).unwrap();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    assert!(runtime_command(&mut runtime, at, "pause", None).ok);
    assert!(state.lock().unwrap().motion_active);
    let candidate = initial
        .replace("schema_version = 4", "schema_version = 5")
        .replace(
            "dynamics_enabled = true",
            "dynamics_enabled = true\nstop_animations_on_battery = true",
        )
        .replace("layer = \"background\"", "layer = \"bottom\"");
    fs::write(&path, candidate).unwrap();
    state.lock().unwrap().fail_applies_remaining = 1;
    let response = runtime.handle_with_power(
        wall_in_one_service::protocol::Request {
            verb: "reload".into(),
            argument: None,
        },
        at,
        |_| power_observation(Battery),
    );
    assert!(!response.ok, "candidate must fail: {}", response.message);
    assert_eq!(status(&mut runtime, at)["playback_state"], "paused");
    assert!(
        state.lock().unwrap().motion_active,
        "rejected battery enable must restore the resident paused renderer"
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
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
    assert!(
        snapshot["schedules"]
            .as_array()
            .unwrap()
            .iter()
            .any(|rule| rule["id"] == "global-now" && rule["connector"].is_null())
    );
    assert!(
        snapshot["schedules"]
            .as_array()
            .unwrap()
            .iter()
            .any(|rule| rule["id"] == "dp-now" && rule["connector"] == "DP-1")
    );

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
    assert_eq!(
        snapshot["supported_config_schemas"],
        serde_json::json!([4, 5, 6, 7, 8])
    );
    assert_eq!(snapshot["runtime_version"], env!("CARGO_PKG_VERSION"));
    assert_eq!(snapshot["install_kind"], wall_in_one_service::INSTALL_KIND);
    assert_eq!(
        snapshot["runtime_executable"],
        std::env::current_exe().unwrap().to_str().unwrap()
    );
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
    assert!(
        events
            .iter()
            .any(|event| event == "still HDMI-A-1 video-two")
    );
    assert!(
        events
            .iter()
            .any(|event| event == "motion HDMI-A-1 video-two")
    );
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
    assert!(
        hdmi["last_error"]
            .as_str()
            .unwrap()
            .contains("previous wallpaper restored")
    );
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
    assert!(
        state
            .lock()
            .unwrap()
            .stage_events
            .iter()
            .all(|event| !event.contains("HDMI-A-1"))
    );
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
    let response = runtime_command(&mut runtime, at, "reload", None);
    assert!(response.ok, "{}", response.message);
    let independent = status(&mut runtime, at);
    assert_ne!(independent["source"], "manual");
    assert!(
        independent["displays"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["manual_override"] == false)
    );
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
    assert!(
        independent_again["displays"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["connected"] == true)
            .all(|row| row["manual_override"] == false)
    );
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
    assert_eq!(snapshot["renderer_failed"], true);
    assert_eq!(dp["playback_state"], "playing");
    assert_eq!(dp["motion_active"], true);
    assert_eq!(dp["renderer_failed"], false);
    assert_eq!(dp["last_error"], "");
    assert_eq!(hdmi["playback_state"], "playing");
    assert_eq!(hdmi["motion_active"], false);
    assert_eq!(hdmi["renderer_failed"], true);
    assert!(
        hdmi["last_error"]
            .as_str()
            .unwrap()
            .contains("mpvpaper crashed")
    );

    state.lock().unwrap().stage_events.clear();
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 play")).ok);
    assert!(
        state.lock().unwrap().stage_events.is_empty(),
        "Play on the healthy route must not reapply either wallpaper"
    );

    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 stop")).ok);
    state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
            entry_id: "video-two".into(),
            kind: wall_in_one_service::config::EntryKind::Video,
            scene_id: None,
            output: "HDMI-A-1".into(),
            message: "late child exit observed after explicit stop".into(),
            permanent_for_session: false,
        });
    runtime.tick(at, Instant::now());
    let explicitly_stopped = display_status(&mut runtime, at, "HDMI-A-1");
    assert_eq!(explicitly_stopped["playback_state"], "stopped");
    assert_eq!(explicitly_stopped["renderer_failed"], true);

    state.lock().unwrap().connected_outputs = Some(vec!["DP-1".into()]);
    runtime.tick(at, Instant::now() + Duration::from_secs(6));
    let detached_failure = status(&mut runtime, at);
    assert_eq!(detached_failure["renderer_failed"], false);
    let hdmi = detached_failure["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == "HDMI-A-1")
        .unwrap();
    assert_eq!(hdmi["connected"], false);
    assert_eq!(hdmi["renderer_failed"], true);
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
    assert!(
        independent_state
            .lock()
            .unwrap()
            .stage_events
            .iter()
            .all(|event| !event.starts_with("palette "))
    );

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
        assert!(
            !detached["assigned_playlist_id"]
                .as_str()
                .unwrap()
                .is_empty()
        );
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
    assert!(
        status(&mut runtime, at)["displays"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["playback_state"] == "paused")
    );
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 stop")).ok);
    let mixed = status(&mut runtime, at);
    assert!(
        mixed["displays"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["playback_state"] == "stopped")
    );
    assert!(
        mixed["displays"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["playback_state"] == "paused")
    );
    assert!(runtime_command(&mut runtime, at, "toggle", None).ok);
    assert!(
        status(&mut runtime, at)["displays"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["playback_state"] == "playing")
    );

    assert!(runtime_command(&mut runtime, at, "stop", None).ok);
    assert!(
        status(&mut runtime, at)["displays"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["playback_state"] == "stopped")
    );
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
            beats_global_rules: false,
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

/// Codex 0.2.1b H-6: a hand-made but valid runtime document at the schema's
/// ceilings -- 64 live and 64 detached displays, 64 taboo records, and
/// identifiers and text full of quotes, each of which the reply escapes
/// twice. The status budget does not count the taboo records' text.
fn status_ceiling_document() -> String {
    let quotes = |count: usize| "\\\"".repeat(count);
    let big = format!("{}BIG", quotes(210));
    let mut document = String::from(
        r#"schema_version = 4
config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
default_playlist = "BIG"
[settings]
cycle_interval_seconds = 300
cycle_enabled = false
shuffle = false
dynamics_enabled = false
display_mode = "independent"
theme_source_connector = "THEME"
[renderer]
noctalia_program = "/bin/true"
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
"#,
    )
    .replace("THEME", &format!("{}D00", quotes(253)))
    .replace("BIG", &big);
    document.push_str(&format!(
        "[[playlists]]\nid = \"{big}\"\nname = \"{}Big\"\n\
         [[playlists.entries]]\nid = \"{}\"\nkind = \"still\"\nstill = \"/tmp/s.png\"\n\
         palette = {{ kind = \"keep\", mode = \"keep\" }}\n",
        quotes(117),
        quotes(256),
    ));
    for index in 0..64 {
        document.push_str(&format!(
            "[[playlists]]\nid = \"t{index:02}{}\"\nname = \"{}{index:04}\"\n\
             [[playlists.entries]]\nid = \"{}\"\nkind = \"still\"\nstill = \"/tmp/t.png\"\n\
             taboo = {{ reason = \"{}\", source = \"{}\" }}\n\
             palette = {{ kind = \"keep\", mode = \"keep\" }}\n",
            quotes(100),
            quotes(116),
            quotes(256),
            quotes(512),
            quotes(64),
        ));
    }
    for index in 0..64 {
        document.push_str(&format!(
            "[[displays]]\nconnector = \"{}D{index:02}\"\nplaylist = \"{big}\"\n",
            quotes(253),
        ));
    }
    document
}

#[test]
fn a_status_over_the_wire_budget_leaves_out_taboo_records_instead_of_failing() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let parsed = Config::from_bytes(status_ceiling_document().into_bytes()).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(
            (0..64)
                .map(|index| format!("{}L{index:02}", "\"".repeat(253)))
                .collect(),
        ),
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
    runtime.tick(at, Instant::now());

    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "status".into(),
            argument: None,
        },
        at,
    );
    assert!(response.ok, "{}", response.message);
    let mut encoded = Vec::new();
    write_response(&mut encoded, &response).unwrap();
    assert!(
        encoded.len() <= MAX_RESPONSE_BYTES,
        "{} bytes",
        encoded.len()
    );
    let snapshot: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    // Degraded within the status's own vocabulary: the newest taboo records,
    // and the count of those left out; every other part whole.
    let kept = snapshot["taboo_entries"].as_array().unwrap().len();
    let omitted = snapshot["taboo_entries_omitted"].as_u64().unwrap() as usize;
    assert!(omitted > 0, "{} bytes", encoded.len());
    assert_eq!(kept + omitted, 64);
    assert!(kept > 0, "only as many left out as the budget needs");
    assert_eq!(snapshot["displays"].as_array().unwrap().len(), 128);
    assert_eq!(snapshot["playlists"].as_array().unwrap().len(), 65);
    for key in [
        "status_version",
        "playlist_id",
        "playback_state",
        "entry_taboo",
        "theme_source",
        "schedule",
        "schedules",
        "last_error",
    ] {
        assert!(snapshot.get(key).is_some(), "{key}");
    }
    // Room for one more record would have pushed it over.
    assert!(
        encoded.len() + 2_000 > MAX_RESPONSE_BYTES - 40_000,
        "left out more than needed: {} bytes",
        encoded.len()
    );
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
    assert!(
        taboo["taboo_entries"][0]["reason"]
            .as_str()
            .unwrap()
            .contains("borked")
    );
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
    let retrying = status(&mut runtime, at);
    assert_eq!(retrying["automatic_retry"]["attempt"], 2);
    assert_eq!(retrying["renderer_failed"], true);
    assert_eq!(retrying["motion_active"], false);
    runtime.tick(at, started + Duration::from_secs(4));
    let quarantined = status(&mut runtime, at);
    assert_eq!(quarantined["taboo_entries"][0]["entry_id"], "still-one");
    assert_eq!(quarantined["automatic_retry"]["attempt"], 0);
    assert_eq!(quarantined["renderer_failed"], true);
    runtime.tick(at, started + Duration::from_secs(6));
    let recovered = status(&mut runtime, at);
    assert_eq!(recovered["entry_id"], "video-two");
    assert_eq!(recovered["motion_active"], true);
    assert_eq!(recovered["renderer_failed"], false);
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
    let retrying = display_status(&mut runtime, at, "DP-1");
    assert_eq!(retrying["automatic_retry"]["attempt"], 2);
    assert_eq!(retrying["renderer_failed"], true);
    assert_eq!(retrying["motion_active"], false);
    assert_eq!(
        display_status(&mut runtime, at, "HDMI-A-1")["renderer_failed"],
        false
    );
    runtime.tick(at, started + Duration::from_secs(4));
    let quarantined = status(&mut runtime, at);
    assert_eq!(quarantined["taboo_entries"][0]["entry_id"], "still-one");
    assert_eq!(quarantined["displays"][0]["automatic_retry"]["attempt"], 0);
    assert_eq!(quarantined["displays"][0]["renderer_failed"], true);
    runtime.tick(at, started + Duration::from_secs(6));
    let recovered = status(&mut runtime, at);
    assert_eq!(recovered["displays"][0]["entry_id"], "video-two");
    assert_eq!(recovered["displays"][0]["motion_active"], true);
    assert_eq!(recovered["displays"][0]["renderer_failed"], false);
    assert!(
        state
            .lock()
            .unwrap()
            .stage_events
            .iter()
            .all(|event| !event.contains("HDMI-A-1"))
    );
}

#[test]
fn mirrored_automatic_retry_keeps_renderer_failed_when_rollback_fails() {
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
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    state.lock().unwrap().fail_apply = true;

    runtime.tick(at, Instant::now() + Duration::from_secs(600));

    let failed = status(&mut runtime, at);
    assert_eq!(failed["entry_id"], "still-one");
    assert_eq!(failed["automatic_retry"]["attempt"], 1);
    assert_eq!(failed["renderer_failed"], true);
    assert_eq!(failed["motion_active"], false);
    assert!(
        failed["last_error"]
            .as_str()
            .unwrap()
            .contains("could not restore the previous wallpaper")
    );
}

#[test]
fn independent_automatic_retry_keeps_renderer_failed_when_rollback_fails() {
    let document = independent_config()
        .replace("cycle_interval_seconds = 300", "cycle_interval_seconds = 5")
        .replace("cycle_enabled = false", "cycle_enabled = true");
    let parsed: Config = toml::from_str(&document).unwrap();
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
    state.lock().unwrap().fail_apply = true;

    runtime.tick(at, Instant::now() + Duration::from_secs(600));

    let failed = display_status(&mut runtime, at, "DP-1");
    assert_eq!(failed["entry_id"], "still-one");
    assert_eq!(failed["automatic_retry"]["attempt"], 1);
    assert_eq!(failed["renderer_failed"], true);
    assert_eq!(failed["motion_active"], false);
    assert!(
        failed["last_error"]
            .as_str()
            .unwrap()
            .contains("could not restore the previous wallpaper")
    );
    assert_eq!(status(&mut runtime, at)["renderer_failed"], true);
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
    assert_eq!(snapshot["playback_state"], "playing");
    assert_eq!(snapshot["renderer_failed"], true);
    assert_eq!(snapshot["taboo_entries"][0]["entry_id"], "scene-three");
    assert_eq!(snapshot["taboo_entries"][0]["scene_id"], "12345");
    assert_eq!(snapshot["taboo_entries"][0]["source"], "renderer-crash");
    assert!(
        snapshot["last_error"]
            .as_str()
            .unwrap()
            .contains("linux-wallpaperengine")
    );
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
    assert!(
        instance
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
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
    let after_reload = status(&mut runtime, at);
    assert_eq!(after_reload["runtime_instance"], instance);
    assert_eq!(after_reload["config_epoch"], 2);
    assert_eq!(after_reload["taboo_entries"][0]["entry_id"], "video-two");
    assert_eq!(
        after_reload["taboo_entries"][0]["observed_config_epoch"], 2,
        "a finding for unchanged resolved media must be attributable to the adopted config"
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
fn reload_reconciles_session_findings_by_resolved_media_identity() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    for (label, updated, expected_entry) in [
        (
            "rebound",
            ("motion = \"/tmp/two.mp4\"", "motion = \"/tmp/rebound.mp4\""),
            None,
        ),
        (
            "moved",
            ("id = \"video-two\"", "id = \"video-moved\""),
            Some("video-moved"),
        ),
    ] {
        let root = directory(&format!("taboo-identity-{label}"));
        let config_path = root.join("runtime.toml");
        let original = config(Path::new("/bin/true"), Path::new("/bin/true"), false);
        fs::write(&config_path, &original).unwrap();
        let parsed = Config::load(&config_path).unwrap();
        let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
        let mut runtime = Runtime::new(
            config_path.clone(),
            parsed,
            RuntimeDriver(state.clone()),
            at,
        )
        .unwrap();
        runtime.apply_current().unwrap();
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
                message: "video-two crashed and its still is active".into(),
                permanent_for_session: true,
            });
        runtime.tick(at, Instant::now());
        assert_eq!(
            status(&mut runtime, at)["taboo_entries"][0]["entry_id"],
            "video-two"
        );

        fs::write(&config_path, original.replace(updated.0, updated.1)).unwrap();
        assert!(runtime_command(&mut runtime, at, "reload", None).ok);
        let snapshot = status(&mut runtime, at);
        assert_eq!(snapshot["config_epoch"], 2);
        let records = snapshot["taboo_entries"].as_array().unwrap();
        if let Some(expected_entry) = expected_entry {
            assert_eq!(records.len(), 1);
            assert_eq!(records[0]["entry_id"], expected_entry);
            assert_eq!(records[0]["observed_config_epoch"], 2);
            assert_eq!(records[0]["durable"], false);
        } else {
            assert!(
                records.is_empty(),
                "an entry id rebound to different resolved media must not inherit its old taboo"
            );
        }
        fs::remove_dir_all(root).unwrap();
    }
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

    let applies_before = state.lock().unwrap().applies.len();
    for playlist in ["scene-copy-list", "video-copy-list"] {
        let refused = runtime_command(&mut runtime, at, "playlist-use", Some(playlist));
        assert!(!refused.ok, "{playlist} unexpectedly bypassed quarantine");
        assert!(refused.message.contains("every entry is marked taboo"));
    }
    assert_eq!(state.lock().unwrap().applies.len(), applies_before);
    assert_eq!(status(&mut runtime, at)["playlist_id"], "day");
}

#[test]
fn manual_navigation_and_playlist_routing_skip_taboo_entries_in_both_display_modes() {
    let summer = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let winter = NaiveDate::from_ymd_opt(2026, 12, 3)
        .unwrap()
        .and_hms_opt(23, 0, 0)
        .unwrap();
    for independent in [false, true] {
        let mut parsed: Config = toml::from_str(
            &(if independent {
                independent_config()
            } else {
                config(Path::new("/bin/true"), Path::new("/bin/true"), false)
            })
            .replace(
                "motion = \"/tmp/two.mp4\"",
                "motion = \"/tmp/two.mp4\"\n\
                 taboo = { reason = \"known decoder failure\", source = \"authoring\" }",
            )
            .replace(
                "scene_id = \"12345\"",
                "scene_id = \"12345\"\n\
                 taboo = { reason = \"known scene crash\", source = \"authoring\" }",
            ),
        )
        .unwrap();
        let mut safe = parsed.playlists[0].entries[0].clone();
        safe.id = "still-safe".into();
        safe.still = "/tmp/safe.png".into();
        parsed.playlists[0].entries.push(safe);
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

        let command = |runtime: &mut Runtime<RuntimeDriver>, verb: &str| {
            if independent {
                runtime_command(runtime, summer, "on", Some(&format!("DP-1 {verb}")))
            } else {
                runtime_command(runtime, summer, verb, None)
            }
        };
        assert!(command(&mut runtime, "next").ok);
        assert_eq!(status(&mut runtime, summer)["entry_id"], "still-safe");
        assert!(command(&mut runtime, "previous").ok);
        assert_eq!(status(&mut runtime, summer)["entry_id"], "still-one");
        assert!(command(&mut runtime, "random").ok);
        assert_eq!(status(&mut runtime, summer)["entry_id"], "still-safe");
        assert!(
            state
                .lock()
                .unwrap()
                .applies
                .iter()
                .all(|(entry, _)| entry != "video-two")
        );

        let applies_before = state.lock().unwrap().applies.len();
        let refused_playlist = if independent {
            runtime_command(&mut runtime, summer, "on", Some("DP-1 playlist-use night"))
        } else {
            runtime_command(&mut runtime, summer, "playlist-use", Some("night"))
        };
        assert!(!refused_playlist.ok);
        assert!(
            refused_playlist
                .message
                .contains("every entry is marked taboo")
        );
        assert_eq!(status(&mut runtime, summer)["playlist_id"], "day");

        let following = if independent {
            runtime_command(&mut runtime, winter, "on", Some("DP-1 schedule-follow"))
        } else {
            runtime_command(&mut runtime, winter, "schedule-follow", None)
        };
        assert!(!following.ok);
        assert!(following.message.contains("every entry is marked taboo"));
        assert_eq!(status(&mut runtime, winter)["playlist_id"], "day");
        assert_eq!(state.lock().unwrap().applies.len(), applies_before);
    }
}

#[test]
fn shuffled_random_escapes_a_taboo_current_entry_when_only_one_safe_item_remains() {
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
        .replace("shuffle = false", "shuffle = true");
        let parsed: Config = toml::from_str(&document).unwrap();
        let output = if independent { "DP-1" } else { "" };
        let state = Arc::new(Mutex::new(RuntimeDriverState {
            connected_outputs: independent.then(|| vec!["DP-1".into(), "HDMI-A-1".into()]),
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
        assert_eq!(status(&mut runtime, at)["entry_id"], "video-two");
        state
            .lock()
            .unwrap()
            .renderer_failures
            .push(RendererFailure {
                entry_id: "video-two".into(),
                kind: wall_in_one_service::config::EntryKind::Video,
                scene_id: None,
                output: output.into(),
                message: "decoder permanently rejected this video".into(),
                permanent_for_session: true,
            });
        runtime.tick(at, Instant::now());

        let taboo = status(&mut runtime, at);
        assert_eq!(taboo["entry_taboo"], true);
        assert!(
            taboo["displays"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|display| display["connected"] == true)
                .all(|display| display["entry_taboo"] == true)
        );
        if independent {
            let healthy_occurrence = taboo["displays"]
                .as_array()
                .unwrap()
                .iter()
                .find(|display| display["connector"] == "HDMI-A-1")
                .unwrap();
            assert_eq!(healthy_occurrence["renderer_failed"], false);
            assert_eq!(healthy_occurrence["entry_taboo"], true);

            let quick_choice =
                runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 playlist-use day"));
            assert!(
                quick_choice.ok,
                "same-playlist Quick Choice did not escape an equivalent taboo occurrence: {quick_choice:?}"
            );
            let after_choice = display_status(&mut runtime, at, "HDMI-A-1");
            assert_eq!(after_choice["entry_id"], "still-one");
            assert_eq!(after_choice["entry_taboo"], false);
        }

        let escaped = runtime_command(&mut runtime, at, "random", None);
        assert!(
            escaped.ok,
            "Random did not escape the taboo shuffle tail: {escaped:?}"
        );
        let safe = status(&mut runtime, at);
        assert_eq!(safe["entry_id"], "still-one");
        assert_eq!(safe["entry_taboo"], false);
    }
}

#[test]
fn mirrored_schedule_follow_preflights_and_resets_effective_display_assignments() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let mut parsed: Config = toml::from_str(&format!(
        "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"night\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    ))
    .unwrap();
    parsed.schedules.clear();
    let mut scene_copy = parsed.playlists[1].entries[0].clone();
    scene_copy.id = "scene-copy".into();
    parsed.playlists[0].entries.push(scene_copy);
    let mut safe = parsed.playlists[0].entries[0].clone();
    safe.id = "night-safe".into();
    safe.still = "/tmp/night-safe.png".into();
    parsed.playlists[1].entries.push(safe);
    parsed.validate().unwrap();
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
    assert_eq!(status(&mut runtime, at)["entry_id"], "scene-three");

    assert!(runtime_command(&mut runtime, at, "playlist-use", Some("day")).ok);
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    assert_eq!(status(&mut runtime, at)["entry_id"], "scene-copy");
    state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
            entry_id: "scene-copy".into(),
            kind: wall_in_one_service::config::EntryKind::Scene,
            scene_id: Some("12345".into()),
            output: "DP-1".into(),
            message: "scene engine crashed on the manual occurrence".into(),
            permanent_for_session: true,
        });
    runtime.tick(at, Instant::now());

    let following = runtime_command(&mut runtime, at, "schedule-follow", None);
    assert!(
        following.ok,
        "safe display assignment was not selected: {following:?}"
    );
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["source"], "schedule");
    assert_eq!(snapshot["displays"][0]["playlist_id"], "night");
    assert_eq!(snapshot["displays"][0]["entry_id"], "night-safe");
    assert_eq!(snapshot["displays"][0]["entry_taboo"], false);
    assert_eq!(
        state.lock().unwrap().applies.last().unwrap().0,
        "night-safe"
    );
}

#[test]
fn previous_discards_a_newly_taboo_history_entry_without_losing_the_safe_forward_path() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    for independent in [false, true] {
        let document = if independent {
            independent_config()
        } else {
            config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        };
        let mut parsed: Config = toml::from_str(&document).unwrap();
        parsed.schedules.clear();
        let mut safe = parsed.playlists[0].entries[0].clone();
        safe.id = "still-safe".into();
        safe.still = "/tmp/safe.png".into();
        parsed.playlists[0].entries.push(safe);
        let output = if independent { "DP-1" } else { "" };
        let state = Arc::new(Mutex::new(RuntimeDriverState {
            connected_outputs: independent.then(|| vec![output.into()]),
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
        let command = |runtime: &mut Runtime<RuntimeDriver>, verb: &str| {
            if independent {
                runtime_command(runtime, at, "on", Some(&format!("DP-1 {verb}")))
            } else {
                runtime_command(runtime, at, verb, None)
            }
        };
        assert!(command(&mut runtime, "next").ok);
        assert_eq!(status(&mut runtime, at)["entry_id"], "video-two");
        assert!(command(&mut runtime, "next").ok);
        assert_eq!(status(&mut runtime, at)["entry_id"], "still-safe");

        state
            .lock()
            .unwrap()
            .renderer_failures
            .push(RendererFailure {
                entry_id: "video-two".into(),
                kind: wall_in_one_service::config::EntryKind::Video,
                scene_id: None,
                output: output.into(),
                message: "late decoder crash from the previous wallpaper".into(),
                permanent_for_session: true,
            });
        runtime.tick(at, Instant::now());

        assert!(command(&mut runtime, "previous").ok);
        assert_eq!(status(&mut runtime, at)["entry_id"], "still-one");
        assert!(command(&mut runtime, "next").ok);
        assert_eq!(status(&mut runtime, at)["entry_id"], "still-safe");
    }
}

#[test]
fn all_taboo_startup_keeps_a_static_fallback_but_manual_play_is_refused() {
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
            "still = \"/tmp/one.png\"",
            "still = \"/tmp/one.png\"\n\
             taboo = { reason = \"known image failure\", source = \"authoring\" }",
        )
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
            RuntimeDriver(state.clone()),
            at,
        )
        .unwrap();
        runtime.apply_current().unwrap();
        let applies_before = state.lock().unwrap().applies.len();
        let row = if independent {
            display_status(&mut runtime, at, "DP-1")
        } else {
            status(&mut runtime, at)["displays"][0].clone()
        };
        assert_eq!(row["entry_id"], "still-one");
        assert_eq!(row["playback_state"], "playing");
        assert_eq!(row["motion_active"], false);
        assert_eq!(row["renderer_failed"], true);
        let diagnostic = row["last_error"].as_str().unwrap();
        assert!(diagnostic.contains("still-one"), "{diagnostic}");
        assert!(diagnostic.contains("known image failure"), "{diagnostic}");
        assert!(diagnostic.contains("paired still"), "{diagnostic}");

        let next = if independent {
            runtime_command(&mut runtime, at, "on", Some("DP-1 next"))
        } else {
            runtime_command(&mut runtime, at, "next", None)
        };
        assert!(!next.ok);
        assert!(next.message.contains("empty or taboo"));
        let still_selected = if independent {
            display_status(&mut runtime, at, "DP-1")
        } else {
            status(&mut runtime, at)["displays"][0].clone()
        };
        assert_eq!(still_selected["entry_id"], "still-one");

        let play = if independent {
            runtime_command(&mut runtime, at, "on", Some("DP-1 play"))
        } else {
            runtime_command(&mut runtime, at, "play", None)
        };
        assert!(!play.ok, "taboo Play unexpectedly succeeded");
        assert!(play.message.contains("marked taboo (Borked)"));
        assert_eq!(state.lock().unwrap().applies.len(), applies_before);
        assert!(
            status(&mut runtime, at)["taboo_entries"]
                .as_array()
                .unwrap()
                .iter()
                .any(|entry| entry["entry_id"] == "still-one")
        );
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

    let mut first_round = vec![
        status(&mut runtime, at)["entry_id"]
            .as_str()
            .unwrap()
            .to_string(),
    ];
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
    // Deliberately changed with schema 8 (r13): an empty playlist below 8 is
    // what 0.4.0's compiler wrote by mistake for an empty All media, never an
    // intention, so the load now refuses it by name (before, the apply did,
    // with "previous configuration restored"). Either way the reload is
    // refused and the previous configuration keeps playing, as asserted
    // below. At schema 8 the same playlist, with its reason, plays nothing:
    // `a_playlist_that_plays_nothing_is_a_state_play_and_toggle_cant_leave`.
    fs::write(&config_path, empty_day).unwrap();
    let refused = Config::load(&config_path).unwrap_err().to_string();
    assert!(
        refused.contains("playlist \"Day\" has no entries")
            && refused.contains("requires schema_version 8"),
        "{refused}"
    );
    let before_rejected_reload = status(&mut runtime, at);
    let reply = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "reload".into(),
            argument: None,
        },
        at,
    );
    assert!(!reply.ok);
    assert!(
        reply.message.contains("has no entries"),
        "{}",
        reply.message
    );
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

/// Two outputs showing the day playlist's video, stopped, with DP-1 about
/// to refuse every apply while HDMI-A-1 still succeeds.
fn stopped_pair_with_one_failing_output(
    independent: bool,
) -> (
    Runtime<RuntimeDriver>,
    Arc<Mutex<RuntimeDriverState>>,
    chrono::NaiveDateTime,
) {
    let document = if independent {
        independent_config()
    } else {
        format!(
            "{}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"day\"\n\
             [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"day\"\n",
            config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        )
    };
    let at = noon();
    let (mut runtime, state) = started_runtime(loaded(&document, &[]), at);
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    let both: HashSet<String> = ["DP-1".to_string(), "HDMI-A-1".to_string()].into();
    assert_eq!(
        state.lock().unwrap().active_outputs,
        both,
        "both play the video"
    );
    assert!(runtime_command(&mut runtime, at, "stop", None).ok);
    assert!(state.lock().unwrap().active_outputs.is_empty());
    state.lock().unwrap().fail_apply_output = Some("DP-1".into());
    (runtime, state, at)
}

#[test]
fn a_partly_failed_resume_from_stopped_leaves_no_output_animating() {
    for independent in [false, true] {
        let (mut runtime, state, at) = stopped_pair_with_one_failing_output(independent);
        let response = runtime_command(&mut runtime, at, "play", None);
        assert!(!response.ok, "DP-1 refused, so Play reports failure");
        assert!(
            response.message.contains("renderer refused DP-1"),
            "{}",
            response.message
        );
        let snapshot = status(&mut runtime, at);
        assert_eq!(
            state.lock().unwrap().active_outputs,
            HashSet::new(),
            "independent={independent}: HDMI-A-1 started, then must be released"
        );
        assert_eq!(
            snapshot["motion_active"], false,
            "independent={independent}"
        );
        for row in snapshot["displays"].as_array().unwrap() {
            assert_eq!(row["playback_state"], "stopped", "{row}");
            assert_eq!(row["motion_active"], false, "{row}");
        }

        // Once DP-1 can start again, Play resumes both.
        state.lock().unwrap().fail_apply_output = None;
        assert!(runtime_command(&mut runtime, at, "play", None).ok);
        assert_eq!(state.lock().unwrap().active_outputs.len(), 2);
    }
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
    assert!(
        manual["displays"]
            .as_array()
            .unwrap()
            .iter()
            .all(|display| display["playlist_id"] == "day")
    );

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
    assert!(
        scheduled["displays"]
            .as_array()
            .unwrap()
            .iter()
            .all(|display| display["playlist_id"] == "night")
    );
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
fn independent_hotplug_keeps_surviving_renderers_and_quarantines_only_the_new_route() {
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
        toml::from_str::<Config>(&independent_config()).unwrap(),
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "next", None).ok);
    assert_eq!(
        state.lock().unwrap().active_outputs,
        HashSet::from(["DP-1".to_string(), "HDMI-A-1".to_string()])
    );

    {
        let mut recorded = state.lock().unwrap();
        recorded.stage_events.clear();
        recorded.applies.clear();
        recorded.applied_outputs.clear();
        recorded.connected_outputs = Some(vec!["DP-1".into(), "HDMI-A-1".into(), "USB-C-1".into()]);
        recorded.fail_stage_entry_id = Some("still-one".into());
        recorded.fail_stage_output = Some("USB-C-1".into());
        recorded.fail_stage_attempts_remaining = 3;
    }

    let base = Instant::now();
    runtime.tick(at, base + Duration::from_secs(6));
    {
        let recorded = state.lock().unwrap();
        assert!(
            recorded
                .stage_events
                .iter()
                .all(|event| { !event.contains("DP-1") && !event.contains("HDMI-A-1") })
        );
        assert!(recorded.active_outputs.contains("DP-1"));
        assert!(recorded.active_outputs.contains("HDMI-A-1"));
    }
    let first = display_status(&mut runtime, at, "USB-C-1");
    assert_eq!(first["automatic_retry"]["attempt"], 1);
    assert_eq!(first["automatic_retry"]["maximum_attempts"], 3);
    assert_eq!(first["automatic_retry"]["reason"], "hotplug");

    runtime.tick(at, base + Duration::from_secs(8));
    assert_eq!(
        display_status(&mut runtime, at, "USB-C-1")["automatic_retry"]["attempt"],
        2
    );
    runtime.tick(at, base + Duration::from_secs(10));
    let quarantined = status(&mut runtime, at);
    assert!(
        quarantined["taboo_entries"]
            .as_array()
            .unwrap()
            .iter()
            .any(|entry| entry["entry_id"] == "still-one")
    );
    assert_eq!(
        quarantined["displays"]
            .as_array()
            .unwrap()
            .iter()
            .find(|display| display["connector"] == "USB-C-1")
            .unwrap()["automatic_retry"]["attempt"],
        0
    );

    runtime.tick(at, base + Duration::from_secs(12));
    let recovered = display_status(&mut runtime, at, "USB-C-1");
    assert_eq!(recovered["entry_id"], "video-two");
    assert!(recovered["automatic_retry"].is_null());
    assert_eq!(recovered["playback_state"], "playing");
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
    assert!(
        scheduled["displays"]
            .as_array()
            .unwrap()
            .iter()
            .all(|display| display["playlist_id"] == "night")
    );

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
            assert!(
                snapshot["schedules"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|rule| rule["id"] == "same-global" && rule["in_force"] == true)
            );
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
    assert!(
        partial_status["output_discovery_error"]
            .as_str()
            .unwrap()
            .contains("no usable connectors")
    );

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
    assert!(
        snapshot["output_discovery_error"]
            .as_str()
            .unwrap()
            .contains("no usable connectors")
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

#[test]
fn independent_reload_of_inactive_schedule_and_detached_assignment_is_quiet() {
    let root = directory("quiet-independent-routing-reload");
    let config_path = root.join("runtime.toml");
    let original = independent_config();
    fs::write(&config_path, &original).unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        config_path.clone(),
        Config::load(&config_path).unwrap(),
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    state.lock().unwrap().stage_events.clear();

    let inactive_schedule_edit = original.replace("months = [12]", "months = [11]");
    fs::write(&config_path, &inactive_schedule_edit).unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    assert!(
        state.lock().unwrap().stage_events.is_empty(),
        "an inactive schedule edit restarted a live renderer"
    );

    let detached_assignment_edit = inactive_schedule_edit.replace(
        "connector = \"HDMI-A-1\"\nplaylist = \"day\"",
        "connector = \"HDMI-A-1\"\nplaylist = \"night\"",
    );
    fs::write(&config_path, detached_assignment_edit).unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    assert!(
        state.lock().unwrap().stage_events.is_empty(),
        "a detached assignment edit restarted a live renderer"
    );
    let detached = display_status(&mut runtime, at, "HDMI-A-1");
    assert_eq!(detached["connected"], false);
    assert_eq!(detached["assigned_playlist_id"], "night");
    assert_eq!(detached["playlist_id"], "night");
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn theme_source_only_reload_changes_only_the_palette_and_rolls_back_on_failure() {
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let original = independent_config().replace(
        "connector = \"HDMI-A-1\"\nplaylist = \"day\"",
        "connector = \"HDMI-A-1\"\nplaylist = \"night\"",
    );
    let updated = original.replace(
        "theme_source_connector = \"DP-1\"",
        "theme_source_connector = \"HDMI-A-1\"",
    );

    for fail in [false, true] {
        let root = directory(if fail {
            "theme-source-rollback"
        } else {
            "theme-source-palette-only"
        });
        let config_path = root.join("runtime.toml");
        fs::write(&config_path, &original).unwrap();
        let state = Arc::new(Mutex::new(RuntimeDriverState {
            connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
            ..RuntimeDriverState::default()
        }));
        let mut runtime = Runtime::new(
            config_path.clone(),
            Config::load(&config_path).unwrap(),
            RuntimeDriver(state.clone()),
            at,
        )
        .unwrap();
        runtime.apply_current().unwrap();
        {
            let mut recorded = state.lock().unwrap();
            recorded.stage_events.clear();
            if fail {
                recorded.fail_palette_attempts_remaining = 1;
            }
        }
        fs::write(&config_path, &updated).unwrap();

        let response = runtime_command(&mut runtime, at, "reload", None);
        let events = state.lock().unwrap().stage_events.clone();
        assert!(events.iter().all(|event| !event.starts_with("stop ")
            && !event.starts_with("still ")
            && !event.starts_with("motion ")));
        if fail {
            assert!(!response.ok);
            assert_eq!(events, ["palette scene-three", "palette still-one"]);
            let snapshot = status(&mut runtime, at);
            assert_eq!(snapshot["config_epoch"], 1);
            assert_eq!(snapshot["theme_source"]["configured"], "DP-1");
            assert_eq!(snapshot["theme_source"]["effective"], "DP-1");
        } else {
            assert!(response.ok, "{}", response.message);
            assert_eq!(events, ["palette scene-three"]);
            let snapshot = status(&mut runtime, at);
            assert_eq!(snapshot["config_epoch"], 2);
            assert_eq!(snapshot["theme_source"]["configured"], "HDMI-A-1");
            assert_eq!(snapshot["theme_source"]["effective"], "HDMI-A-1");
        }
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn failed_independent_reload_rolls_back_only_its_route_and_restores_the_old_palette() {
    let root = directory("scoped-independent-reload-rollback");
    let config_path = root.join("runtime.toml");
    let original = independent_config().replace(
        "theme_source_connector = \"DP-1\"",
        "theme_source_connector = \"HDMI-A-1\"",
    );
    let updated = original
        .replace(
            "connector = \"DP-1\"\nplaylist = \"day\"",
            "connector = \"DP-1\"\nplaylist = \"night\"",
        )
        .replace(
            "theme_source_connector = \"HDMI-A-1\"",
            "theme_source_connector = \"DP-1\"",
        );
    fs::write(&config_path, &original).unwrap();
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
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 next")).ok);
    assert_eq!(
        display_status(&mut runtime, at, "HDMI-A-1")["entry_id"],
        "video-two"
    );
    assert!(state.lock().unwrap().active_outputs.contains("HDMI-A-1"));
    {
        let mut recorded = state.lock().unwrap();
        recorded.stage_events.clear();
        // The candidate source palette is the last staged operation before the
        // simulated failure. Rollback must explicitly restore HDMI-A-1's old
        // palette without restarting HDMI-A-1's surviving renderer.
        recorded.fail_palette_attempts_remaining = 1;
    }
    fs::write(&config_path, updated).unwrap();

    let response = runtime_command(&mut runtime, at, "reload", None);
    assert!(!response.ok);
    assert!(response.message.contains("previous configuration restored"));
    let events = state.lock().unwrap().stage_events.clone();
    assert_eq!(
        events
            .iter()
            .filter(|event| event.as_str() == "stop DP-1")
            .count(),
        2,
        "the changed route needs one candidate hand-over and one rollback: {events:?}"
    );
    assert!(
        events.iter().all(|event| !event.contains("HDMI-A-1")),
        "the unchanged survivor was restarted during rollback: {events:?}"
    );
    assert_eq!(
        events,
        [
            "stop DP-1",
            "still DP-1 scene-three",
            "palette scene-three",
            "stop DP-1",
            "still DP-1 still-one",
            "motion DP-1 still-one",
            "palette video-two",
        ]
    );
    assert!(state.lock().unwrap().active_outputs.contains("HDMI-A-1"));

    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["config_epoch"], 1);
    assert_eq!(snapshot["theme_source"]["configured"], "HDMI-A-1");
    assert_eq!(snapshot["theme_source"]["effective"], "HDMI-A-1");
    let dp = display_status(&mut runtime, at, "DP-1");
    assert_eq!(dp["assigned_playlist_id"], "day");
    assert_eq!(dp["playlist_id"], "day");
    assert_eq!(dp["entry_id"], "still-one");
    let hdmi = display_status(&mut runtime, at, "HDMI-A-1");
    assert_eq!(hdmi["entry_id"], "video-two");
    assert_eq!(hdmi["motion_active"], true);
    assert_eq!(hdmi["renderer_failed"], false);
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
fn one_wallpapers_dials_hand_over_only_the_display_showing_it() {
    // DP-1 shows video-two, HDMI-A-1 shows video-four, each from its own playlist.
    let root = directory("dials-hand-over");
    let config_path = root.join("runtime.toml");
    let original = independent_config()
        .replace("schema_version = 4", "schema_version = 7")
        .replace(
            "[[playlists.entries]]\nid = \"still-one\"\nkind = \"still\"\nstill = \"/tmp/one.png\"\npalette = { kind = \"adaptive\", scheme = \"m3-tonal-spot\", mode = \"dark\" }\n",
            "",
        )
        .replace(
            "[[playlists]]\nid = \"night\"",
            "[[playlists]]\nid = \"evening\"\nname = \"Evening\"\n[[playlists.entries]]\nid = \"video-four\"\nkind = \"video\"\nstill = \"/tmp/four.png\"\nmotion = \"/tmp/four.mp4\"\npalette = { kind = \"keep\", mode = \"keep\" }\n[[playlists]]\nid = \"night\"",
        )
        .replace(
            "connector = \"HDMI-A-1\"\nplaylist = \"day\"",
            "connector = \"HDMI-A-1\"\nplaylist = \"evening\"",
        );
    fs::write(&config_path, &original).unwrap();
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
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let take = |state: &Arc<Mutex<RuntimeDriverState>>| {
        let mut recorded = state.lock().unwrap();
        // Which displays were handed over; a display may be applied in
        // more than one step.
        let mut outputs = std::mem::take(&mut recorded.applied_outputs);
        outputs.sort();
        outputs.dedup();
        let reconfigures = std::mem::take(&mut recorded.reconfigures);
        recorded.applies.clear();
        (outputs, reconfigures)
    };
    take(&state);

    // video-four's own dials: only HDMI-A-1's renderer is handed over.
    let four = original.replace(
        "motion = \"/tmp/four.mp4\"",
        "motion = \"/tmp/four.mp4\"\nvideo = { speed = 2.0 }",
    );
    fs::write(&config_path, &four).unwrap();
    let response = runtime_command(&mut runtime, at, "reload", None);
    assert!(response.ok, "{}", response.message);
    assert_eq!(take(&state), (vec!["HDMI-A-1".to_string()], 0));

    // video-two's: only DP-1's.
    let two = four.replace(
        "motion = \"/tmp/two.mp4\"",
        "motion = \"/tmp/two.mp4\"\nvideo = { fit = \"fit\" }",
    );
    fs::write(&config_path, &two).unwrap();
    let response = runtime_command(&mut runtime, at, "reload", None);
    assert!(response.ok, "{}", response.message);
    assert_eq!(take(&state), (vec!["DP-1".to_string()], 0));

    // The same dials again: nothing is handed over.
    fs::write(&config_path, format!("{two}\n")).unwrap();
    let response = runtime_command(&mut runtime, at, "reload", None);
    assert!(response.ok, "{}", response.message);
    assert_eq!(take(&state), (Vec::<String>::new(), 0));

    // A global dial is a renderer change: every display, after a reconfigure.
    let global = two.replace(
        "scene_clamp = \"\"\n",
        "scene_clamp = \"\"\nvideo_deband = true\n",
    );
    fs::write(&config_path, &global).unwrap();
    let response = runtime_command(&mut runtime, at, "reload", None);
    assert!(response.ok, "{}", response.message);
    assert_eq!(
        take(&state),
        (vec!["DP-1".to_string(), "HDMI-A-1".to_string()], 1)
    );
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

    let mut owner = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .stdout(Stdio::null())
            .spawn()
            .unwrap(),
    );
    assert_eq!(request(&socket, "status", None)["ok"], true);
    let before = fs::read_to_string(&events).unwrap();

    let contender = service_command()
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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );

    let failed = request(&socket, "status", None);
    let failed: serde_json::Value =
        serde_json::from_str(failed["message"].as_str().unwrap()).unwrap();
    assert!(
        failed["last_error"]
            .as_str()
            .unwrap()
            .contains("desktop bus is not ready yet")
    );
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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );
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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );
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
    assert!(
        fs::read_to_string(&events)
            .unwrap()
            .contains("msg wallpaper-set /tmp/one.png")
    );
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
fn initial_all_output_scene_waits_for_the_compositor_without_retrying_a_crashed_renderer() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("initial-compositor-readiness");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    // Where the compositor will listen once it has started: not yet.
    let wayland = root.join("wayland-0");
    let launches = root.join("launches");
    let noctalia = root.join("noctalia");
    let engine = root.join("linux-wallpaperengine");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::write(
        &engine,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {:?}\nprintf '%s\\n' 'scene renderer crashed once' >&2\nexit 42\n",
            launches
        ),
    )
    .unwrap();
    for executable in [&noctalia, &engine] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let document = config(&noctalia, &noctalia, true)
        .replace("default_playlist = \"day\"", "default_playlist = \"night\"")
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!("linux_wallpaperengine_program = {engine:?}"),
        );
    fs::write(&config_path, document).unwrap();
    let mut child = ServiceChild(
        service_command()
            .env("WAYLAND_DISPLAY", &wayland)
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );

    let failed = request(&socket, "status", None);
    let failed: serde_json::Value =
        serde_json::from_str(failed["message"].as_str().unwrap()).unwrap();
    assert!(
        failed["last_error"]
            .as_str()
            .unwrap()
            .contains("cannot connect to the Wayland compositor"),
        "{failed}"
    );

    let compositor = FakeCompositor::start(
        &wayland,
        Layout::of(vec![FakeOutput::new(
            "eDP-1",
            (1920, 1080, 60_000),
            (0, 0, 1920, 1080),
        )]),
    );
    // The launch line whole, not just the file: it was read half-written under load.
    let launched = recorded_lines(&launches, 1);
    assert!(
        !launched.is_empty(),
        "scene was not launched once the compositor was there"
    );
    assert!(launched.contains("--screen-root eDP-1"), "{launched}");
    let failure_deadline = Instant::now() + RECORD_DEADLINE;
    loop {
        let status = request(&socket, "status", None);
        let status: serde_json::Value =
            serde_json::from_str(status["message"].as_str().unwrap()).unwrap();
        if status["last_error"]
            .as_str()
            .unwrap()
            .contains("scene renderer crashed once")
        {
            assert_eq!(status["outputs"][0]["name"], "eDP-1", "{status}");
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
    drop(compositor);
    fs::remove_dir_all(root).unwrap();
}
/// `status`, or `None` when the service did not answer within `wait`.
fn status_within(socket: &Path, wait: Duration) -> Option<serde_json::Value> {
    let stream = UnixStream::connect(socket).ok()?;
    stream.set_read_timeout(Some(wait)).ok()?;
    stream.set_write_timeout(Some(wait)).ok()?;
    let mut writer = stream.try_clone().ok()?;
    writeln!(writer, "{{\"verb\":\"status\"}}").ok()?;
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line).ok()?;
    serde_json::from_str(&line).ok()
}

#[test]
fn a_compositor_that_stopped_accepting_never_stalls_the_service_or_its_shutdown() {
    use std::os::unix::fs::PermissionsExt;

    let root = directory("wedged-compositor");
    let wayland = root.join("wayland-0");
    let compositor = WedgedCompositor::start(&wayland);
    let noctalia = root.join("noctalia");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, config(&noctalia, &noctalia, false)).unwrap();
    let socket = root.join("runtime.sock");
    let mut child = ServiceChild(
        service_command()
            .env("WAYLAND_DISPLAY", &wayland)
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );
    let ready = Instant::now() + Duration::from_secs(10);
    while status_within(&socket, Duration::from_secs(5)).is_none() {
        assert!(Instant::now() < ready, "the service never answered");
        thread::sleep(Duration::from_millis(50));
    }

    // Long enough for several five-second probes, each of which may hold the
    // loop for one three-second question, and no longer.
    let started = Instant::now();
    let mut unanswered = Vec::new();
    let mut reported = String::new();
    while started.elapsed() < Duration::from_secs(22) {
        let asked = started.elapsed();
        match status_within(&socket, Duration::from_secs(4)) {
            Some(reply) => {
                let status: serde_json::Value =
                    serde_json::from_str(reply["message"].as_str().unwrap()).unwrap();
                reported = status["output_discovery_error"]
                    .as_str()
                    .unwrap()
                    .to_string();
            }
            None => unanswered.push(asked),
        }
        thread::sleep(Duration::from_millis(400));
    }

    unsafe { libc::kill(child.id() as i32, libc::SIGTERM) };
    let stopping = Instant::now();
    let stopped = loop {
        if child.try_wait().unwrap().is_some() {
            break true;
        }
        if stopping.elapsed() > Duration::from_secs(8) {
            break false;
        }
        thread::sleep(Duration::from_millis(50));
    };
    if !stopped {
        // Release it so the test can clean up, then fail.
        drop(compositor);
        let _ = child.kill();
        let _ = child.wait();
        panic!("SIGTERM did not stop the service within 8 s");
    }
    drop(compositor);
    fs::remove_dir_all(root).unwrap();
    assert!(
        unanswered.is_empty(),
        "status went unanswered at {unanswered:?}"
    );
    assert!(
        reported.starts_with("cannot connect to the Wayland compositor")
            || reported.contains("did not answer within"),
        "{reported}"
    );
}

#[test]
fn a_compositor_going_away_writes_nothing_unprefixed_to_stderr() {
    use std::io::Read;
    use std::os::unix::fs::PermissionsExt;

    let root = directory("compositor-goes-away");
    let wayland = root.join("wayland-0");
    let compositor = FakeCompositor::start(
        &wayland,
        Layout::of(vec![FakeOutput::new(
            "eDP-1",
            (1920, 1080, 60_000),
            (0, 0, 1920, 1080),
        )]),
    );
    let noctalia = root.join("noctalia");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, config(&noctalia, &noctalia, false)).unwrap();
    let socket = root.join("runtime.sock");
    let mut child = ServiceChild(
        service_command()
            .env("WAYLAND_DISPLAY", &wayland)
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let status_now = || -> serde_json::Value {
        let reply = request(&socket, "status", None);
        serde_json::from_str(reply["message"].as_str().unwrap()).unwrap()
    };
    let deadline = Instant::now() + Duration::from_secs(10);
    while status_now()["outputs"][0]["name"] != "eDP-1" {
        assert!(
            Instant::now() < deadline,
            "the compositor's output was never published"
        );
        thread::sleep(Duration::from_millis(50));
    }

    // The compositor exits under the connection; the next probe finds it gone.
    drop(compositor);
    let deadline = Instant::now() + Duration::from_secs(12);
    while status_now()["output_discovery_error"] == "" {
        assert!(
            Instant::now() < deadline,
            "the lost compositor was never noticed"
        );
        thread::sleep(Duration::from_millis(100));
    }
    stop(&mut child, &socket);
    let mut written = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut written)
        .unwrap();
    fs::remove_dir_all(root).unwrap();
    let unprefixed: Vec<&str> = written
        .lines()
        .filter(|line| !line.trim().is_empty() && !line.starts_with("wall-in-one-service: "))
        .collect();
    assert!(unprefixed.is_empty(), "{unprefixed:?}");
}

/// What the service writes to its journal over three probes of a compositor
/// that floods wlr-output-management, `slow` when it answers nothing else in
/// time either, and how often wlr-output-management was bound.
fn journal_of_a_flooding_compositor(slow: bool) -> (String, usize) {
    use std::io::Read;
    use std::os::unix::fs::PermissionsExt;

    let root = directory(if slow {
        "flooding-slow-wlr"
    } else {
        "flooding-wlr"
    });
    let wayland = root.join("wayland-0");
    let compositor = FakeCompositor::start(
        &wayland,
        Layout {
            heads: Some(vec![FakeHead {
                extra_modes: 5_000,
                ..FakeHead::new("eDP-1", true)
            }]),
            slow_without_wlr: slow,
            ..Layout::of(vec![FakeOutput::new(
                "eDP-1",
                (1920, 1080, 60_000),
                (0, 0, 1920, 1080),
            )])
        },
    );
    let noctalia = root.join("noctalia");
    fs::write(&noctalia, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&noctalia, fs::Permissions::from_mode(0o755)).unwrap();
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, config(&noctalia, &noctalia, false)).unwrap();
    let socket = root.join("runtime.sock");
    let mut child = ServiceChild(
        service_command()
            .env("WAYLAND_DISPLAY", &wayland)
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let reply = request(&socket, "status", None);
        let status: serde_json::Value =
            serde_json::from_str(reply["message"].as_str().unwrap()).unwrap();
        // The first question's answer: the output, or, from a compositor
        // that answers nothing in time, its timeout.
        let answered = if slow {
            status["output_discovery_error"] != ""
        } else {
            status["outputs"][0]["name"] == "eDP-1"
        };
        if answered {
            break;
        }
        assert!(Instant::now() < deadline, "the first question never ended");
        thread::sleep(Duration::from_millis(50));
    }
    // Two more five-second probes of the same compositor.
    thread::sleep(Duration::from_secs(11));
    stop(&mut child, &socket);
    let binds = compositor.wlr_binds();
    drop(compositor);
    let mut written = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut written)
        .unwrap();
    fs::remove_dir_all(root).unwrap();
    (written, binds)
}

fn said_once_in(written: &str) {
    let said: Vec<&str> = written
        .lines()
        .filter(|line| line.contains("leaving that protocol out"))
        .collect();
    assert_eq!(said.len(), 1, "{written}");
    assert!(said[0].starts_with("wall-in-one-service: "), "{}", said[0]);
    assert!(said[0].contains("until the compositor or the service restarts"));
}

#[test]
fn leaving_out_a_flooding_wlr_output_management_is_said_once() {
    let (written, binds) = journal_of_a_flooding_compositor(false);
    said_once_in(&written);
    assert_eq!(binds, 1, "{written}");
}

#[test]
fn a_flooding_compositor_that_also_times_out_is_flooded_and_said_once() {
    // A timeout is no sign that the compositor went away: wlr-output-management
    // stays left out over the questions it leaves unanswered.
    let (written, binds) = journal_of_a_flooding_compositor(true);
    said_once_in(&written);
    assert_eq!(binds, 1, "{written}");
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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );
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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );
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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );
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
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .stderr(Stdio::from(stderr))
            .spawn()
            .unwrap(),
    );
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
    assert_eq!(
        status.code(),
        Some(1),
        "a post-load runtime failure must remain restartable"
    );
    let stderr = fs::read_to_string(&stderr_path).unwrap();
    assert!(stderr.contains("readiness deadline expired after 8 seconds"));
    assert!(stderr.contains("desktop stayed unavailable"));
    assert!(!socket.exists());
    fs::remove_dir_all(root).unwrap();
}

// -- runtime-overrides.toml: per-playlist rotation and display precedence ---

/// A third, two-still playlist so rule winners and cycling are observable.
const EVENING_PLAYLIST: &str = "\n[[playlists]]\nid = \"evening\"\nname = \"Evening\"\n\
[[playlists.entries]]\nid = \"evening-one\"\nkind = \"still\"\nstill = \"/tmp/evening-one.png\"\n\
palette = { kind = \"keep\", mode = \"keep\" }\n\
[[playlists.entries]]\nid = \"evening-two\"\nkind = \"still\"\nstill = \"/tmp/evening-two.png\"\n\
palette = { kind = \"keep\", mode = \"keep\" }\n";

/// The independent fixture with the evening playlist and `extra` appended.
fn independent_with_evening(extra: &str) -> String {
    format!("{}{EVENING_PLAYLIST}{extra}", independent_config())
}

/// One display's opt-in, as a `runtime-overrides.toml` fragment.
fn beats(connector: &str) -> String {
    format!("[[displays]]\nconnector = \"{connector}\"\nbeats_global_rules = true\n")
}

/// One playlist's own rotation, as a `runtime-overrides.toml` fragment.
fn rotation(id: &str, fields: &str) -> String {
    format!("[[playlists]]\nid = \"{id}\"\n{fields}\n")
}

/// A complete `runtime-overrides.toml` from fragments.
fn overrides(fragments: &[String]) -> String {
    format!("schema_version = 1\n{}", fragments.concat())
}

/// `runtime.toml` text plus optional overrides, applied as `Config::load` does.
fn loaded(document: &str, fragments: &[String]) -> Config {
    let mut parsed: Config = toml::from_str(document).unwrap();
    parsed.validate().unwrap();
    if !fragments.is_empty() {
        let applied = Overrides::from_bytes(overrides(fragments).as_bytes()).unwrap();
        assert_eq!(parsed.apply_overrides(&applied), Vec::<String>::new());
    }
    parsed
}

fn noon() -> chrono::NaiveDateTime {
    NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap()
}

fn started_runtime(
    config: Config,
    at: chrono::NaiveDateTime,
) -> (Runtime<RuntimeDriver>, Arc<Mutex<RuntimeDriverState>>) {
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        config,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    (runtime, state)
}

fn rule_status(snapshot: &serde_json::Value, id: &str) -> serde_json::Value {
    snapshot["schedules"]
        .as_array()
        .unwrap()
        .iter()
        .find(|rule| rule["id"] == id)
        .unwrap()
        .clone()
}

fn route(snapshot: &serde_json::Value, connector: &str) -> serde_json::Value {
    snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["connector"] == connector)
        .unwrap()
        .clone()
}

/// Status without the per-process identity, for comparing two runtimes.
fn comparable_status<D: WallpaperDriver>(
    runtime: &mut Runtime<D>,
    at: chrono::NaiveDateTime,
) -> serde_json::Value {
    let mut snapshot = status(runtime, at);
    snapshot.as_object_mut().unwrap().remove("runtime_instance");
    snapshot
}

const GLOBAL_NOW: &str = "\n[[schedules]]\nid = \"global-now\"\nplaylist = \"night\"\n";

#[test]
fn display_precedence_with_the_flag_off_is_exactly_todays_order() {
    // No overrides, and an explicit opt-out, route exactly like the released
    // document: same winners, same status.
    let document = format!("{}{GLOBAL_NOW}", independent_config());
    let at = noon();
    let (mut today, _) = started_runtime(loaded(&document, &[]), at);
    let expected = comparable_status(&mut today, at);
    assert_eq!(route(&expected, "DP-1")["playlist_id"], "night");
    assert_eq!(route(&expected, "DP-1")["route_source"], "schedule");
    assert_eq!(route(&expected, "DP-1")["schedule_rule_id"], "global-now");
    assert_eq!(route(&expected, "DP-1")["beats_global_rules"], false);
    assert_eq!(rule_status(&expected, "global-now")["selected"], true);
    assert_eq!(
        expected["supported_config_schemas"],
        serde_json::json!([4, 5, 6, 7, 8])
    );
    assert_eq!(
        expected["supported_override_schemas"],
        serde_json::json!([1])
    );
    assert!(expected["loaded_overrides_sha256"].is_null());
    assert!(expected["overrides_ignored"].is_null());
    let opted_out = "[[displays]]\nconnector = \"DP-1\"\nbeats_global_rules = false\n".to_string();
    let (mut runtime, _) = started_runtime(loaded(&document, &[opted_out]), at);
    assert_eq!(comparable_status(&mut runtime, at), expected);
}

#[test]
fn display_flag_beats_global_rules_and_status_stops_selecting_the_beaten_rule() {
    let at = noon();
    let document = independent_with_evening(GLOBAL_NOW);
    let (mut runtime, _) = started_runtime(loaded(&document, &[beats("DP-1")]), at);
    let snapshot = status(&mut runtime, at);
    let dp = route(&snapshot, "DP-1");
    assert_eq!(dp["playlist_id"], "day");
    assert_eq!(dp["route_source"], "assignment");
    assert!(dp["schedule_rule_id"].is_null());
    assert_eq!(dp["beats_global_rules"], true);
    assert_eq!(dp["manual_override"], false);
    let hdmi = route(&snapshot, "HDMI-A-1");
    assert_eq!(hdmi["playlist_id"], "night");
    assert_eq!(hdmi["route_source"], "schedule");
    assert_eq!(hdmi["schedule_rule_id"], "global-now");
    assert_eq!(hdmi["beats_global_rules"], false);
    // The rule still wins on the display that did not opt in.
    let rule = rule_status(&snapshot, "global-now");
    assert_eq!(
        (&rule["selected"], &rule["in_force"]),
        (&true.into(), &true.into())
    );
    assert_eq!(snapshot["source"], "schedule");
    assert!(snapshot["schedule"]["rule_id"].is_null());

    // With both displays opted in, the global rule wins nowhere.
    let (mut runtime, _) =
        started_runtime(loaded(&document, &[beats("DP-1"), beats("HDMI-A-1")]), at);
    let snapshot = status(&mut runtime, at);
    for connector in ["DP-1", "HDMI-A-1"] {
        assert_eq!(route(&snapshot, connector)["playlist_id"], "day");
        assert_eq!(route(&snapshot, connector)["route_source"], "assignment");
    }
    let rule = rule_status(&snapshot, "global-now");
    assert_eq!(
        (&rule["selected"], &rule["in_force"]),
        (&false.into(), &false.into())
    );
    assert_eq!(snapshot["schedule"]["playlist_id"], "day");
    assert!(snapshot["schedule"]["rule_id"].is_null());
}

#[test]
fn display_flag_never_beats_a_rule_aimed_at_that_display() {
    // A targeted rule followed by a later global one: today the later global
    // rule wins on DP-1 (last match). Opted in, DP-1's own rule wins instead,
    // because only global rules lose to the display's own playlist.
    let rules = "\n[[schedules]]\nid = \"dp-now\"\nplaylist = \"night\"\nconnector = \"DP-1\"\n\
                 [[schedules]]\nid = \"global-later\"\nplaylist = \"evening\"\n";
    let at = noon();
    let document = independent_with_evening(rules);
    let (mut runtime, _) = started_runtime(loaded(&document, &[]), at);
    let snapshot = status(&mut runtime, at);
    assert_eq!(route(&snapshot, "DP-1")["schedule_rule_id"], "global-later");
    assert_eq!(route(&snapshot, "DP-1")["playlist_id"], "evening");

    let (mut runtime, _) = started_runtime(loaded(&document, &[beats("DP-1")]), at);
    let snapshot = status(&mut runtime, at);
    let dp = route(&snapshot, "DP-1");
    assert_eq!(dp["playlist_id"], "night");
    assert_eq!(dp["route_source"], "schedule");
    assert_eq!(dp["schedule_rule_id"], "dp-now");
    assert_eq!(dp["beats_global_rules"], true);
    let hdmi = route(&snapshot, "HDMI-A-1");
    assert_eq!(hdmi["playlist_id"], "evening");
    assert_eq!(hdmi["schedule_rule_id"], "global-later");
    assert_eq!(rule_status(&snapshot, "dp-now")["in_force"], true);
    assert_eq!(rule_status(&snapshot, "global-later")["in_force"], true);
}

#[test]
fn display_flag_follows_the_clock_and_a_manual_choice_still_wins() {
    let rules = "\n[[schedules]]\nid = \"global-evening\"\nplaylist = \"night\"\n\
                 start = \"18:00\"\nend = \"22:00\"\n\
                 [[schedules]]\nid = \"dp-late\"\nplaylist = \"evening\"\nconnector = \"DP-1\"\n\
                 start = \"20:00\"\nend = \"21:00\"\n";
    let config = loaded(&independent_with_evening(rules), &[beats("DP-1")]);
    let at = |hour: u32, minute: u32| {
        NaiveDate::from_ymd_opt(2026, 8, 3)
            .unwrap()
            .and_hms_opt(hour, minute, 0)
            .unwrap()
    };
    let (mut runtime, state) = started_runtime(config, at(12, 0));
    let now = Instant::now();
    let dp_applies = |state: &Arc<Mutex<RuntimeDriverState>>| -> usize {
        state
            .lock()
            .unwrap()
            .applied_outputs
            .iter()
            .filter(|output| *output == "DP-1")
            .count()
    };

    let before = dp_applies(&state);
    runtime.tick(at(19, 0), now + Duration::from_secs(1));
    let snapshot = status(&mut runtime, at(19, 0));
    assert_eq!(route(&snapshot, "DP-1")["playlist_id"], "day");
    assert_eq!(route(&snapshot, "DP-1")["route_source"], "assignment");
    assert_eq!(
        dp_applies(&state),
        before,
        "a beaten rule must not restart DP-1"
    );
    assert_eq!(route(&snapshot, "HDMI-A-1")["playlist_id"], "night");
    assert_eq!(
        route(&snapshot, "HDMI-A-1")["schedule_rule_id"],
        "global-evening"
    );

    runtime.tick(at(20, 30), now + Duration::from_secs(2));
    let dp = route(&status(&mut runtime, at(20, 30)), "DP-1");
    assert_eq!(dp["playlist_id"], "evening");
    assert_eq!(dp["route_source"], "schedule");
    assert_eq!(dp["schedule_rule_id"], "dp-late");

    assert!(
        runtime_command(
            &mut runtime,
            at(20, 30),
            "on",
            Some("DP-1 playlist-use day")
        )
        .ok
    );
    let dp = route(&status(&mut runtime, at(20, 30)), "DP-1");
    assert_eq!(dp["route_source"], "manual");
    assert_eq!(dp["playlist_id"], "day");
    assert!(runtime_command(&mut runtime, at(20, 30), "on", Some("DP-1 schedule-follow")).ok);
    assert_eq!(
        route(&status(&mut runtime, at(20, 30)), "DP-1")["schedule_rule_id"],
        "dp-late"
    );

    runtime.tick(at(21, 30), now + Duration::from_secs(3));
    let dp = route(&status(&mut runtime, at(21, 30)), "DP-1");
    assert_eq!(dp["playlist_id"], "day");
    assert_eq!(dp["route_source"], "assignment");
}

#[test]
fn reloading_the_overrides_file_reroutes_and_its_removal_restores() {
    let root = directory("overrides-reload");
    let path = root.join("runtime.toml");
    let sidecar = root.join(wall_in_one_service::config::OVERRIDES_FILENAME);
    let document = independent_with_evening(GLOBAL_NOW);
    fs::write(&path, &document).unwrap();
    let at = noon();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        path.clone(),
        Config::load(&path).unwrap(),
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    assert_eq!(
        route(&status(&mut runtime, at), "DP-1")["playlist_id"],
        "night"
    );

    fs::write(
        &sidecar,
        overrides(&[
            beats("DP-1"),
            rotation("night", "cycle_interval_seconds = 45"),
        ]),
    )
    .unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    let snapshot = status(&mut runtime, at);
    assert_eq!(route(&snapshot, "DP-1")["playlist_id"], "day");
    assert_eq!(route(&snapshot, "DP-1")["route_source"], "assignment");
    assert_eq!(route(&snapshot, "HDMI-A-1")["playlist_id"], "night");
    assert_eq!(route(&snapshot, "HDMI-A-1")["cycle_interval_seconds"], 45);
    assert_eq!(
        snapshot["loaded_overrides_sha256"].as_str().map(str::len),
        Some(64)
    );

    // A malformed overrides file of this schema is a failed reload:
    // everything stays.
    fs::write(&sidecar, "schema_version = 1\ncolour = \"red\"\n").unwrap();
    assert!(!runtime_command(&mut runtime, at, "reload", None).ok);
    assert_eq!(
        route(&status(&mut runtime, at), "DP-1")["playlist_id"],
        "day"
    );

    fs::remove_file(&sidecar).unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    let snapshot = status(&mut runtime, at);
    assert_eq!(route(&snapshot, "DP-1")["schedule_rule_id"], "global-now");
    assert_eq!(route(&snapshot, "HDMI-A-1")["cycle_interval_seconds"], 300);
    assert!(snapshot["loaded_overrides_sha256"].is_null());
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_newer_overrides_schema_is_ignored_and_reported_while_reloads_keep_working() {
    let root = directory("overrides-newer");
    let path = root.join("runtime.toml");
    let sidecar = root.join(wall_in_one_service::config::OVERRIDES_FILENAME);
    let document = independent_with_evening(GLOBAL_NOW);
    fs::write(&path, &document).unwrap();
    // What a later release might write: a new schema with keys this one lacks.
    fs::write(
        &sidecar,
        "schema_version = 2\n[[displays]]\nconnector = \"DP-1\"\nbeats_global_rules = true\n\
         precedence = \"own-playlist\"\n[[wallpapers]]\nid = \"x\"\n",
    )
    .unwrap();
    let at = noon();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        path.clone(),
        Config::load(&path).unwrap(),
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let snapshot = status(&mut runtime, at);
    assert_eq!(
        snapshot["overrides_ignored"],
        "unsupported schema_version 2"
    );
    assert!(snapshot["loaded_overrides_sha256"].is_null());
    assert_eq!(route(&snapshot, "DP-1")["schedule_rule_id"], "global-now");

    // Reloads keep working around it, and a supported file applies again.
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    fs::write(&sidecar, overrides(&[beats("DP-1")])).unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    let snapshot = status(&mut runtime, at);
    assert!(snapshot["overrides_ignored"].is_null());
    assert_eq!(route(&snapshot, "DP-1")["route_source"], "assignment");
    fs::write(&sidecar, "schema_version = 3\n").unwrap();
    assert!(runtime_command(&mut runtime, at, "reload", None).ok);
    let snapshot = status(&mut runtime, at);
    assert_eq!(
        snapshot["overrides_ignored"],
        "unsupported schema_version 3"
    );
    assert_eq!(route(&snapshot, "DP-1")["schedule_rule_id"], "global-now");
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn display_flag_is_dormant_in_mirrored_mode() {
    let document = format!(
        "{}{GLOBAL_NOW}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"day\"\n",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    );
    let at = noon();
    let (mut runtime, _) = started_runtime(loaded(&document, &[beats("DP-1")]), at);
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["display_mode"], "mirrored");
    let dp = route(&snapshot, "DP-1");
    assert_eq!(dp["playlist_id"], "night");
    assert_eq!(dp["route_source"], "schedule");
    assert_eq!(dp["beats_global_rules"], false);
}

#[test]
fn playlist_rotation_overrides_drive_each_independent_route() {
    let document = independent_with_evening("")
        .replace("cycle_enabled = false", "cycle_enabled = true")
        .replace(
            "connector = \"HDMI-A-1\"\nplaylist = \"day\"\n",
            "connector = \"HDMI-A-1\"\nplaylist = \"evening\"\n",
        );
    let config = loaded(
        &document,
        &[rotation(
            "day",
            "cycle_interval_seconds = 60\nshuffle = true",
        )],
    );
    let at = noon();
    let (mut runtime, _) = started_runtime(config, at);
    let start = Instant::now();
    let snapshot = status(&mut runtime, at);
    let dp = route(&snapshot, "DP-1");
    assert_eq!(dp["cycle_interval_seconds"], 60);
    assert_eq!(
        (
            &dp["shuffle"],
            &dp["shuffle_default"],
            &dp["shuffle_source"]
        ),
        (&true.into(), &true.into(), &"config".into())
    );
    let hdmi = route(&snapshot, "HDMI-A-1");
    assert_eq!(hdmi["cycle_interval_seconds"], 300);
    assert_eq!(
        (&hdmi["shuffle"], &hdmi["shuffle_default"]),
        (&false.into(), &false.into())
    );
    assert!(
        snapshot["cycle_interval_seconds"].is_null(),
        "routes disagree"
    );
    assert_eq!(snapshot["shuffle_default"], false);

    let dp_entry = dp["entry_id"].clone();
    let hdmi_entry = hdmi["entry_id"].clone();
    runtime.tick(at, start + Duration::from_secs(61));
    let snapshot = status(&mut runtime, at);
    assert_ne!(
        route(&snapshot, "DP-1")["entry_id"],
        dp_entry,
        "60 s playlist"
    );
    assert_eq!(
        route(&snapshot, "HDMI-A-1")["entry_id"],
        hdmi_entry,
        "global 300 s"
    );
    runtime.tick(at, start + Duration::from_secs(301));
    assert_ne!(
        route(&status(&mut runtime, at), "HDMI-A-1")["entry_id"],
        hdmi_entry
    );

    // A live override still beats the playlist's saved default, and
    // `default` returns to the playlist's value rather than the global one.
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 shuffle off")).ok);
    let dp = route(&status(&mut runtime, at), "DP-1");
    assert_eq!(
        (
            &dp["shuffle"],
            &dp["shuffle_default"],
            &dp["shuffle_source"]
        ),
        (&false.into(), &true.into(), &"manual".into())
    );
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 shuffle default")).ok);
    let dp = route(&status(&mut runtime, at), "DP-1");
    assert_eq!(
        (&dp["shuffle"], &dp["shuffle_source"]),
        (&true.into(), &"config".into())
    );

    // Switching a route's playlist adopts that playlist's own settings.
    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 playlist-use day")).ok);
    let snapshot = status(&mut runtime, at);
    let hdmi = route(&snapshot, "HDMI-A-1");
    assert_eq!(hdmi["cycle_interval_seconds"], 60);
    assert_eq!(
        (&hdmi["shuffle"], &hdmi["shuffle_default"]),
        (&true.into(), &true.into())
    );
    assert_eq!(snapshot["cycle_interval_seconds"], 60);
    assert_eq!(snapshot["shuffle_default"], true);
}

#[test]
fn playlist_rotation_overrides_drive_the_mirrored_timer_and_shuffle() {
    let document = format!(
        "{}{EVENING_PLAYLIST}",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
            .replace("cycle_enabled = false", "cycle_enabled = true")
    );
    let config = loaded(
        &document,
        &[rotation(
            "day",
            "cycle_interval_seconds = 60\nshuffle = true",
        )],
    );
    let at = noon();
    let (mut runtime, _) = started_runtime(config, at);
    let start = Instant::now();
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["cycle_interval_seconds"], 60);
    assert_eq!(
        (
            &snapshot["shuffle"],
            &snapshot["shuffle_default"],
            &snapshot["shuffle_source"]
        ),
        (&true.into(), &true.into(), &"config".into())
    );
    let all = route(&snapshot, "ALL");
    assert_eq!(
        (
            &all["shuffle"],
            &all["shuffle_default"],
            &all["cycle_interval_seconds"]
        ),
        (&true.into(), &true.into(), &60.into())
    );
    let entry = snapshot["entry_id"].clone();
    runtime.tick(at, start + Duration::from_secs(61));
    assert_ne!(status(&mut runtime, at)["entry_id"], entry);

    assert!(runtime_command(&mut runtime, at, "shuffle", Some("off")).ok);
    let snapshot = status(&mut runtime, at);
    assert_eq!(
        (
            &snapshot["shuffle"],
            &snapshot["shuffle_default"],
            &snapshot["shuffle_source"]
        ),
        (&false.into(), &true.into(), &"manual".into())
    );
    assert!(runtime_command(&mut runtime, at, "shuffle", Some("default")).ok);
    assert!(runtime_command(&mut runtime, at, "playlist-use", Some("evening")).ok);
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["playlist_id"], "evening");
    assert_eq!(snapshot["cycle_interval_seconds"], 300);
    assert_eq!(
        (&snapshot["shuffle"], &snapshot["shuffle_default"]),
        (&false.into(), &false.into())
    );
}

#[test]
fn rotation_status_without_overrides_reports_the_global_settings() {
    let at = noon();
    for document in [
        independent_config(),
        config(Path::new("/bin/true"), Path::new("/bin/true"), false),
    ] {
        let (mut runtime, _) = started_runtime(loaded(&document, &[]), at);
        let snapshot = status(&mut runtime, at);
        assert_eq!(snapshot["cycle_interval_seconds"], 300);
        assert_eq!(snapshot["shuffle_default"], false);
        for row in snapshot["displays"].as_array().unwrap() {
            assert_eq!(row["cycle_interval_seconds"], 300);
            assert_eq!(row["shuffle_default"], false);
            assert_eq!(row["beats_global_rules"], false);
        }
    }
}

#[test]
fn the_file_watcher_reloads_when_only_the_overrides_file_changes() {
    use std::os::unix::fs::PermissionsExt;
    let root = directory("overrides-watch");
    let config_path = root.join("runtime.toml");
    let sidecar = root.join(wall_in_one_service::config::OVERRIDES_FILENAME);
    let socket = root.join("runtime.sock");
    let harmless = root.join("harmless");
    fs::write(&harmless, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&harmless, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(&config_path, config(&harmless, &harmless, false)).unwrap();
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );
    let loaded_overrides = || -> serde_json::Value {
        let reply = request(&socket, "status", None);
        let status: serde_json::Value =
            serde_json::from_str(reply["message"].as_str().unwrap()).unwrap();
        status["loaded_overrides_sha256"].clone()
    };
    let wait_for = |wanted: &dyn Fn(&serde_json::Value) -> bool| {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let value = loaded_overrides();
            if wanted(&value) {
                return value;
            }
            assert!(
                Instant::now() < deadline,
                "the watcher never reloaded: {value}"
            );
            thread::sleep(Duration::from_millis(100));
        }
    };
    assert!(loaded_overrides().is_null());

    fs::write(
        &sidecar,
        overrides(&[rotation("day", "cycle_interval_seconds = 45")]),
    )
    .unwrap();
    let first = wait_for(&|value| value.is_string());
    fs::write(
        &sidecar,
        overrides(&[rotation("day", "cycle_interval_seconds = 90")]),
    )
    .unwrap();
    wait_for(&|value| value.is_string() && *value != first);
    fs::remove_file(&sidecar).unwrap();
    wait_for(&|value| value.is_null());

    stop(&mut child, &socket);
    fs::remove_dir_all(root).unwrap();
}

// -- status shape: older fields stay byte-identical -------------------------

/// Display-row keys added after the shape fixture was recorded. They are
/// removed from a fresh status before it is compared byte for byte, so every
/// older field must keep its exact name, order, value and encoding.
const ADDED_DISPLAY_KEYS: &[&str] = &[
    "route_change_at",
    "route_change_in_s",
    "next_cycle_at",
    "next_cycle_in_s",
    "until",
    "next_change_in_s",
    // 0.4.1: a display that plays nothing (schema 8) says why. Null on every
    // row of the recorded samples, which all play something.
    "nothing_to_play",
    "entry_name",
];

/// Top-level keys added after the shape fixture was recorded: the compositor's
/// display snapshot and the renderers the service could not start (0.4.0), and
/// the items it refused (0.4.1). Removed the same way, so that the released
/// ones are still compared byte for byte.
const ADDED_TOP_LEVEL_KEYS: &[&str] = &["outputs", "missing_renderers", "refused"];

/// Top-level string keys added after the shape fixture was recorded: the
/// build's install kind (0.4.0), which also differs between a `cargo test`
/// (`source`) and the packaged build (`nix`).
const ADDED_TOP_LEVEL_STRINGS: &[&str] = &["install_kind", "nothing_to_play"];

/// Top-level flags added after the shape fixture was recorded: whether the
/// loaded document allows web wallpapers, and how many refused items status
/// left out (0.4.1). Removed the same way.
const ADDED_TOP_LEVEL_FLAGS: &[&str] = &["allow_web_wallpapers", "refused_omitted"];

/// `"key":"value"` with the value replaced, for fields that differ per process
/// or per build. A missing key is left alone; a `null` value is kept.
fn mask_string_value(text: &str, key: &str) -> String {
    let marker = format!("\"{key}\":\"");
    let Some(start) = text.find(&marker) else {
        return text.to_string();
    };
    let value_start = start + marker.len();
    let value_end = value_start + text[value_start..].find('"').unwrap();
    format!("{}<masked>{}", &text[..value_start], &text[value_end..])
}

/// Remove `,"key":value` for every key in `ADDED_DISPLAY_KEYS`,
/// `ADDED_TOP_LEVEL_STRINGS` and `ADDED_TOP_LEVEL_FLAGS`. The added values
/// are `null`, booleans, integers or strings without commas, quotes or braces.
fn without_added_keys(text: &str) -> String {
    let mut text = text.to_string();
    for key in ADDED_DISPLAY_KEYS
        .iter()
        .chain(ADDED_TOP_LEVEL_STRINGS)
        .chain(ADDED_TOP_LEVEL_FLAGS)
    {
        let marker = format!(",\"{key}\":");
        while let Some(start) = text.find(&marker) {
            let value_start = start + marker.len();
            let rest = &text[value_start..];
            let value_end = if let Some(quoted) = rest.strip_prefix('"') {
                value_start + 1 + quoted.find('"').unwrap() + 1
            } else {
                value_start + rest.find([',', '}']).unwrap()
            };
            text.replace_range(start..value_end, "");
        }
    }
    for key in ADDED_TOP_LEVEL_KEYS {
        let marker = format!(",\"{key}\":[");
        let Some(start) = text.find(&marker) else {
            continue;
        };
        let mut depth = 0_usize;
        let mut close = None;
        for (offset, byte) in text.bytes().enumerate().skip(start + marker.len() - 1) {
            match byte {
                b'[' => depth += 1,
                b']' => {
                    depth -= 1;
                    if depth == 0 {
                        close = Some(offset);
                        break;
                    }
                }
                _ => {}
            }
        }
        let close = close.expect("the added array is closed");
        text.replace_range(start..=close, "");
    }
    text
}

fn raw_status<D: WallpaperDriver>(runtime: &mut Runtime<D>, at: chrono::NaiveDateTime) -> String {
    let response = runtime_command(runtime, at, "status", None);
    assert!(response.ok, "{}", response.message);
    let mut text = response.message;
    for key in ["runtime_instance", "runtime_executable", "runtime_version"] {
        text = mask_string_value(&text, key);
    }
    text
}

/// One status per shape the runtime can produce: mirrored to all outputs,
/// mirrored with assignments, and independent with overrides, a schedule in
/// force, a manual pick and a paused display.
fn status_shape_samples() -> Vec<(&'static str, String)> {
    let at = noon();
    let afternoon = "\n[[schedules]]\nid = \"afternoon\"\nplaylist = \"night\"\n\
                     start = \"12:00\"\nend = \"18:00\"\n";
    let cycling =
        |document: String| document.replace("cycle_enabled = false", "cycle_enabled = true");
    let base = cycling(config(
        Path::new("/bin/true"),
        Path::new("/bin/true"),
        false,
    ));

    let (mut all, _) = started_runtime(loaded(&format!("{base}{afternoon}"), &[]), at);
    let mirrored_all = raw_status(&mut all, at);

    let assigned = format!(
        "{base}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"day\"\n\
         [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"night\"\n"
    );
    let (mut mirrored, _) = started_runtime(loaded(&assigned, &[]), at);
    assert!(runtime_command(&mut mirrored, at, "pause", None).ok);
    let mirrored_assigned = raw_status(&mut mirrored, at);

    let targeted = "\n[[schedules]]\nid = \"dp-evening\"\nplaylist = \"evening\"\n\
                    connector = \"DP-1\"\nstart = \"18:00\"\nend = \"23:00\"\n";
    let document = cycling(independent_with_evening(&format!("{afternoon}{targeted}")));
    let (mut independent, _) = started_runtime(
        loaded(
            &document,
            &[
                beats("DP-1"),
                rotation("night", "cycle_interval_seconds = 45"),
            ],
        ),
        at,
    );
    assert!(runtime_command(&mut independent, at, "on", Some("HDMI-A-1 pause")).ok);
    let independent_paused = raw_status(&mut independent, at);
    assert!(
        runtime_command(
            &mut independent,
            at,
            "on",
            Some("HDMI-A-1 playlist-use evening")
        )
        .ok
    );
    let independent_manual = raw_status(&mut independent, at);

    vec![
        ("mirrored-all", mirrored_all),
        ("mirrored-assigned", mirrored_assigned),
        ("independent-paused", independent_paused),
        ("independent-manual", independent_manual),
    ]
}

#[test]
fn status_and_version_say_the_install_kind_this_build_was_compiled_as() {
    let at = noon();
    let kind = wall_in_one_service::INSTALL_KIND;
    assert!(wall_in_one_service::is_install_kind(kind), "{kind}");
    for (label, text) in status_shape_samples() {
        let snapshot: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(snapshot["install_kind"], kind, "{label}");
        assert_eq!(
            text.matches("\"install_kind\":").count(),
            1,
            "{label}: top level only"
        );
    }
    let (mut independent, _) = started_runtime(loaded(&independent_with_evening(""), &[]), at);
    assert_eq!(status(&mut independent, at)["install_kind"], kind);

    let version = service_command().arg("--version").output().unwrap();
    assert!(version.status.success());
    assert_eq!(
        String::from_utf8(version.stdout).unwrap(),
        format!(
            "wall-in-one-service {}\ninstall kind: {kind}\n",
            env!("CARGO_PKG_VERSION")
        ),
        "the first line is the one 0.3 printed"
    );
}

#[test]
fn older_status_fields_stay_byte_identical() {
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/status-shape-v2.txt");
    let rendered: String = status_shape_samples()
        .into_iter()
        .map(|(label, text)| format!("{label}\n{}\n", without_added_keys(&text)))
        .collect();
    if std::env::var_os("WALL_IN_ONE_BLESS_STATUS_SHAPE").is_some() {
        fs::write(&fixture, &rendered).unwrap();
    }
    for (label, text) in status_shape_samples() {
        let rows = serde_json::from_str::<serde_json::Value>(&text).unwrap()["displays"]
            .as_array()
            .unwrap()
            .len();
        for key in ADDED_DISPLAY_KEYS {
            // `nothing_to_play` is also a top-level key.
            let top_level = usize::from(ADDED_TOP_LEVEL_STRINGS.contains(key));
            assert_eq!(
                text.matches(&format!(",\"{key}\":")).count(),
                rows + top_level,
                "{label}: every display row carries {key}"
            );
        }
    }
    let recorded = fs::read_to_string(&fixture).unwrap();
    for (expected, actual) in recorded.lines().zip(rendered.lines()) {
        assert_eq!(
            actual, expected,
            "a released status field changed its bytes"
        );
    }
    assert_eq!(rendered, recorded);
}

// -- status timing: when the route and the rotation next change ------------

fn at_time(day: u32, hour: u32, minute: u32) -> chrono::NaiveDateTime {
    NaiveDate::from_ymd_opt(2026, 8, day)
        .unwrap()
        .and_hms_opt(hour, minute, 0)
        .unwrap()
}

/// A global rule for the night playlist, 12:00 to 18:00.
const AFTERNOON: &str = "\n[[schedules]]\nid = \"afternoon\"\nplaylist = \"night\"\n\
                         start = \"12:00\"\nend = \"18:00\"\n";

/// The mirrored fixture with rotation on and `extra` appended.
fn mirrored_cycling(extra: &str) -> String {
    format!(
        "{}{extra}",
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
            .replace("cycle_enabled = false", "cycle_enabled = true")
    )
}

/// A fresh rotation deadline: the interval, less the moments the test itself
/// took (with room for a loaded build machine).
fn assert_cycle_within(row: &serde_json::Value, interval: u64) {
    let seconds = row["next_cycle_in_s"].as_u64().unwrap();
    assert!(
        (interval.saturating_sub(10)..=interval).contains(&seconds),
        "{seconds} s left of a {interval} s rotation: {row}"
    );
    assert_eq!(row["next_change_in_s"], row["next_cycle_in_s"]);
    assert!(row["next_cycle_at"].is_string());
}

fn assert_no_route_change(row: &serde_json::Value) {
    assert!(row["route_change_at"].is_null(), "{row}");
    assert!(row["route_change_in_s"].is_null(), "{row}");
    assert!(row["until"].is_null(), "{row}");
}

fn assert_no_cycle(row: &serde_json::Value) {
    assert!(row["next_cycle_at"].is_null(), "{row}");
    assert!(row["next_cycle_in_s"].is_null(), "{row}");
    assert!(row["next_change_in_s"].is_null(), "{row}");
}

#[test]
fn status_reports_the_next_rule_boundary_and_the_cycle_deadline() {
    let at = at_time(3, 17, 0);
    let (mut runtime, _) = started_runtime(loaded(&mirrored_cycling(AFTERNOON), &[]), at);
    let row = route(&status(&mut runtime, at), "ALL");
    assert_eq!(row["route_source"], "schedule");
    assert_eq!(row["route_change_at"], "2026-08-03T18:00:00");
    assert_eq!(row["route_change_in_s"], 3600);
    assert_eq!(row["until"], "18:00");
    assert_cycle_within(&row, 300);

    // Half a minute before the boundary rounds to the boundary minute.
    let almost = at_time(3, 17, 59) + chrono::Duration::seconds(30);
    let row = route(&status(&mut runtime, almost), "ALL");
    assert_eq!(row["route_change_in_s"], 30);
    assert_eq!(row["until"], "18:00");

    // After the window the default plays until tomorrow's window opens. (Once the tick has
    // applied it: until then the row is the rule's decision, which still plays; r13d N3.)
    let evening = at_time(3, 19, 0);
    runtime.tick(evening, Instant::now());
    let row = route(&status(&mut runtime, evening), "ALL");
    assert_eq!(row["route_source"], "default");
    assert_eq!(row["route_change_at"], "2026-08-04T12:00:00");
    assert_eq!(row["route_change_in_s"], 17 * 3600);
    assert_eq!(row["until"], "12:00");
}

#[test]
fn a_rotation_restarts_the_cycle_deadline_from_the_tick_that_advanced_it() {
    let at = at_time(3, 9, 0);
    let (mut runtime, state) = started_runtime(loaded(&mirrored_cycling(""), &[]), at);
    let applies = state.lock().unwrap().applies.len();
    let advanced = Instant::now() + Duration::from_secs(301);
    runtime.tick(at, advanced);
    assert!(
        state.lock().unwrap().applies.len() > applies,
        "the rotation ran"
    );
    let before = chrono::Utc::now();
    let row = route(&status(&mut runtime, at), "ALL");
    let after = chrono::Utc::now();
    assert_eq!(row["next_cycle_in_s"], 300);
    assert_eq!(row["next_change_in_s"], 300);
    // The deadline's wall-clock time is the real clock plus the elapsed 300 s
    // in the local zone, not the synthetic schedule reading plus 300 s.
    let due = chrono::NaiveDateTime::parse_from_str(
        row["next_cycle_at"].as_str().unwrap(),
        "%Y-%m-%dT%H:%M:%S",
    )
    .unwrap();
    let earliest = wall_clock_after(before, 299, &chrono::Local).unwrap();
    let latest = wall_clock_after(after, 300, &chrono::Local).unwrap();
    assert!(
        earliest <= due && due <= latest,
        "{earliest} <= {due} <= {latest}"
    );
    // No enabled rule: the route never changes on its own.
    assert_no_route_change(&row);
}

#[test]
fn route_changes_cross_midnight_for_weekday_and_wrapped_rules() {
    // 2026-08-03 is a Monday.
    let monday = "\n[[schedules]]\nid = \"monday\"\nplaylist = \"night\"\nweekdays = [0]\n";
    let at = at_time(3, 23, 0);
    let (mut runtime, _) = started_runtime(loaded(&mirrored_cycling(monday), &[]), at);
    let row = route(&status(&mut runtime, at), "ALL");
    assert_eq!(row["route_change_at"], "2026-08-04T00:00:00");
    assert_eq!(row["route_change_in_s"], 3600);
    assert_eq!(row["until"], "00:00");

    let wrapped = "\n[[schedules]]\nid = \"late\"\nplaylist = \"night\"\n\
                   start = \"22:00\"\nend = \"06:00\"\n";
    let (mut runtime, _) = started_runtime(loaded(&mirrored_cycling(wrapped), &[]), at);
    let row = route(&status(&mut runtime, at), "ALL");
    assert_eq!(row["route_source"], "schedule");
    assert_eq!(row["route_change_at"], "2026-08-04T06:00:00");
    assert_eq!(row["until"], "06:00");

    // Outside the wrapped window, the next change is tonight's 22:00.
    let tuesday = at_time(4, 9, 0);
    let row = route(&status(&mut runtime, tuesday), "ALL");
    assert_eq!(row["route_change_at"], "2026-08-04T22:00:00");

    // A change more than a day away keeps its time but has no "until".
    let (mut runtime, _) = started_runtime(loaded(&mirrored_cycling(monday), &[]), tuesday);
    let weekly = route(&status(&mut runtime, tuesday), "ALL");
    assert_eq!(weekly["route_change_at"], "2026-08-10T00:00:00");
    assert_eq!(weekly["route_change_in_s"], 5 * 86400 + 15 * 3600);
    assert!(weekly["until"].is_null());
}

#[test]
fn a_manual_pick_holds_the_route_but_not_the_rotation() {
    let at = at_time(3, 17, 0);
    let (mut runtime, _) = started_runtime(loaded(&mirrored_cycling(AFTERNOON), &[]), at);
    assert!(runtime_command(&mut runtime, at, "playlist-use", Some("day")).ok);
    let row = route(&status(&mut runtime, at), "ALL");
    assert_eq!(row["route_source"], "manual");
    assert_no_route_change(&row);
    assert_cycle_within(&row, 300);

    assert!(runtime_command(&mut runtime, at, "schedule-follow", None).ok);
    let row = route(&status(&mut runtime, at), "ALL");
    assert_eq!(row["until"], "18:00");
}

#[test]
fn a_paused_display_has_no_cycle_deadline_but_keeps_its_schedule() {
    let at = at_time(3, 17, 0);
    let (mut runtime, _) = started_runtime(loaded(&mirrored_cycling(AFTERNOON), &[]), at);
    assert!(runtime_command(&mut runtime, at, "pause", None).ok);
    let row = route(&status(&mut runtime, at), "ALL");
    assert_eq!(row["playback_state"], "paused");
    assert_no_cycle(&row);
    // The schedule still moves a paused route.
    assert_eq!(row["until"], "18:00");

    // Stop keeps rotating stills, so it keeps its deadline.
    assert!(runtime_command(&mut runtime, at, "stop", None).ok);
    let row = route(&status(&mut runtime, at), "ALL");
    assert_eq!(row["playback_state"], "stopped");
    assert!(row["next_cycle_in_s"].is_u64());

    assert!(runtime_command(&mut runtime, at, "cycle", Some("off")).ok);
    assert_no_cycle(&route(&status(&mut runtime, at), "ALL"));
}

#[test]
fn mirrored_rows_share_one_timer_and_ignore_a_dormant_opt_in() {
    let assigned = mirrored_cycling(&format!(
        "{AFTERNOON}\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"day\"\n\
         [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"night\"\n\
         [[displays]]\nconnector = \"DETACHED-1\"\nplaylist = \"day\"\n"
    ));
    let at = at_time(3, 17, 0);
    // The opt-in is dormant in mirrored mode: the global rule still applies.
    let (mut runtime, _) = started_runtime(loaded(&assigned, &[beats("DP-1")]), at);
    let snapshot = status(&mut runtime, at);
    let dp = route(&snapshot, "DP-1");
    let hdmi = route(&snapshot, "HDMI-A-1");
    assert_eq!(dp["beats_global_rules"], false);
    // DP-1 returns to its own playlist at 18:00; HDMI-A-1 keeps the night
    // playlist, but its reason changes from the schedule to its assignment.
    assert_eq!(dp["until"], "18:00");
    assert_eq!(hdmi["until"], "18:00");
    assert_eq!(dp["next_cycle_at"], hdmi["next_cycle_at"]);
    assert_cycle_within(&dp, 300);
    let detached = route(&snapshot, "DETACHED-1");
    assert_eq!(detached["connected"], false);
    assert_no_route_change(&detached);
    assert_no_cycle(&detached);
}

/// DP-1's own evening rule (19:00 to 23:00) authored before the global
/// afternoon rule (12:00 to 20:00) for the night playlist.
const OPT_IN_RULES: &str = "\n[[schedules]]\nid = \"dp-evening\"\nplaylist = \"evening\"\n\
                            connector = \"DP-1\"\nstart = \"19:00\"\nend = \"23:00\"\n\
                            [[schedules]]\nid = \"afternoon\"\nplaylist = \"night\"\n\
                            start = \"12:00\"\nend = \"20:00\"\n";

#[test]
fn independent_route_changes_follow_the_displays_own_precedence() {
    let document = independent_with_evening(OPT_IN_RULES)
        .replace("cycle_enabled = false", "cycle_enabled = true");
    let at = at_time(3, 13, 0);

    // Without the opt-in the later global rule wins DP-1 until 20:00.
    let (mut plain, _) = started_runtime(loaded(&document, &[]), at);
    let dp = route(&status(&mut plain, at), "DP-1");
    assert_eq!(dp["playlist_id"], "night");
    assert_eq!(dp["route_change_at"], "2026-08-03T20:00:00");
    assert_eq!(dp["until"], "20:00");

    // With it, DP-1 plays its own playlist until its own rule at 19:00.
    let (mut runtime, _) = started_runtime(
        loaded(
            &document,
            &[
                beats("DP-1"),
                rotation("night", "cycle_interval_seconds = 45"),
            ],
        ),
        at,
    );
    let snapshot = status(&mut runtime, at);
    let dp = route(&snapshot, "DP-1");
    let hdmi = route(&snapshot, "HDMI-A-1");
    assert_eq!(dp["route_source"], "assignment");
    assert_eq!(dp["route_change_at"], "2026-08-03T19:00:00");
    assert_eq!(dp["until"], "19:00");
    assert_cycle_within(&dp, 300);
    // HDMI-A-1 follows the global rule, with the night playlist's interval.
    assert_eq!(hdmi["route_source"], "schedule");
    assert_eq!(hdmi["until"], "20:00");
    assert_cycle_within(&hdmi, 45);

    // A pick on one display holds only that display's route; a pause stops
    // only that display's rotation.
    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 playlist-use day")).ok);
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 pause")).ok);
    let snapshot = status(&mut runtime, at);
    let dp = route(&snapshot, "DP-1");
    let hdmi = route(&snapshot, "HDMI-A-1");
    assert_no_route_change(&hdmi);
    assert_cycle_within(&hdmi, 300);
    assert_eq!(dp["until"], "19:00");
    assert_no_cycle(&dp);
}

#[test]
fn companion_status_fields_keep_their_wire_names_and_types() {
    // The Noctalia companion reads `until` (local HH:MM, shown for every
    // reason but a pick) and `next_change_in_s` (whole seconds, aged between
    // polls) from each display row of `wall-in-one ctl status`.
    let document = independent_with_evening(OPT_IN_RULES)
        .replace("cycle_enabled = false", "cycle_enabled = true");
    let at = at_time(3, 13, 0);
    let (mut runtime, _) = started_runtime(loaded(&document, &[]), at);
    assert!(runtime_command(&mut runtime, at, "on", Some("HDMI-A-1 playlist-use day")).ok);
    assert!(runtime_command(&mut runtime, at, "on", Some("DP-1 cycle off")).ok);
    let mut samples = vec![status(&mut runtime, at)];
    let (mut mirrored, _) = started_runtime(loaded(&mirrored_cycling(AFTERNOON), &[]), at);
    samples.push(status(&mut mirrored, at));
    let mut seen_until = false;
    let mut seen_countdown = false;
    for snapshot in &samples {
        for row in snapshot["displays"].as_array().unwrap() {
            let object = row.as_object().unwrap();
            assert!(object.contains_key("until") && object.contains_key("next_change_in_s"));
            match &row["until"] {
                serde_json::Value::Null => {}
                serde_json::Value::String(text) => {
                    let bytes = text.as_bytes();
                    assert!(
                        bytes.len() == 5
                            && bytes[2] == b':'
                            && [0, 1, 3, 4].iter().all(|i| bytes[*i].is_ascii_digit()),
                        "until must be local HH:MM: {text:?}"
                    );
                    assert_ne!(row["route_source"], "manual");
                    seen_until = true;
                }
                other => panic!("until must be a string or null: {other}"),
            }
            match &row["next_change_in_s"] {
                serde_json::Value::Null => {}
                value => {
                    assert!(value.is_u64(), "next_change_in_s must be whole seconds");
                    assert_eq!(row["cycle_enabled"], true);
                    seen_countdown = true;
                }
            }
        }
    }
    assert!(seen_until && seen_countdown);
    let hdmi = route(&samples[0], "HDMI-A-1");
    assert_eq!(hdmi["route_source"], "manual");
    assert!(hdmi["until"].is_null());
    let dp = route(&samples[0], "DP-1");
    assert_eq!(dp["cycle_enabled"], false);
    assert!(dp["next_change_in_s"].is_null());
}

#[test]
fn hundreds_of_rules_on_many_displays_cannot_stall_status() {
    // Every rule has its own window, and the last one always wins, so no
    // display's route ever changes: each search would scan its whole horizon.
    let mut parsed: Config = toml::from_str(&independent_config()).unwrap();
    parsed.schedules = (0..511)
        .map(|index| wall_in_one_service::config::ScheduleRule {
            id: format!("rule-{index}"),
            playlist: "night".into(),
            connector: String::new(),
            months: vec![],
            weekdays: vec![],
            start: Some(format!("{:02}:{:02}", index / 60 % 24, index % 60)),
            end: Some(format!("{:02}:{:02}", (index / 60 + 3) % 24, index % 60)),
            enabled: true,
        })
        .chain(std::iter::once(wall_in_one_service::config::ScheduleRule {
            id: "always".into(),
            playlist: "day".into(),
            connector: String::new(),
            months: vec![],
            weekdays: vec![],
            start: None,
            end: None,
            enabled: true,
        }))
        .collect();
    parsed.validate().unwrap();
    let outputs: Vec<String> = (0..64).map(|index| format!("LIVE-{index:02}")).collect();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(outputs),
        ..RuntimeDriverState::default()
    }));
    let at = noon();
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let started = Instant::now();
    let snapshot = status(&mut runtime, at);
    let rows = snapshot["displays"].as_array().unwrap();
    assert_eq!(
        rows.iter().filter(|row| row["connected"] == true).count(),
        64
    );
    assert!(rows.iter().all(|row| row["route_change_at"].is_null()));
    assert!(
        started.elapsed() < Duration::from_secs(30),
        "status took {:?}",
        started.elapsed()
    );
}

// A renderer child that exits while the user has paused or stopped must not
// change that transport intent (HF-2). Pause holds the playlist cursor.
fn renderer_exit_runtime(
    independent: bool,
    label: &str,
) -> (
    Runtime<RuntimeDriver>,
    Arc<Mutex<RuntimeDriverState>>,
    chrono::NaiveDateTime,
    PathBuf,
) {
    let document = if independent {
        independent_config()
    } else {
        config(Path::new("/bin/true"), Path::new("/bin/true"), false)
    }
    .replace("cycle_interval_seconds = 300", "cycle_interval_seconds = 5")
    .replace("cycle_enabled = false", "cycle_enabled = true");
    let mut parsed: Config = toml::from_str(&document).unwrap();
    parsed.schedules.clear();
    let first = parsed.playlists[0].entries[1].clone();
    let mut second = first.clone();
    second.id = "other-video".into();
    parsed.playlists[0].entries = vec![first, second];
    parsed.validate().unwrap();
    let at = NaiveDate::from_ymd_opt(2026, 10, 18)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let root = directory(label);
    let mut runtime = Runtime::new(
        root.join("runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        at,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    (runtime, state, at, root)
}

fn queue_renderer_exit(state: &Arc<Mutex<RuntimeDriverState>>, independent: bool) {
    state
        .lock()
        .unwrap()
        .renderer_failures
        .push(RendererFailure {
            entry_id: "video-two".into(),
            kind: wall_in_one_service::config::EntryKind::Video,
            scene_id: None,
            output: if independent { "DP-1" } else { "" }.into(),
            message: "renderer child exited".into(),
            permanent_for_session: false,
        });
}

fn assert_failed_and_held(
    runtime: &mut Runtime<RuntimeDriver>,
    at: chrono::NaiveDateTime,
    independent: bool,
    expected_state: &str,
) {
    let after = status(runtime, at);
    assert_eq!(after["playback_state"], expected_state, "{after}");
    if expected_state != "stopped" {
        // Only Stopped keeps cycling its still; Playing and Paused hold here.
        assert_eq!(after["entry_id"], "video-two", "{after}");
        assert_eq!(after["motion_active"], false, "{after}");
    }
    assert_eq!(after["renderer_failed"], true, "{after}");
    assert!(
        after["last_error"]
            .as_str()
            .unwrap()
            .contains("renderer child exited"),
        "{after}"
    );
    if independent {
        let row = display_status(runtime, at, "DP-1");
        assert_eq!(row["playback_state"], expected_state, "{row}");
        assert_eq!(row["renderer_failed"], true, "{row}");
    }
}

#[test]
fn renderer_exit_while_paused_stays_paused_and_holds_the_cursor() {
    for independent in [false, true] {
        let (mut runtime, state, at, root) = renderer_exit_runtime(independent, "exit-paused");
        assert!(runtime_command(&mut runtime, at, "pause", None).ok);
        assert_eq!(status(&mut runtime, at)["entry_id"], "video-two");
        queue_renderer_exit(&state, independent);
        let applies = state.lock().unwrap().applies.len();
        let base = Instant::now();
        runtime.tick(at, base);
        assert_failed_and_held(&mut runtime, at, independent, "paused");
        // Past the 5 s cycle interval: Pause must still hold the cursor and
        // must not render anything new.
        runtime.tick(at, base + Duration::from_secs(6));
        runtime.tick(at, base + Duration::from_secs(60));
        assert_failed_and_held(&mut runtime, at, independent, "paused");
        assert_eq!(state.lock().unwrap().applies.len(), applies);
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn paused_without_a_renderer_exit_stays_paused_after_the_interval() {
    for independent in [false, true] {
        let (mut runtime, state, at, root) = renderer_exit_runtime(independent, "exit-control");
        assert!(runtime_command(&mut runtime, at, "pause", None).ok);
        let base = Instant::now();
        runtime.tick(at, base + Duration::from_secs(6));
        let after = status(&mut runtime, at);
        assert_eq!(after["playback_state"], "paused", "{after}");
        assert_eq!(after["entry_id"], "video-two", "{after}");
        assert_eq!(after["renderer_failed"], false, "{after}");
        assert!(state.lock().unwrap().motion_active);
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn renderer_exit_while_stopped_stays_stopped() {
    for independent in [false, true] {
        let (mut runtime, state, at, root) = renderer_exit_runtime(independent, "exit-stopped");
        assert!(runtime_command(&mut runtime, at, "stop", None).ok);
        assert_eq!(status(&mut runtime, at)["playback_state"], "stopped");
        queue_renderer_exit(&state, independent);
        let base = Instant::now();
        runtime.tick(at, base);
        assert_failed_and_held(&mut runtime, at, independent, "stopped");
        // A later cycle may move the still, but never restarts motion.
        runtime.tick(at, base + Duration::from_secs(6));
        let after = status(&mut runtime, at);
        assert_eq!(after["playback_state"], "stopped", "{after}");
        assert!(!state.lock().unwrap().motion_active);
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn renderer_exit_while_playing_stays_playing_and_reports_the_failure() {
    for independent in [false, true] {
        let (mut runtime, state, at, root) = renderer_exit_runtime(independent, "exit-playing");
        assert_eq!(status(&mut runtime, at)["playback_state"], "playing");
        queue_renderer_exit(&state, independent);
        runtime.tick(at, Instant::now());
        assert_failed_and_held(&mut runtime, at, independent, "playing");
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn play_after_a_renderer_exit_while_paused_retries_and_resumes() {
    for independent in [false, true] {
        let (mut runtime, state, at, root) = renderer_exit_runtime(independent, "exit-play");
        assert!(runtime_command(&mut runtime, at, "pause", None).ok);
        queue_renderer_exit(&state, independent);
        let base = Instant::now();
        runtime.tick(at, base + Duration::from_secs(6));
        assert_failed_and_held(&mut runtime, at, independent, "paused");
        let applies = state.lock().unwrap().applies.len();
        let response = runtime_command(&mut runtime, at, "play", None);
        assert!(response.ok, "{}", response.message);
        let after = status(&mut runtime, at);
        assert_eq!(after["playback_state"], "playing", "{after}");
        assert_eq!(after["entry_id"], "video-two", "{after}");
        assert_eq!(after["renderer_failed"], false, "{after}");
        assert_eq!(after["motion_active"], true, "{after}");
        assert!(state.lock().unwrap().applies.len() > applies);
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn failing_to_pause_a_scheduled_apply_keeps_paused_and_play_retries() {
    for independent in [false, true] {
        let document = if independent {
            independent_config()
        } else {
            config(Path::new("/bin/true"), Path::new("/bin/true"), false)
        };
        let parsed: Config = toml::from_str(&document).unwrap();
        let summer = NaiveDate::from_ymd_opt(2026, 8, 3)
            .unwrap()
            .and_hms_opt(23, 0, 0)
            .unwrap();
        let winter = NaiveDate::from_ymd_opt(2026, 12, 3)
            .unwrap()
            .and_hms_opt(23, 0, 0)
            .unwrap();
        let state = Arc::new(Mutex::new(RuntimeDriverState {
            connected_outputs: Some(vec!["DP-1".into()]),
            ..RuntimeDriverState::default()
        }));
        let root = directory("pause-fails-scheduled");
        let mut runtime = Runtime::new(
            root.join("runtime.toml"),
            parsed,
            RuntimeDriver(state.clone()),
            summer,
        )
        .unwrap();
        runtime.apply_current().unwrap();
        assert!(runtime_command(&mut runtime, summer, "pause", None).ok);
        state.lock().unwrap().fail_pause = true;
        // The schedule moves to the night playlist; the new renderer cannot
        // be paused, but nobody pressed Play.
        let base = Instant::now() + Duration::from_secs(600);
        runtime.tick(winter, base);
        let held = status(&mut runtime, winter);
        assert_eq!(held["playback_state"], "paused", "{held}");
        if !independent {
            // The mirrored baseline is re-applied and cannot be paused either.
            // Independent mode restores a still baseline that needs no pause,
            // with the automatic retry pending.
            assert_eq!(held["renderer_failed"], true, "{held}");
        }
        state.lock().unwrap().fail_pause = false;
        let response = runtime_command(&mut runtime, winter, "play", None);
        assert!(response.ok, "{}", response.message);
        let after = status(&mut runtime, winter);
        assert_eq!(after["playback_state"], "playing", "{after}");
        assert_eq!(after["renderer_failed"], false, "{after}");
        fs::remove_dir_all(root).unwrap();
    }
}

/// Taboo records kept and omitted, and output rows, in the status of a
/// document at the status ceiling whose driver publishes `published`.
fn ceiling_status_with_outputs(published: Vec<Output>) -> (usize, usize, usize) {
    RUNTIME_DRIVER_PUBLISHED.with(|cell| cell.set(Box::leak(published.into_boxed_slice())));
    let at = NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let parsed = Config::from_bytes(status_ceiling_document().into_bytes()).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(
            (0..64)
                .map(|index| format!("{}L{index:02}", "\"".repeat(253)))
                .collect(),
        ),
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
    runtime.tick(at, Instant::now());
    let response = runtime.handle(
        wall_in_one_service::protocol::Request {
            verb: "status".into(),
            argument: None,
        },
        at,
    );
    RUNTIME_DRIVER_PUBLISHED.with(|cell| cell.set(&[]));
    assert!(response.ok, "{}", response.message);
    let snapshot: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    (
        snapshot["taboo_entries"].as_array().unwrap().len(),
        snapshot["taboo_entries_omitted"].as_u64().unwrap() as usize,
        snapshot["outputs"].as_array().unwrap().len(),
    )
}

#[test]
fn the_display_snapshot_never_displaces_taboo_records() {
    let (kept_without, omitted_without, _) = ceiling_status_with_outputs(Vec::new());
    // 64 ordinary monitors, as a compositor would describe them.
    let monitors = (0..64)
        .map(|index| Output {
            name: format!("DP-{index}"),
            enabled: true,
            make: "Dell Inc.".into(),
            model: "DELL U2720Q".into(),
            width: 2560,
            height: 1440,
            scale: 1.5,
            mode_width: 3840,
            mode_height: 2160,
            refresh_millihz: Some(59_997),
            physical_width_mm: 597,
            physical_height_mm: 336,
            ..Output::default()
        })
        .collect();
    let (kept_with, omitted_with, rows) = ceiling_status_with_outputs(monitors);
    assert_eq!(
        (kept_with, omitted_with),
        (kept_without, omitted_without),
        "the compositor's snapshot pushed taboo records out of status"
    );
    assert_eq!(rows, 0, "at the ceiling, the snapshot is what is left out");
}

// -- a renderer program that is not there (docs/missing-components.md) -------

/// A runtime document with one playlist of a video, a scene and a still, and
/// (independent) a second display on a playlist of stills.
fn missing_renderer_document(
    noctalia: &Path,
    mpvpaper: &Path,
    engine: &Path,
    independent: bool,
) -> String {
    let (mode, theme, displays) = if independent {
        (
            "independent",
            "DP-1",
            "[[displays]]\nconnector = \"DP-1\"\nplaylist = \"clips\"\n\
             [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"stills\"\n",
        )
    } else {
        ("mirrored", "", "")
    };
    format!(
        r#"schema_version = 4
config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
default_playlist = "clips"
[settings]
cycle_interval_seconds = 300
cycle_enabled = true
shuffle = false
dynamics_enabled = true
display_mode = "{mode}"
theme_source_connector = "{theme}"
[renderer]
noctalia_program = {noctalia:?}
mpvpaper_program = {mpvpaper:?}
linux_wallpaperengine_program = {engine:?}
own_scene_renderer = true
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
id = "clips"
name = "Clips"
[[playlists.entries]]
id = "video-a"
kind = "video"
still = "/tmp/a.png"
motion = "/tmp/a.mp4"
palette = {{ kind = "keep", mode = "keep" }}
[[playlists.entries]]
id = "scene-b"
kind = "scene"
still = "/tmp/b.png"
scene_id = "12345"
palette = {{ kind = "keep", mode = "keep" }}
[[playlists.entries]]
id = "still-c"
kind = "still"
still = "/tmp/c.png"
palette = {{ kind = "keep", mode = "keep" }}
[[playlists]]
id = "stills"
name = "Stills"
[[playlists.entries]]
id = "still-d"
kind = "still"
still = "/tmp/d.png"
palette = {{ kind = "keep", mode = "keep" }}
{displays}"#,
        noctalia = noctalia.display(),
        mpvpaper = mpvpaper.display(),
        engine = engine.display(),
    )
}

/// An executable shell script at `path`.
fn script(path: &Path, body: &str) {
    use std::os::unix::fs::PermissionsExt;
    fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

/// A real driver over `document`, written to `root/runtime.toml` so that
/// `reload` reads it, with a Noctalia that logs each call to `root/noctalia.log`
/// and two connected displays.
fn missing_renderer_runtime(
    root: &Path,
    mpvpaper: &Path,
    engine: &Path,
    independent: bool,
) -> (Runtime<SystemDriver>, PathBuf) {
    let source = SwitchableOutputs::default();
    source.set(vec![live("DP-1", None), live("HDMI-A-1", None)]);
    missing_renderer_runtime_over(
        root,
        mpvpaper,
        engine,
        independent,
        |document| document,
        &source,
    )
}

/// `missing_renderer_runtime`, with the document adjusted by `adjust` and the
/// displays a test switches itself.
fn missing_renderer_runtime_over(
    root: &Path,
    mpvpaper: &Path,
    engine: &Path,
    independent: bool,
    adjust: impl Fn(String) -> String,
    source: &SwitchableOutputs,
) -> (Runtime<SystemDriver>, PathBuf) {
    let log = root.join("noctalia.log");
    let noctalia = root.join("noctalia");
    script(&noctalia, &format!("printf '%s\\n' \"$*\" >> {log:?}"));
    let document = adjust(missing_renderer_document(
        &noctalia,
        mpvpaper,
        engine,
        independent,
    ));
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, &document).unwrap();
    let parsed = loaded(&document, &[]);
    let driver = SystemDriver::with_output_source(
        parsed.renderer.clone(),
        Path::new("/wall-in-one-runtime.sock"),
        Box::new(source.clone()),
    )
    .with_workshop_home(Some(scene_home()));
    (
        Runtime::new(config_path, parsed, driver, noon()).unwrap(),
        log,
    )
}

/// The service's start-up, as `main` drives it: the first apply, the two
/// automatic retries a failure gets, and the moment after the third.
fn start_up_as_the_service_does<D: WallpaperDriver>(runtime: &mut Runtime<D>) -> Instant {
    let started = Instant::now();
    if runtime.apply_current().is_err() {
        runtime.schedule_initial_apply_retry(started);
    }
    for seconds in [2, 4, 6] {
        runtime.tick(noon(), started + Duration::from_secs(seconds));
    }
    started
}

fn wallpapers_set(log: &Path) -> Vec<String> {
    fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter(|line| line.starts_with("msg wallpaper-set "))
        .map(str::to_owned)
        .collect()
}

fn missing(program: &str, path: &Path) -> serde_json::Value {
    missing_because(program, path, "not-found")
}

fn missing_because(program: &str, path: &Path, reason: &str) -> serde_json::Value {
    serde_json::json!({"program": program, "path": path.display().to_string(), "reason": reason})
}

/// Neither a taboo nor anything else the health sync would save or chase.
fn assert_no_entry_was_blamed(snapshot: &serde_json::Value) {
    assert_eq!(
        snapshot["taboo_entries"],
        serde_json::json!([]),
        "{snapshot}"
    );
    assert_eq!(snapshot["taboo_entries_omitted"], 0, "{snapshot}");
    assert_eq!(snapshot["entry_taboo"], false, "{snapshot}");
    assert!(snapshot["automatic_retry"].is_null(), "{snapshot}");
    for row in snapshot["displays"].as_array().unwrap() {
        assert!(row["automatic_retry"].is_null(), "{row}");
        assert_eq!(row["entry_taboo"], false, "{row}");
    }
}

#[test]
fn a_missing_renderer_shows_the_still_and_is_never_a_taboo_in_mirrored_mode() {
    let root = directory("missing-renderer-mirrored");
    let mpvpaper = root.join("absent-mpvpaper");
    let engine = root.join("absent-linux-wallpaperengine");
    let (mut runtime, log) = missing_renderer_runtime(&root, &mpvpaper, &engine, false);

    // Before: the failed spawn was an apply failure, retried twice at start,
    // then the video was quarantined (and the health sync saved it).
    let started = start_up_as_the_service_does(&mut runtime);
    let shown = status(&mut runtime, noon());
    assert_no_entry_was_blamed(&shown);
    assert_eq!(shown["entry_id"], "video-a", "{shown}");
    assert_eq!(shown["motion_active"], false);
    assert_eq!(
        shown["missing_renderers"],
        serde_json::json!([missing("mpvpaper", &mpvpaper)])
    );
    // Said like a taboo's still: Play retries, and the bar says why.
    assert_eq!(shown["renderer_failed"], true);
    let error = shown["last_error"].as_str().unwrap();
    assert!(error.contains("mpvpaper is missing"), "{error}");
    assert!(error.contains(&mpvpaper.display().to_string()), "{error}");
    assert!(error.contains("No such file or directory"), "{error}");
    assert_eq!(shown["displays"][0]["renderer_failed"], true);
    // Its still is on the desktop, set once.
    assert_eq!(wallpapers_set(&log), ["msg wallpaper-set /tmp/a.png"]);

    // Rotation carries on: the scene shows its still too, for its own program.
    runtime.tick(noon(), started + Duration::from_secs(310));
    let scene = status(&mut runtime, noon());
    assert_no_entry_was_blamed(&scene);
    assert_eq!(scene["entry_id"], "scene-b", "{scene}");
    assert_eq!(
        scene["missing_renderers"],
        serde_json::json!([
            missing("mpvpaper", &mpvpaper),
            missing("linux-wallpaperengine", &engine)
        ]),
        "mpvpaper first, whatever the order they went missing in"
    );
    assert!(
        scene["last_error"]
            .as_str()
            .unwrap()
            .contains("linux-wallpaperengine is missing")
    );

    // A still needs no renderer: healthy again, and the programs are still not there.
    runtime.tick(noon(), started + Duration::from_secs(620));
    let still = status(&mut runtime, noon());
    assert_eq!(still["entry_id"], "still-c", "{still}");
    assert_eq!(still["renderer_failed"], false);
    assert_eq!(still["last_error"], "");
    assert_eq!(still["missing_renderers"].as_array().unwrap().len(), 2);
    assert_eq!(
        wallpapers_set(&log),
        [
            "msg wallpaper-set /tmp/a.png",
            "msg wallpaper-set /tmp/b.png",
            "msg wallpaper-set /tmp/c.png"
        ]
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_missing_renderer_shows_the_still_and_is_never_a_taboo_in_independent_mode() {
    let root = directory("missing-renderer-independent");
    let mpvpaper = root.join("absent-mpvpaper");
    let engine = root.join("absent-linux-wallpaperengine");
    let (mut runtime, log) = missing_renderer_runtime(&root, &mpvpaper, &engine, true);

    let started = start_up_as_the_service_does(&mut runtime);
    let shown = status(&mut runtime, noon());
    assert_no_entry_was_blamed(&shown);
    assert_eq!(
        shown["missing_renderers"],
        serde_json::json!([missing("mpvpaper", &mpvpaper)])
    );
    let video = display_status(&mut runtime, noon(), "DP-1");
    assert_eq!(video["entry_id"], "video-a", "{video}");
    assert_eq!(video["motion_active"], false);
    assert_eq!(video["renderer_failed"], true);
    assert!(
        video["last_error"]
            .as_str()
            .unwrap()
            .contains("mpvpaper is missing")
    );
    // The other display, on a still, is untouched by it.
    let other = display_status(&mut runtime, noon(), "HDMI-A-1");
    assert_eq!(other["renderer_failed"], false, "{other}");
    assert_eq!(other["last_error"], "");
    assert_eq!(applied_on(&log, "DP-1"), 1);
    assert!(wallpapers_set(&log).contains(&"msg wallpaper-set DP-1 /tmp/a.png".to_string()));

    // Rotation carries on, through the automatic path a failure used to take.
    runtime.tick(noon(), started + Duration::from_secs(310));
    let scene = status(&mut runtime, noon());
    assert_no_entry_was_blamed(&scene);
    let rotated = display_status(&mut runtime, noon(), "DP-1");
    assert_eq!(rotated["entry_id"], "scene-b", "{rotated}");
    assert!(
        rotated["last_error"]
            .as_str()
            .unwrap()
            .contains("linux-wallpaperengine is missing")
    );
    assert_eq!(scene["missing_renderers"].as_array().unwrap().len(), 2);
    assert!(wallpapers_set(&log).contains(&"msg wallpaper-set DP-1 /tmp/b.png".to_string()));
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_renderer_that_is_not_executable_is_missing_too() {
    let root = directory("missing-renderer-not-executable");
    let mpvpaper = root.join("mpvpaper");
    fs::write(&mpvpaper, "#!/bin/sh\nexec sleep 30\n").unwrap();
    let engine = root.join("absent-linux-wallpaperengine");
    let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, false);
    start_up_as_the_service_does(&mut runtime);
    let shown = status(&mut runtime, noon());
    assert_no_entry_was_blamed(&shown);
    assert_eq!(
        shown["missing_renderers"],
        serde_json::json!([missing_because("mpvpaper", &mpvpaper, "not-executable")])
    );
    assert!(
        shown["last_error"]
            .as_str()
            .unwrap()
            .contains("Permission denied"),
        "{shown}"
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_reload_starts_a_renderer_that_appeared_at_the_same_path() {
    // Arch: the document names /usr/bin/mpvpaper; the package then puts it
    // there, and the recompiled document has the very same bytes.
    for independent in [false, true] {
        let root = directory(&format!("missing-renderer-reload-{independent}"));
        let mpvpaper = root.join("mpvpaper");
        let engine = root.join("absent-linux-wallpaperengine");
        let (mut runtime, log) = missing_renderer_runtime(&root, &mpvpaper, &engine, independent);
        start_up_as_the_service_does(&mut runtime);
        assert_eq!(
            status(&mut runtime, noon())["missing_renderers"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let still_set = wallpapers_set(&log).len();

        // A reload that proves nothing changes nothing.
        assert!(runtime_command(&mut runtime, noon(), "reload", None).ok);
        assert_eq!(wallpapers_set(&log).len(), still_set, "{independent}");
        assert_eq!(
            status(&mut runtime, noon())["missing_renderers"],
            serde_json::json!([missing("mpvpaper", &mpvpaper)])
        );

        script(&mpvpaper, "exec sleep 30");
        let reloaded = without_text_file_busy(|| {
            let response = runtime_command(&mut runtime, noon(), "reload", None);
            if response.ok {
                Ok(response)
            } else {
                Err(response.message)
            }
        });
        assert!(reloaded.is_ok(), "{independent}: {reloaded:?}");
        let playing = status(&mut runtime, noon());
        assert_no_entry_was_blamed(&playing);
        assert_eq!(
            playing["missing_renderers"],
            serde_json::json!([]),
            "{independent}"
        );
        let row = if independent {
            display_status(&mut runtime, noon(), "DP-1")
        } else {
            playing["displays"][0].clone()
        };
        assert_eq!(row["entry_id"], "video-a", "{row}");
        assert_eq!(row["motion_active"], true, "{row}");
        assert_eq!(row["renderer_failed"], false, "{row}");
        assert_eq!(row["last_error"], "", "{row}");
        if independent {
            // Only the display that showed a still for it is applied again.
            assert_eq!(applied_on(&log, "HDMI-A-1"), 1);
            assert_eq!(applied_on(&log, "DP-1"), 2);
        }
        runtime.shutdown();
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn play_starts_a_renderer_that_appeared_meanwhile() {
    for independent in [false, true] {
        let root = directory(&format!("missing-renderer-play-{independent}"));
        let mpvpaper = root.join("mpvpaper");
        let engine = root.join("absent-linux-wallpaperengine");
        let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, independent);
        start_up_as_the_service_does(&mut runtime);
        script(&mpvpaper, "exec sleep 30");
        let played = without_text_file_busy(|| {
            let response = if independent {
                runtime_command(&mut runtime, noon(), "on", Some("DP-1 play"))
            } else {
                runtime_command(&mut runtime, noon(), "play", None)
            };
            if response.ok {
                Ok(response)
            } else {
                Err(response.message)
            }
        });
        assert!(played.is_ok(), "{independent}: {played:?}");
        let playing = status(&mut runtime, noon());
        assert_eq!(
            playing["missing_renderers"],
            serde_json::json!([]),
            "{independent}"
        );
        let row = if independent {
            display_status(&mut runtime, noon(), "DP-1")
        } else {
            playing["displays"][0].clone()
        };
        assert_eq!(row["motion_active"], true, "{row}");
        assert_eq!(row["renderer_failed"], false, "{row}");
        runtime.shutdown();
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn a_renderer_that_starts_and_then_exits_is_still_a_failure() {
    let root = directory("renderer-exits");
    let mpvpaper = root.join("mpvpaper");
    let engine = root.join("linux-wallpaperengine");
    script(&mpvpaper, "exit 1");
    script(&engine, "exit 1");
    let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, false);
    let started = Instant::now();
    assert!(runtime.apply_current().is_ok());
    // The video's renderer started, then exited: reported, retryable.
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        runtime.tick(noon(), started);
        let snapshot = status(&mut runtime, noon());
        if snapshot["renderer_failed"] == true || Instant::now() > deadline {
            assert!(
                snapshot["last_error"]
                    .as_str()
                    .unwrap()
                    .contains("because mpvpaper exited"),
                "{snapshot}"
            );
            // Always present: empty while nothing is missing.
            assert_eq!(snapshot["missing_renderers"], serde_json::json!([]));
            assert_eq!(snapshot["taboo_entries"], serde_json::json!([]));
            break;
        }
        thread::sleep(Duration::from_millis(20));
    }
    // The scene's renderer started, then exited: quarantined for the
    // session, as a crashed scene always was.
    assert!(runtime_command(&mut runtime, noon(), "next", None).ok);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        runtime.tick(noon(), started);
        let snapshot = status(&mut runtime, noon());
        let taboo = snapshot["taboo_entries"].as_array().unwrap().clone();
        if !taboo.is_empty() || Instant::now() > deadline {
            assert_eq!(taboo.len(), 1, "{snapshot}");
            assert_eq!(taboo[0]["entry_id"], "scene-b");
            assert_eq!(taboo[0]["source"], "renderer-crash");
            assert_eq!(snapshot["missing_renderers"], serde_json::json!([]));
            break;
        }
        thread::sleep(Duration::from_millis(20));
    }
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

/// Two records at the longest path a document can name, every byte escaped
/// twice.
fn longest_missing_renderers() -> Vec<MissingRenderer> {
    let longest = format!("/{}", "\"".repeat(4095));
    ["mpvpaper", "linux-wallpaperengine"]
        .into_iter()
        .map(|program| MissingRenderer {
            program,
            path: longest.clone(),
            error: "No such file or directory (os error 2)".into(),
            reason: MissingReason::NotFound,
            identity: PathIdentity::Absent,
        })
        .collect()
}

#[test]
fn a_missing_renderers_path_is_cut_at_1024_bytes() {
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        missing_renderers: longest_missing_renderers(),
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
        noon(),
    )
    .unwrap();
    let snapshot = status(&mut runtime, noon());
    let reported = snapshot["missing_renderers"].as_array().unwrap();
    assert_eq!(reported.len(), 2);
    assert_eq!(reported[0]["program"], "mpvpaper");
    assert_eq!(reported[1]["program"], "linux-wallpaperengine");
    for record in reported {
        let path = record["path"].as_str().unwrap();
        assert!(path.len() <= 1024, "{} bytes", path.len());
        assert!(path.contains("[truncated]"));
        assert_eq!(record["reason"], "not-found");
    }
}

/// The status at the schema's ceilings, with 64 published monitors and
/// `missing`: its taboo records kept and left out, its `outputs`, and its
/// `missing_renderers`.
fn ceiling_status_with_missing(
    missing: Vec<MissingRenderer>,
) -> (usize, usize, usize, serde_json::Value) {
    let monitors: Vec<Output> = (0..64)
        .map(|index| Output {
            name: format!("DP-{index}"),
            enabled: true,
            make: "Dell Inc.".into(),
            model: "DELL U2720Q".into(),
            width: 2560,
            height: 1440,
            scale: 1.5,
            mode_width: 3840,
            mode_height: 2160,
            refresh_millihz: Some(59_997),
            physical_width_mm: 597,
            physical_height_mm: 336,
            ..Output::default()
        })
        .collect();
    RUNTIME_DRIVER_PUBLISHED.with(|cell| cell.set(Box::leak(monitors.into_boxed_slice())));
    let at = noon();
    let parsed = Config::from_bytes(status_ceiling_document().into_bytes()).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(
            (0..64)
                .map(|index| format!("{}L{index:02}", "\"".repeat(253)))
                .collect(),
        ),
        missing_renderers: missing,
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
    runtime.tick(at, Instant::now());
    let response = runtime_command(&mut runtime, at, "status", None);
    RUNTIME_DRIVER_PUBLISHED.with(|cell| cell.set(&[]));
    assert!(response.ok, "{}", response.message);
    let mut encoded = Vec::new();
    write_response(&mut encoded, &response).unwrap();
    assert!(
        encoded.len() <= MAX_RESPONSE_BYTES,
        "{} bytes",
        encoded.len()
    );
    let snapshot: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    assert_eq!(snapshot["displays"].as_array().unwrap().len(), 128);
    assert_eq!(snapshot["playlists"].as_array().unwrap().len(), 65);
    (
        snapshot["taboo_entries"].as_array().unwrap().len(),
        snapshot["taboo_entries_omitted"].as_u64().unwrap() as usize,
        snapshot["outputs"].as_array().unwrap().len(),
        snapshot["missing_renderers"].clone(),
    )
}

#[test]
fn missing_renderers_never_displace_taboo_records_at_the_ceiling() {
    let (kept, omitted, outputs, none) = ceiling_status_with_missing(Vec::new());
    assert!(omitted > 0, "the ceiling leaves records out");
    assert_eq!(none, serde_json::json!([]));
    let (kept_with, omitted_with, outputs_with, rows) =
        ceiling_status_with_missing(longest_missing_renderers());
    assert_eq!(
        (kept_with, omitted_with, outputs_with),
        (kept, omitted, outputs),
        "missing_renderers pushed taboo records or outputs out"
    );
    // What room is left: the rows without their paths, or none, in order.
    let rows = rows.as_array().unwrap();
    assert!(rows.len() == 2 || rows.is_empty(), "{rows:?}");
    for (row, program) in rows.iter().zip(["mpvpaper", "linux-wallpaperengine"]) {
        assert_eq!(row["program"], program);
        assert_eq!(row["reason"], "not-found");
        assert!(row["path"].as_str().unwrap().len() <= 1024);
    }
}

#[test]
fn a_program_that_cannot_be_executed_is_a_failure_not_a_missing_renderer() {
    // There, executable, and not a program: exec says "Exec format error",
    // which no install fixes. It stays an apply failure, retried and then
    // quarantined like any other (the VM test's health persistence uses it).
    use std::os::unix::fs::PermissionsExt;
    let root = directory("renderer-not-a-program");
    let mpvpaper = root.join("mpvpaper");
    fs::write(&mpvpaper, "not a program\n").unwrap();
    fs::set_permissions(&mpvpaper, fs::Permissions::from_mode(0o755)).unwrap();
    let engine = root.join("absent-linux-wallpaperengine");
    let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, false);
    let error = runtime.apply_current().unwrap_err();
    assert!(error.contains("cannot start mpvpaper"), "{error}");
    let shown = status(&mut runtime, noon());
    assert_eq!(shown["missing_renderers"], serde_json::json!([]), "{shown}");
    assert_eq!(shown["renderer_failed"], true);
    let started = Instant::now();
    assert!(runtime.schedule_initial_apply_retry(started));
    runtime.tick(noon(), started + Duration::from_secs(2));
    runtime.tick(noon(), started + Duration::from_secs(4));
    let quarantined = status(&mut runtime, noon());
    assert_eq!(
        quarantined["taboo_entries"][0]["entry_id"], "video-a",
        "{quarantined}"
    );
    assert_eq!(quarantined["taboo_entries"][0]["source"], "automatic-apply");
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_display_that_leaves_drops_its_missing_renderer_mark() {
    // Both displays assigned, in either mode: DP-1 to a playlist that starts
    // with a video, HDMI-A-1 to stills. Independent mode keeps a route for the
    // detached DP-1; neither mode may re-apply anything for it, or for the
    // display that never needed the renderer, when the program appears.
    for independent in [false, true] {
        let root = directory(&format!("missing-renderer-unplugged-{independent}"));
        let mpvpaper = root.join("mpvpaper");
        let engine = root.join("absent-linux-wallpaperengine");
        let both = vec![live("DP-1", None), live("HDMI-A-1", None)];
        let source = SwitchableOutputs::default();
        source.set(both.clone());
        let (mut runtime, log) = missing_renderer_runtime_over(
            &root,
            &mpvpaper,
            &engine,
            true,
            |document| {
                if independent {
                    document
                } else {
                    document.replace(
                        "display_mode = \"independent\"",
                        "display_mode = \"mirrored\"",
                    )
                }
            },
            &source,
        );
        let started = start_up_as_the_service_does(&mut runtime);
        let shown = display_status(&mut runtime, noon(), "DP-1");
        assert_eq!(shown["entry_id"], "video-a", "{independent}: {shown}");
        assert_eq!(shown["motion_active"], false);
        assert_eq!(
            status(&mut runtime, noon())["missing_renderers"]
                .as_array()
                .unwrap()
                .len(),
            1
        );

        // DP-1 is unplugged: what it showed, and why, go with it.
        source.set(vec![live("HDMI-A-1", None)]);
        runtime.tick(noon(), started + Duration::from_secs(12));
        let detached = display_status(&mut runtime, noon(), "DP-1");
        assert_eq!(detached["connected"], false, "{independent}: {detached}");
        let hdmi = applied_on(&log, "HDMI-A-1");

        script(&mpvpaper, "exec sleep 30");
        let reloaded = without_text_file_busy(|| {
            let response = runtime_command(&mut runtime, noon(), "reload", None);
            if response.ok {
                Ok(response)
            } else {
                Err(response.message)
            }
        });
        assert!(reloaded.is_ok(), "{independent}: {reloaded:?}");
        assert_eq!(
            applied_on(&log, "HDMI-A-1"),
            hdmi,
            "{independent}: the display on a still never needed it"
        );
        assert_eq!(
            applied_on(&log, "DP-1"),
            1,
            "{independent}: nor the detached one"
        );
        assert_eq!(
            status(&mut runtime, noon())["missing_renderers"],
            serde_json::json!([]),
            "{independent}"
        );

        // Plugged back in: applied again, and the program starts.
        source.set(both);
        runtime.tick(noon(), started + Duration::from_secs(24));
        let back = display_status(&mut runtime, noon(), "DP-1");
        assert_eq!(back["connected"], true, "{independent}: {back}");
        assert_eq!(back["motion_active"], true, "{independent}: {back}");
        assert_eq!(back["renderer_failed"], false, "{independent}: {back}");
        runtime.shutdown();
        fs::remove_dir_all(root).unwrap();
    }
}

fn reloaded<D: WallpaperDriver>(runtime: &mut Runtime<D>) -> Response {
    let answer = without_text_file_busy(|| {
        let response = runtime_command(runtime, noon(), "reload", None);
        if response.ok {
            Ok(response)
        } else {
            Err(response.message)
        }
    });
    answer.unwrap_or_else(|message| panic!("reload refused: {message}"))
}

#[test]
fn a_reload_recovers_every_display_left_on_a_still_after_another_started_the_program() {
    // Review probe A1. Both displays on the playlist that starts with the
    // video; Play on DP-1 starts the program that appeared, which clears the
    // service's record, and HDMI-A-1 must still come back on the reload.
    let root = directory("missing-renderer-sibling");
    let mpvpaper = root.join("mpvpaper");
    let engine = root.join("absent-linux-wallpaperengine");
    let source = SwitchableOutputs::default();
    source.set(vec![live("DP-1", None), live("HDMI-A-1", None)]);
    let (mut runtime, _log) = missing_renderer_runtime_over(
        &root,
        &mpvpaper,
        &engine,
        true,
        |document| {
            document.replace(
                "connector = \"HDMI-A-1\"\nplaylist = \"stills\"",
                "connector = \"HDMI-A-1\"\nplaylist = \"clips\"",
            )
        },
        &source,
    );
    start_up_as_the_service_does(&mut runtime);
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = display_status(&mut runtime, noon(), connector);
        assert_eq!(row["entry_id"], "video-a", "{row}");
        assert_eq!(row["renderer_failed"], true, "{row}");
    }
    script(&mpvpaper, "exec sleep 30");
    let played = without_text_file_busy(|| {
        let response = runtime_command(&mut runtime, noon(), "on", Some("DP-1 play"));
        if response.ok {
            Ok(response)
        } else {
            Err(response.message)
        }
    });
    assert!(played.is_ok(), "{played:?}");
    assert_eq!(
        status(&mut runtime, noon())["missing_renderers"],
        serde_json::json!([])
    );
    reloaded(&mut runtime);
    let other = display_status(&mut runtime, noon(), "HDMI-A-1");
    assert_eq!(other["motion_active"], true, "{other}");
    assert_eq!(other["renderer_failed"], false, "{other}");
    assert_eq!(other["last_error"], "", "{other}");
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_reload_that_changes_nothing_changes_nothing_for_a_renderer_that_cannot_run() {
    // Review probe A2: an executable whose interpreter is missing. The same
    // file can only fail the same way, so a reload leaves it be until the
    // file changes, and then tries it again.
    use std::os::unix::fs::PermissionsExt;
    for independent in [false, true] {
        let root = directory(&format!("missing-renderer-cannot-run-{independent}"));
        let mpvpaper = root.join("mpvpaper");
        fs::write(&mpvpaper, "#!/nonexistent/interpreter\n").unwrap();
        fs::set_permissions(&mpvpaper, fs::Permissions::from_mode(0o755)).unwrap();
        let engine = root.join("absent-linux-wallpaperengine");
        let (mut runtime, log) = missing_renderer_runtime(&root, &mpvpaper, &engine, independent);
        start_up_as_the_service_does(&mut runtime);
        let shown = status(&mut runtime, noon());
        assert_eq!(
            shown["missing_renderers"],
            serde_json::json!([missing_because("mpvpaper", &mpvpaper, "cannot-run")]),
            "{independent}: {shown}"
        );
        let video = if independent {
            display_status(&mut runtime, noon(), "DP-1")
        } else {
            shown["displays"][0].clone()
        };
        let error = video["last_error"].as_str().unwrap();
        assert!(error.contains("mpvpaper cannot run here"), "{error}");
        let calls = fs::read_to_string(&log).unwrap().lines().count();
        for _ in 0..3 {
            reloaded(&mut runtime);
        }
        assert_eq!(
            fs::read_to_string(&log).unwrap().lines().count(),
            calls,
            "{independent}: reloads that changed nothing ran noctalia again"
        );
        // A fixed program is a changed file: the next reload starts it.
        script(&mpvpaper, "exec sleep 30");
        reloaded(&mut runtime);
        let playing = status(&mut runtime, noon());
        assert_eq!(
            playing["missing_renderers"],
            serde_json::json!([]),
            "{independent}"
        );
        let video = if independent {
            display_status(&mut runtime, noon(), "DP-1")
        } else {
            playing["displays"][0].clone()
        };
        assert_eq!(video["motion_active"], true, "{independent}: {video}");
        runtime.shutdown();
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn a_no_op_reload_never_restarts_a_healthy_scene_on_another_display() {
    // Review probe A3: mirrored, with assignments. DP-1's mpvpaper cannot
    // run; HDMI-A-1's scene runs and must not be torn down and started again
    // by each reload that changes nothing.
    use std::os::unix::fs::PermissionsExt;
    let root = directory("missing-renderer-healthy-scene");
    let mpvpaper = root.join("mpvpaper");
    fs::write(&mpvpaper, "#!/nonexistent/interpreter\n").unwrap();
    fs::set_permissions(&mpvpaper, fs::Permissions::from_mode(0o755)).unwrap();
    let engine = root.join("linux-wallpaperengine");
    let starts = root.join("engine-starts.log");
    script(&engine, &format!("echo start >> {starts:?}; exec sleep 30"));
    let source = SwitchableOutputs::default();
    source.set(vec![live("DP-1", None), live("HDMI-A-1", None)]);
    let (mut runtime, _log) = missing_renderer_runtime_over(
        &root,
        &mpvpaper,
        &engine,
        true,
        |document| {
            document
                .replace(
                    "display_mode = \"independent\"",
                    "display_mode = \"mirrored\"",
                )
                .replace(
                    "connector = \"HDMI-A-1\"\nplaylist = \"stills\"",
                    "connector = \"HDMI-A-1\"\nplaylist = \"scenes\"",
                )
                + "[[playlists]]\nid = \"scenes\"\nname = \"Scenes\"\n\
                   [[playlists.entries]]\nid = \"scene-x\"\nkind = \"scene\"\n\
                   still = \"/tmp/x.png\"\nscene_id = \"777\"\n\
                   palette = { kind = \"keep\", mode = \"keep\" }\n"
        },
        &source,
    );
    without_text_file_busy(|| runtime.apply_current()).unwrap();
    let count = |path: &Path| fs::read_to_string(path).unwrap_or_default().lines().count();
    let deadline = Instant::now() + Duration::from_secs(5);
    while count(&starts) < 1 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(count(&starts), 1, "the scene started once");
    for _ in 0..3 {
        reloaded(&mut runtime);
    }
    thread::sleep(Duration::from_millis(500));
    assert_eq!(
        count(&starts),
        1,
        "a reload that changed nothing restarted it"
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_renderer_whose_libraries_are_missing_is_a_missing_renderer_not_a_crashed_scene() {
    // Review probe A5: the loader exits 127 before the renderer runs; the
    // scene is not quarantined, and the reason names the library.
    let root = directory("renderer-missing-library");
    let mpvpaper = root.join("absent-mpvpaper");
    let engine = root.join("linux-wallpaperengine");
    script(
        &engine,
        "echo 'linux-wallpaperengine: error while loading shared libraries: libboost_filesystem.so.1.83.0: cannot open shared object file: No such file or directory' >&2; exit 127",
    );
    let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, false);
    let started = Instant::now();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, noon(), "next", None).ok);
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut snapshot = status(&mut runtime, noon());
    while Instant::now() < deadline {
        runtime.tick(noon(), started);
        snapshot = status(&mut runtime, noon());
        if snapshot["missing_renderers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["program"] == "linux-wallpaperengine")
        {
            break;
        }
        thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(snapshot["entry_id"], "scene-b", "{snapshot}");
    assert_no_entry_was_blamed(&snapshot);
    assert_eq!(
        snapshot["missing_renderers"],
        serde_json::json!([
            missing("mpvpaper", &mpvpaper),
            missing_because("linux-wallpaperengine", &engine, "cannot-run")
        ])
    );
    assert_eq!(snapshot["renderer_failed"], true);
    let error = snapshot["last_error"].as_str().unwrap();
    assert!(
        error.contains("linux-wallpaperengine cannot run here"),
        "{error}"
    );
    assert!(
        error.contains("error while loading shared libraries: libboost_filesystem"),
        "{error}"
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_renderer_that_exits_127_after_it_ran_is_still_a_crash() {
    // Past `LOADER_FAILURE_WINDOW` a 127 is the renderer's own doing.
    let root = directory("renderer-late-127");
    let mpvpaper = root.join("absent-mpvpaper");
    let engine = root.join("linux-wallpaperengine");
    let window = wall_in_one_service::renderer::LOADER_FAILURE_WINDOW;
    script(
        &engine,
        &format!("sleep {}; exit 127", window.as_secs() + 1),
    );
    let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, false);
    let started = Instant::now();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, noon(), "next", None).ok);
    let deadline = Instant::now() + window + Duration::from_secs(10);
    let mut snapshot = status(&mut runtime, noon());
    while Instant::now() < deadline {
        runtime.tick(noon(), started);
        snapshot = status(&mut runtime, noon());
        if !snapshot["taboo_entries"].as_array().unwrap().is_empty() {
            break;
        }
        thread::sleep(Duration::from_millis(50));
    }
    assert_eq!(
        snapshot["taboo_entries"][0]["entry_id"], "scene-b",
        "{snapshot}"
    );
    assert_eq!(snapshot["taboo_entries"][0]["source"], "renderer-crash");
    assert_eq!(
        snapshot["missing_renderers"],
        serde_json::json!([missing("mpvpaper", &mpvpaper)])
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_command_onto_a_wallpaper_whose_renderer_is_missing_says_only_its_still_shows() {
    for independent in [false, true] {
        let root = directory(&format!("missing-renderer-reply-{independent}"));
        let mpvpaper = root.join("absent-mpvpaper");
        let engine = root.join("absent-linux-wallpaperengine");
        let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, independent);
        runtime.apply_current().unwrap();
        // video-a, then next (the scene), then next (the still).
        let (verb, argument) = if independent {
            ("on", Some("DP-1 next"))
        } else {
            ("next", None)
        };
        let scene = runtime_command(&mut runtime, noon(), verb, argument);
        assert!(scene.ok, "{}", scene.message);
        assert!(
            scene.message.contains("still")
                && scene.message.contains("linux-wallpaperengine is missing"),
            "{independent}: {}",
            scene.message
        );
        let still = runtime_command(&mut runtime, noon(), verb, argument);
        assert!(still.ok, "{}", still.message);
        assert!(
            !still.message.contains("is missing"),
            "{independent}: {}",
            still.message
        );
        runtime.shutdown();
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn a_reload_refused_for_a_display_says_which_and_keeps_the_configuration() {
    // Review probe A6: a detached display assigned a playlist with nothing
    // in it. The reload is refused, and the configuration kept. Since schema
    // 8 a playlist with no entries plays nothing on purpose, with its reason;
    // below 8 it is still refused, now by the load, which names it.
    let root = directory("reload-refused-detached");
    let mpvpaper = root.join("absent-mpvpaper");
    let engine = root.join("absent-linux-wallpaperengine");
    let source = SwitchableOutputs::default();
    source.set(vec![live("DP-1", None)]);
    let (mut runtime, _log) = missing_renderer_runtime_over(
        &root,
        &mpvpaper,
        &engine,
        true,
        |document| document,
        &source,
    );
    start_up_as_the_service_does(&mut runtime);
    let config_path = root.join("runtime.toml");
    let document = fs::read_to_string(&config_path).unwrap().replace(
        "connector = \"HDMI-A-1\"\nplaylist = \"stills\"",
        "connector = \"HDMI-A-1\"\nplaylist = \"later\"",
    ) + "[[playlists]]\nid = \"later\"\nname = \"Later\"\nentries = []\n";
    fs::write(&config_path, document).unwrap();
    let response = runtime_command(&mut runtime, noon(), "reload", None);
    assert!(!response.ok);
    assert!(
        response
            .message
            .contains("playlist \"Later\" has no entries")
            && response.message.contains("requires schema_version 8"),
        "{}",
        response.message
    );
    assert_eq!(
        display_status(&mut runtime, noon(), "HDMI-A-1")["playlist_id"],
        "stills"
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

/// The missing list, by program, as a status reports it.
fn missing_programs(snapshot: &serde_json::Value) -> Vec<String> {
    snapshot["missing_renderers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["program"].as_str().unwrap().to_string())
        .collect()
}

#[test]
fn a_command_onto_a_renderer_that_cannot_run_keeps_it_listed() {
    // Re-review probe B1. A spawn that succeeds used to clear the record
    // before the 127 that follows was polled, so the status the window asks
    // for right after a command said the program could run, and the next tick
    // said it could not: the list flickered, and the windows said it again.
    let root = directory("loader-failure-listed");
    let mpvpaper = root.join("mpvpaper");
    script(
        &mpvpaper,
        "echo 'mpvpaper: error while loading shared libraries: libmpv.so.2: cannot open shared object file: No such file or directory' >&2; exit 127",
    );
    let engine = root.join("absent-linux-wallpaperengine");
    let source = SwitchableOutputs::default();
    source.set(vec![live("DP-1", None), live("HDMI-A-1", None)]);
    let (mut runtime, _log) = missing_renderer_runtime_over(
        &root,
        &mpvpaper,
        &engine,
        true,
        |document| {
            document.replace(
                "connector = \"HDMI-A-1\"\nplaylist = \"stills\"",
                "connector = \"HDMI-A-1\"\nplaylist = \"scenes\"",
            ) + "[[playlists]]\nid = \"scenes\"\nname = \"Scenes\"\n\
                 [[playlists.entries]]\nid = \"scene-x\"\nkind = \"scene\"\n\
                 still = \"/tmp/x.png\"\nscene_id = \"777\"\n\
                 palette = { kind = \"keep\", mode = \"keep\" }\n"
        },
        &source,
    );
    let started = Instant::now();
    without_text_file_busy(|| runtime.apply_current()).unwrap();
    let settle = |runtime: &mut Runtime<SystemDriver>| {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            runtime.tick(noon(), started);
            let snapshot = status(runtime, noon());
            if missing_programs(&snapshot).len() == 2 || Instant::now() > deadline {
                return snapshot;
            }
            thread::sleep(Duration::from_millis(20));
        }
    };
    let both = ["mpvpaper", "linux-wallpaperengine"];
    assert_eq!(missing_programs(&settle(&mut runtime)), both);
    for _ in 0..2 {
        let played = without_text_file_busy(|| {
            let response = runtime_command(&mut runtime, noon(), "on", Some("DP-1 play"));
            if response.ok {
                Ok(response)
            } else {
                Err(response.message)
            }
        });
        assert!(played.is_ok(), "{played:?}");
        let right_after = status(&mut runtime, noon());
        assert_eq!(missing_programs(&right_after), both, "the list flickered");
        let after = settle(&mut runtime);
        assert_eq!(missing_programs(&after), both);
        assert_eq!(
            after["missing_renderers"][0]["reason"], "cannot-run",
            "{after}"
        );
    }
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_program_recorded_as_unable_to_run_that_then_runs_is_cleared() {
    // The record kept across a spawn goes once a child outlives the window.
    let root = directory("loader-failure-then-runs");
    let mpvpaper = root.join("mpvpaper");
    let flag = root.join("broken");
    fs::write(&flag, "").unwrap();
    script(
        &mpvpaper,
        &format!(
            "if [ -e {flag:?} ]; then echo 'mpvpaper: error while loading shared libraries: libmpv.so.2: cannot open shared object file' >&2; exit 127; fi; exec sleep 30"
        ),
    );
    let engine = root.join("absent-linux-wallpaperengine");
    let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, false);
    let started = Instant::now();
    without_text_file_busy(|| runtime.apply_current()).unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    while missing_programs(&status(&mut runtime, noon())).is_empty() && Instant::now() < deadline {
        runtime.tick(noon(), started);
        thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        missing_programs(&status(&mut runtime, noon())),
        ["mpvpaper"]
    );
    // The library arrives (the same binary); Play starts it, and it runs.
    fs::remove_file(&flag).unwrap();
    assert!(runtime_command(&mut runtime, noon(), "play", None).ok);
    assert_eq!(
        missing_programs(&status(&mut runtime, noon())),
        ["mpvpaper"],
        "kept while it might still exit 127"
    );
    thread::sleep(
        wall_in_one_service::renderer::LOADER_FAILURE_WINDOW + Duration::from_millis(300),
    );
    runtime.tick(noon(), started);
    let running = status(&mut runtime, noon());
    assert_eq!(
        running["missing_renderers"],
        serde_json::json!([]),
        "{running}"
    );
    assert_eq!(running["motion_active"], true, "{running}");
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_loader_failure_noticed_late_is_still_not_a_crashed_scene() {
    // Re-review probe B2: the window was measured when the exit was polled,
    // and a loop held past it (two slow Noctalia calls, say) made the scene a
    // crash again. The loader's own line decides, whenever it is noticed.
    let root = directory("loader-failure-late");
    let mpvpaper = root.join("absent-mpvpaper");
    let engine = root.join("linux-wallpaperengine");
    script(
        &engine,
        "echo 'linux-wallpaperengine: error while loading shared libraries: libboost_filesystem.so.1.83.0: cannot open shared object file: No such file or directory' >&2; exit 127",
    );
    let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, false);
    let started = Instant::now();
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, noon(), "next", None).ok);
    thread::sleep(
        wall_in_one_service::renderer::LOADER_FAILURE_WINDOW + Duration::from_millis(300),
    );
    runtime.tick(noon(), started + Duration::from_secs(6));
    let snapshot = status(&mut runtime, noon());
    assert_no_entry_was_blamed(&snapshot);
    assert_eq!(
        snapshot["missing_renderers"],
        serde_json::json!([
            missing("mpvpaper", &mpvpaper),
            missing_because("linux-wallpaperengine", &engine, "cannot-run")
        ])
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

/// `program` (`mpvpaper` or `linux-wallpaperengine`) fails in the loader in
/// mirrored mode, with a Noctalia that refuses to put the still back once it
/// has started when `still_fails`. The status once the exit was polled.
fn a_loader_failure_and_the_still_put_back(program: &str, still_fails: bool) -> serde_json::Value {
    let root = directory(&format!("loader-failure-still-{program}-{still_fails}"));
    let loader = format!(
        "echo '{program}: error while loading shared libraries: libx.so.1: cannot open shared object file: No such file or directory' >&2; exit 127"
    );
    let (mpvpaper, engine) = if program == "mpvpaper" {
        let mpvpaper = root.join("mpvpaper");
        script(&mpvpaper, &loader);
        (mpvpaper, root.join("absent-linux-wallpaperengine"))
    } else {
        let engine = root.join("linux-wallpaperengine");
        script(&engine, &loader);
        (root.join("absent-mpvpaper"), engine)
    };
    // Every call is logged; `wallpaper-set` fails while `refuse` exists.
    let log = root.join("noctalia.log");
    let refuse = root.join("refuse");
    let picky = root.join("picky-noctalia");
    script(
        &picky,
        &format!(
            "printf '%s\\n' \"$*\" >> {log:?}\nif [ -e {refuse:?} ] && [ \"$2\" = wallpaper-set ]; then echo 'the still was refused' >&2; exit 1; fi"
        ),
    );
    let source = SwitchableOutputs::default();
    source.set(vec![live("DP-1", None), live("HDMI-A-1", None)]);
    let ordinary = format!("noctalia_program = {:?}", root.join("noctalia"));
    let (mut runtime, _) = missing_renderer_runtime_over(
        &root,
        &mpvpaper,
        &engine,
        false,
        |document| {
            assert!(document.contains(&ordinary));
            document.replace(&ordinary, &format!("noctalia_program = {picky:?}"))
        },
        &source,
    );
    let started = Instant::now();
    without_text_file_busy(|| runtime.apply_current()).unwrap();
    if program != "mpvpaper" {
        // Off the video (its program is absent) onto the scene.
        let next = without_text_file_busy(|| {
            let response = runtime_command(&mut runtime, noon(), "next", None);
            if response.ok {
                Ok(response)
            } else {
                Err(response.message)
            }
        });
        assert!(next.is_ok(), "{next:?}");
    }
    if still_fails {
        fs::write(&refuse, "").unwrap();
    }
    let calls = wallpapers_set(&log).len();
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut snapshot = status(&mut runtime, noon());
    while !missing_programs(&snapshot)
        .iter()
        .any(|name| name == program)
        && Instant::now() < deadline
    {
        runtime.tick(noon(), started);
        snapshot = status(&mut runtime, noon());
        thread::sleep(Duration::from_millis(20));
    }
    assert!(
        wallpapers_set(&log).len() > calls,
        "the still was not put back: {snapshot}"
    );
    assert_no_entry_was_blamed(&snapshot);
    let path = if program == "mpvpaper" {
        &mpvpaper
    } else {
        &engine
    };
    assert!(
        snapshot["missing_renderers"]
            .as_array()
            .unwrap()
            .contains(&missing_because(program, path, "cannot-run")),
        "{snapshot}"
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
    snapshot
}

/// Codex review R40-6: the loader's 127 was reported with the record's own
/// words alone, which say the still is showing, and the still's own failure
/// was dropped. An ordinary crash already kept it.
fn the_still_that_could_not_be_put_back_is_said(program: &str) {
    let snapshot = a_loader_failure_and_the_still_put_back(program, true);
    let error = snapshot["last_error"].as_str().unwrap();
    assert!(
        error.contains(&format!("{program} cannot run here: ")),
        "{error}"
    );
    assert!(
        error.contains("error while loading shared libraries: libx.so.1"),
        "{error}"
    );
    assert!(
        error.contains("could not reapply the still: ") && error.contains("the still was refused"),
        "{error}"
    );
    assert!(!error.contains("paired still is active"), "{error}");
}

#[test]
fn a_video_renderer_that_cannot_run_says_when_its_still_could_not_be_put_back() {
    the_still_that_could_not_be_put_back_is_said("mpvpaper");
}

#[test]
fn a_scene_renderer_that_cannot_run_says_when_its_still_could_not_be_put_back() {
    the_still_that_could_not_be_put_back_is_said("linux-wallpaperengine");
}

#[test]
fn a_renderer_that_cannot_run_over_its_still_says_the_still_is_showing() {
    // The control for R40-6: the still was put back, and that is all it adds.
    for program in ["mpvpaper", "linux-wallpaperengine"] {
        let snapshot = a_loader_failure_and_the_still_put_back(program, false);
        let error = snapshot["last_error"].as_str().unwrap();
        assert!(
            error.contains(&format!("{program} cannot run here: ")),
            "{error}"
        );
        assert!(error.ends_with("; paired still is active"), "{error}");
        assert!(!error.contains("could not reapply"), "{error}");
    }
}

#[test]
fn a_renderer_behind_a_folder_the_service_cannot_enter_is_no_access() {
    // Re-review nit N3: a parent folder that cannot be searched used to read
    // as "not-executable", which says nothing true about the program.
    use std::os::unix::fs::PermissionsExt;
    if unsafe { libc::geteuid() } == 0 {
        return; // root enters every folder
    }
    let root = directory("renderer-no-access");
    let locked = root.join("locked");
    fs::create_dir(&locked).unwrap();
    let mpvpaper = locked.join("mpvpaper");
    script(&mpvpaper, "exec sleep 30");
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o600)).unwrap();
    let engine = root.join("absent-linux-wallpaperengine");
    let (mut runtime, _log) = missing_renderer_runtime(&root, &mpvpaper, &engine, false);
    start_up_as_the_service_does(&mut runtime);
    let shown = status(&mut runtime, noon());
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o700)).unwrap();
    assert_no_entry_was_blamed(&shown);
    assert_eq!(
        shown["missing_renderers"],
        serde_json::json!([missing_because("mpvpaper", &mpvpaper, "no-access")]),
        "{shown}"
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
}

// -- r13: a playlist that plays nothing, and web pages while web is off ----------------------
//
// The four documents are the app's own (`tests/test_web_off_nothing_to_play.py` keeps them
// what its compiler writes): a library whose only playable item is the web page 2777242023,
// and the same with a still beside it, each compiled with "Allow web wallpapers" on, then off.

const WEB_ONLY_ON: &str = include_str!("fixtures/web-off/web-only-on.toml");
const WEB_ONLY_OFF: &str = include_str!("fixtures/web-off/web-only-off.toml");
const WITH_STILL_ON: &str = include_str!("fixtures/web-off/with-still-on.toml");
const WITH_STILL_OFF: &str = include_str!("fixtures/web-off/with-still-off.toml");
const WEB_PAGE: &str = "2777242023";

/// `on` as the app publishes it for a service not confirmed to read schema 8: without
/// `allow_web_wallpapers` (the step-down). A service that predates 8, as at c8c7857, loads
/// it, so these tests compare the same document before and after the change.
fn stepped_down(on: &str) -> String {
    on.replace("schema_version = 8", "schema_version = 4")
        .replace("allow_web_wallpapers = true\n", "")
}

/// A test driver over the app's `document`, applied: the runtime, its state and the page's
/// entry id.
fn web_runtime(
    label: &str,
    document: &str,
) -> (
    Runtime<RuntimeDriver>,
    Arc<Mutex<RuntimeDriverState>>,
    PathBuf,
    String,
) {
    let root = directory(label);
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, document).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let page = parsed
        .playlists
        .iter()
        .flat_map(|playlist| playlist.entries.iter())
        .find(|entry| entry.scene_id.as_deref() == Some(WEB_PAGE))
        .map(|entry| entry.id.clone())
        .unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(
        config_path.clone(),
        parsed,
        RuntimeDriver(state.clone()),
        noon(),
    )
    .unwrap();
    runtime.apply_current().unwrap();
    (runtime, state, config_path, page)
}

#[test]
fn the_apps_web_off_document_ends_the_page_when_web_pages_were_all_that_played() {
    // Review probe `rust_probe_web_off_reload.rs` (M1). Before: the document had no
    // `entries`, the reload was refused ("missing field `entries`") and the page played on.
    let (mut runtime, state, config_path, page) =
        web_runtime("web-only-off", &stepped_down(WEB_ONLY_ON));
    assert_eq!(status(&mut runtime, noon())["entry_id"], page.as_str());
    assert!(
        state.lock().unwrap().motion_active,
        "the page plays while it is allowed"
    );

    fs::write(&config_path, WEB_ONLY_OFF).unwrap();
    let reply = runtime_command(&mut runtime, noon(), "reload", None);
    assert!(reply.ok, "{}", reply.message);
    let after = status(&mut runtime, noon());
    assert!(after["entry_id"].is_null(), "{after}");
    assert_eq!(after["nothing_to_play"], "web wallpapers are off");
    assert_eq!(after["last_error"], "", "nothing to play is not a fault");
    assert_eq!(after["renderer_failed"], false);
    let row = &after["displays"][0];
    assert_eq!(row["connector"], "ALL");
    assert_eq!(row["entry_id"], "");
    assert_eq!(row["kind"], "");
    assert_eq!(row["still"], "");
    assert_eq!(row["motion_active"], false);
    assert_eq!(row["nothing_to_play"], "web wallpapers are off");
    assert_eq!(row["entry_name"], "Nothing to play: web wallpapers are off");
    assert!(row["next_cycle_in_s"].is_null(), "{row}");
    assert_eq!(after["playlists"][0]["entries"], 0);
    let driver = state.lock().unwrap();
    assert!(
        driver.active_outputs.is_empty(),
        "{:?}",
        driver.active_outputs
    );
    assert!(!driver.motion_active);
    assert_eq!(driver.retained_outputs.last(), Some(&Vec::new()));
}

#[test]
fn control_with_a_still_beside_the_page_the_still_plays() {
    // Review probe `rust_probe_web_off_reload_control.rs`: passed before and still does.
    let (mut runtime, state, config_path, page) =
        web_runtime("with-still-off", &stepped_down(WITH_STILL_ON));
    assert_eq!(status(&mut runtime, noon())["entry_id"], page.as_str());
    fs::write(&config_path, WITH_STILL_OFF).unwrap();
    let reply = runtime_command(&mut runtime, noon(), "reload", None);
    assert!(reply.ok, "{}", reply.message);
    let after = status(&mut runtime, noon());
    assert_ne!(after["entry_id"], page.as_str());
    assert_eq!(after["kind"], "still");
    assert!(after["nothing_to_play"].is_null());
    assert!(after["displays"][0]["entry_name"].is_null());
    assert!(!state.lock().unwrap().motion_active);
}

#[test]
fn a_playlist_that_plays_nothing_is_a_state_play_and_toggle_cant_leave() {
    // Review probe `rust_probe_stop_then_play.rs` (M2), reframed. The probe sent `stop` to a
    // service still holding the page's own document, then `play`: a service told only to
    // stop keeps a document that names the page, so that can only ever start it again,
    // and it still does. The fix changes the document instead: once the service holds the
    // one that plays nothing (what the app now publishes, and what its guard installs when
    // a publication fails), nothing a client sends brings the page back.
    let cycling = WEB_ONLY_OFF
        .replace("cycle_enabled = false", "cycle_enabled = true")
        .replace("cycle_interval_seconds = 300", "cycle_interval_seconds = 5");
    let (mut runtime, state, config_path, page) =
        web_runtime("nothing-holds", &stepped_down(WEB_ONLY_ON));
    fs::write(&config_path, &cycling).unwrap();
    assert!(runtime_command(&mut runtime, noon(), "reload", None).ok);
    let applies_before = state.lock().unwrap().applies.len();
    for (verb, argument) in [
        ("stop", None),
        ("play", None),
        ("stop", None),
        ("toggle", None),
        ("pause", None),
        ("toggle", None),
        ("play", None),
        ("schedule-follow", None),
        ("playlist-use", Some("All media")),
        ("shuffle", Some("on")),
    ] {
        let reply = runtime_command(&mut runtime, noon(), verb, argument);
        assert!(reply.ok, "{verb}: {}", reply.message);
        if verb == "play" {
            assert_eq!(reply.message, "nothing to play: web wallpapers are off");
        }
    }
    for verb in ["next", "previous", "random"] {
        let reply = runtime_command(&mut runtime, noon(), verb, None);
        assert!(!reply.ok, "{verb}");
        assert_eq!(reply.message, "nothing to play: web wallpapers are off");
    }
    // Its own rotation and the file poll's reload of the same bytes find nothing either.
    let started = Instant::now();
    for seconds in [6, 12, 18] {
        runtime.tick(noon(), started + Duration::from_secs(seconds));
    }
    assert!(runtime_command(&mut runtime, noon(), "reload", None).ok);
    let applies = state.lock().unwrap().applies.clone();
    assert!(
        applies[applies_before..].is_empty(),
        "nothing may be applied while nothing is to play: {:?}",
        &applies[applies_before..]
    );
    assert!(
        !applies[applies_before..]
            .iter()
            .any(|(entry, _)| entry == &page)
    );
    let after = status(&mut runtime, noon());
    assert_eq!(after["nothing_to_play"], "web wallpapers are off");
    assert_eq!(after["last_error"], "");
    assert_eq!(after["playback_state"], "playing");
    assert!(state.lock().unwrap().active_outputs.is_empty());
}

#[test]
fn a_service_that_starts_on_a_document_that_plays_nothing_runs_and_says_why() {
    let root = directory("starts-on-nothing");
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, WEB_ONLY_OFF).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime =
        Runtime::new(config_path, parsed, RuntimeDriver(state.clone()), noon()).unwrap();
    let applied = runtime.apply_current().unwrap();
    assert_eq!(applied, "nothing to play: web wallpapers are off");
    assert!(state.lock().unwrap().applies.is_empty());
    assert_eq!(
        status(&mut runtime, noon())["nothing_to_play"],
        "web wallpapers are off"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn independent_displays_that_play_nothing_say_so_and_status_still_answers() {
    let document = independent_config()
        .replace("schema_version = 4", "schema_version = 8")
        .replace(
            "[[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"day\"\n",
            "[[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"later\"\n",
        )
        + "[[playlists]]\nid = \"later\"\nname = \"Later\"\n\
           nothing_to_play = \"web wallpapers are off\"\nentries = []\n";
    let (mut runtime, state) = started_runtime(loaded(&document, &[]), noon());
    let snapshot = status(&mut runtime, noon());
    assert!(snapshot["nothing_to_play"].is_null(), "DP-1 still plays");
    let hdmi = display_status(&mut runtime, noon(), "HDMI-A-1");
    assert_eq!(hdmi["nothing_to_play"], "web wallpapers are off");
    assert_eq!(
        hdmi["entry_name"],
        "Nothing to play: web wallpapers are off"
    );
    assert_eq!(hdmi["entry_id"], "");
    assert_eq!(hdmi["last_error"], "");
    let dp = display_status(&mut runtime, noon(), "DP-1");
    assert!(dp["nothing_to_play"].is_null());
    assert_ne!(dp["entry_id"], "");
    assert!(!state.lock().unwrap().active_outputs.contains("HDMI-A-1"));

    let play = runtime_command(&mut runtime, noon(), "on", Some("HDMI-A-1 play"));
    assert!(play.ok, "{}", play.message);
    assert!(play.message.contains("nothing to play"), "{}", play.message);
    let next = runtime_command(&mut runtime, noon(), "on", Some("HDMI-A-1 next"));
    assert!(!next.ok);
    assert!(next.message.contains("nothing to play"), "{}", next.message);
    let moved = runtime_command(&mut runtime, noon(), "on", Some("DP-1 playlist-use later"));
    assert!(moved.ok, "{}", moved.message);
    let snapshot = status(&mut runtime, noon());
    assert_eq!(snapshot["nothing_to_play"], "web wallpapers are off");
    assert!(state.lock().unwrap().active_outputs.is_empty());
    let back = runtime_command(&mut runtime, noon(), "on", Some("DP-1 schedule-follow"));
    assert!(back.ok, "{}", back.message);
    assert!(display_status(&mut runtime, noon(), "DP-1")["nothing_to_play"].is_null());
}

#[test]
fn the_file_poll_loads_the_document_that_plays_nothing_and_play_leaves_it() {
    // The service's own reload when runtime.toml changes (main.rs), with the real driver.
    // The page is assigned to DP-1 so that no compositor needs asking.
    use std::os::unix::fs::PermissionsExt;
    let root = directory("file-poll-nothing");
    let config_path = root.join("runtime.toml");
    let socket = root.join("runtime.sock");
    let noctalia = root.join("noctalia");
    script(&noctalia, "exit 0");
    let pids = root.join("engine-pids");
    let engine = root.join("linux-wallpaperengine");
    fs::write(
        &engine,
        format!("#!/bin/sh\necho $$ >> {:?}\nexec sleep 30\n", pids),
    )
    .unwrap();
    fs::set_permissions(&engine, fs::Permissions::from_mode(0o755)).unwrap();
    let assigned = |document: &str| {
        document
            .replace(
                "noctalia_program = \"/bin/true\"",
                &format!("noctalia_program = {:?}", noctalia.display().to_string()),
            )
            .replace(
                "linux_wallpaperengine_program = \"/bin/true\"",
                &format!(
                    "linux_wallpaperengine_program = {:?}",
                    engine.display().to_string()
                ),
            )
            + "\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"all-media\"\n"
    };
    // As published for a service that isn't confirmed for 8; the page is a scene to the
    // driver's checks here (`scene_home`), so it starts.
    fs::write(&config_path, assigned(&stepped_down(WEB_ONLY_ON))).unwrap();
    let mut child = ServiceChild(
        service_command()
            .arg("--config")
            .arg(&config_path)
            .arg("--socket")
            .arg(&socket)
            .spawn()
            .unwrap(),
    );
    let started = |count: usize| {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let lines: Vec<String> = fs::read_to_string(&pids)
                .unwrap_or_default()
                .lines()
                .map(str::to_owned)
                .collect();
            if lines.len() >= count || Instant::now() >= deadline {
                return lines;
            }
            thread::sleep(Duration::from_millis(20));
        }
    };
    let [page] = started(1).try_into().unwrap();
    assert_eq!(request(&socket, "status", None)["ok"], true);

    // No reload is sent: the service notices the new file itself.
    fs::write(&config_path, assigned(WEB_ONLY_OFF)).unwrap();
    let gone = Path::new("/proc").join(&page);
    let deadline = Instant::now() + Duration::from_secs(10);
    while gone.exists() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    assert!(!gone.exists(), "the page's engine is still running");
    let snapshot: serde_json::Value = serde_json::from_str(
        request(&socket, "status", None)["message"]
            .as_str()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(snapshot["nothing_to_play"], "web wallpapers are off");
    for verb in ["play", "stop", "play"] {
        let reply = request(&socket, verb, None);
        assert_eq!(reply["ok"], true, "{verb}: {reply}");
    }
    thread::sleep(Duration::from_millis(1500));
    assert_eq!(started(2).len(), 1, "play started the engine again");
    stop(&mut child.0, &socket);
    fs::remove_dir_all(root).unwrap();
}

/// A home where the engine finds `id` with `project` as its project.json.
fn workshop_home_with(label: &str, items: &[(&str, &str)]) -> PathBuf {
    let home = directory(label);
    for (id, project) in items {
        let item = home
            .join(".local/share/Steam/steamapps/workshop/content/431960")
            .join(id);
        fs::create_dir_all(&item).unwrap();
        fs::write(item.join("project.json"), project).unwrap();
    }
    home
}

/// A real driver over a one-scene document, its engine an argument recorder.
fn p2_runtime(
    root: &Path,
    document: &str,
    home: &Path,
) -> (Runtime<SystemDriver>, PathBuf, PathBuf) {
    let noctalia = root.join("noctalia");
    script(&noctalia, "exit 0");
    let (engine, arguments) = argument_recorder(root, "linux-wallpaperengine");
    let document = document
        .replace(
            "noctalia_program = \"/bin/true\"",
            &format!("noctalia_program = {:?}", noctalia.display().to_string()),
        )
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!(
                "linux_wallpaperengine_program = {:?}",
                engine.display().to_string()
            ),
        );
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, &document).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let (driver, _) = driver_with(parsed.renderer.clone(), vec![live("DP-1", None)]);
    let driver = driver.with_workshop_home(Some(home.to_path_buf()));
    let runtime = Runtime::new(config_path.clone(), parsed, driver, noon()).unwrap();
    (runtime, arguments, config_path)
}

/// Whether the engine was started: its recorder recorded a launch whole, waited for up to
/// `RECORD_DEADLINE`.
fn engine_started(arguments: &Path) -> bool {
    recorded_lines(&launches_marked(arguments), 1).contains('\n')
}

#[test]
fn a_scene_whose_item_became_a_web_page_is_not_started_while_web_is_off() {
    // P2: a Workshop update turned the compiled scene into a web page. The document doesn't
    // allow web wallpapers (schema 4, as 0.4.0 wrote), so the engine is never started for it.
    let root = directory("p2-became-web");
    let home = workshop_home_with(
        "p2-became-web-home",
        &[(
            WEB_PAGE,
            r#"{"type":"web","title":"Electric Eye","file":"index.html"}"#,
        )],
    );
    let document = stepped_down(WITH_STILL_ON);
    let (mut runtime, arguments, _config) = p2_runtime(&root, &document, &home);
    let applied = runtime.apply_current();
    assert_eq!(
        applied.as_deref(),
        Ok("playing entry-2"),
        "the page is skipped to the still beside it"
    );
    thread::sleep(Duration::from_millis(300));
    assert!(
        !arguments.exists(),
        "linux-wallpaperengine was started for a web page"
    );
    let snapshot = status(&mut runtime, noon());
    assert_eq!(snapshot["entry_id"], "entry-2");
    assert_eq!(snapshot["last_error"], "", "a skip is not a fault");
    assert_eq!(snapshot["motion_active"], false);
    assert!(
        snapshot["taboo_entries"].as_array().unwrap().is_empty(),
        "never a taboo"
    );
    assert!(runtime_command(&mut runtime, noon(), "play", None).ok);
    thread::sleep(Duration::from_millis(300));
    assert!(!arguments.exists());
    // Remembered for this document: once it is a scene again, `next` still steps over
    // it until a reload asks again.
    let project = home.join(".local/share/Steam/steamapps/workshop/content/431960/2777242023");
    fs::write(
        project.join("project.json"),
        r#"{"type":"scene","file":"scene.json"}"#,
    )
    .unwrap();
    assert!(!runtime_command(&mut runtime, noon(), "next", None).ok);
    // The service's own account: still on the still, nothing in motion (a stand-in's file,
    // looked for at once, would prove nothing).
    let snapshot = status(&mut runtime, noon());
    assert_eq!(snapshot["entry_id"], "entry-2");
    assert_eq!(snapshot["motion_active"], false);
    assert!(runtime_command(&mut runtime, noon(), "reload", None).ok);
    assert!(runtime_command(&mut runtime, noon(), "next", None).ok);
    assert_eq!(status(&mut runtime, noon())["entry_id"], "entry-1");
    assert!(engine_started(&arguments), "a scene starts");
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn an_item_whose_type_cant_be_read_is_refused_while_web_is_off() {
    let root = directory("p2-unclassified");
    let home = workshop_home_with("p2-unclassified-home", &[]);
    let document = stepped_down(WITH_STILL_ON);
    let (mut runtime, arguments, _config) = p2_runtime(&root, &document, &home);
    assert_eq!(runtime.apply_current().as_deref(), Ok("playing entry-2"));
    thread::sleep(Duration::from_millis(300));
    assert!(!arguments.exists());
    let snapshot = status(&mut runtime, noon());
    assert_eq!(snapshot["entry_id"], "entry-2");
    assert_eq!(snapshot["last_error"], "");
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn a_document_that_allows_web_starts_the_page_and_turning_it_off_stops_it() {
    let root = directory("p2-allowed");
    let home = workshop_home_with(
        "p2-allowed-home",
        &[(
            WEB_PAGE,
            r#"{"type":"web","title":"Electric Eye","file":"index.html"}"#,
        )],
    );
    let (mut runtime, arguments, config_path) = p2_runtime(&root, WITH_STILL_ON, &home);
    let applied = without_text_file_busy(|| runtime.apply_current());
    assert!(applied.is_ok(), "{applied:?}");
    assert!(engine_started(&arguments), "allowed, the page plays");
    // The same document without the permission (the entries unchanged): every display
    // starts again, and the page is skipped this time.
    let refused = fs::read_to_string(&config_path)
        .unwrap()
        .replace("allow_web_wallpapers = true\n", "");
    fs::write(&config_path, refused).unwrap();
    fs::remove_file(&arguments).unwrap();
    let reply = runtime_command(&mut runtime, noon(), "reload", None);
    assert!(reply.ok, "{}", reply.message);
    // What the service did, not what a stand-in wrote meanwhile: the page was asked about and
    // refused (it would be running otherwise, its recorder sleeping), and the still plays. The
    // recorder's own file can't say: a stand-in writes in its own time, after an apply returns,
    // so its absence straight after the reload proves nothing (review r13f N16).
    let snapshot = status(&mut runtime, noon());
    assert_eq!(snapshot["motion_active"], false);
    assert_eq!(snapshot["entry_id"], "entry-2");
    assert_eq!(snapshot["last_error"], "");
    assert_eq!(
        snapshot["refused"],
        serde_json::json!([{
            "scene_id": WEB_PAGE,
            "web_page": true,
            "reason": snapshot["refused"][0]["reason"],
        }]),
        "{snapshot}"
    );
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn a_preset_of_a_web_page_is_refused_by_its_base_while_web_is_off() {
    let root = directory("p2-preset");
    let home = workshop_home_with(
        "p2-preset-home",
        &[
            (
                WEB_PAGE,
                r#"{"type":"web","title":"Electric Eye","file":"index.html"}"#,
            ),
            (
                "3000000001",
                r#"{"title":"Y2K","dependency":"2777242023","preset":{}}"#,
            ),
        ],
    );
    let document = WITH_STILL_ON
        .replace("schema_version = 8", "schema_version = 6")
        .replace("allow_web_wallpapers = true\n", "")
        .replace(
            "scene_id = \"2777242023\"",
            "scene_id = \"3000000001\"\nscene_base = \"2777242023\"",
        );
    let (mut runtime, arguments, _config) = p2_runtime(&root, &document, &home);
    assert_eq!(runtime.apply_current().as_deref(), Ok("playing entry-2"));
    thread::sleep(Duration::from_millis(300));
    assert!(!arguments.exists(), "a preset renders its base, a web page");
    assert_eq!(status(&mut runtime, noon())["entry_id"], "entry-2");
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn the_apps_captured_nothing_to_play_status_keeps_this_services_shape() {
    // fixtures/web-off/status-nothing-to-play.json (the app's tests read it too) was
    // captured from this service over the app's web-off document; its keys must stay these.
    // It is in this crate because the package builds from `service/` alone.
    let captured: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/web-off/status-nothing-to-play.json")).unwrap();
    let root = directory("captured-nothing");
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, WEB_ONLY_OFF).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState::default()));
    let mut runtime = Runtime::new(config_path, parsed, RuntimeDriver(state), noon()).unwrap();
    runtime.apply_current().unwrap();
    let live = status(&mut runtime, noon());
    let keys = |value: &serde_json::Value| {
        value
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>()
    };
    assert_eq!(keys(&live), keys(&captured));
    assert_eq!(keys(&live["displays"][0]), keys(&captured["displays"][0]));
    assert_eq!(
        live["allow_web_wallpapers"],
        captured["allow_web_wallpapers"]
    );
    for key in [
        "nothing_to_play",
        "entry_id",
        "kind",
        "still",
        "playlist_id",
    ] {
        assert_eq!(live[key], captured[key], "{key}");
        assert_eq!(
            live["displays"][0][key], captured["displays"][0][key],
            "row {key}"
        );
    }
    assert_eq!(
        live["displays"][0]["entry_name"],
        captured["displays"][0]["entry_name"]
    );
    fs::remove_dir_all(root).unwrap();
}

// -- r13 L1: an entry the service won't start is skipped ------------------------------------
//
// Owner: "skip to next, if it's in a playlist alone, then use the default playlist in
// schedule." While the document doesn't allow web wallpapers the driver refuses a web page,
// and an item it can't find or read, before the engine starts (P2). Such an entry is now
// skipped to the next of its playlist; a playlist refused throughout plays the default
// playlist, then All media; and with nothing left the display plays nothing and says why.
// Never a taboo. Every test here uses only what 27c3726 has, so each fails there by what it
// does, not by a missing name.

/// A scene the engine finds where it looks.
const SKIP_SCENE: &str = "12345";
/// A scene on a second Steam library: the app finds it through `libraryfolders.vdf`, the
/// engine looks only in Steam's own folders under `$HOME` and never finds it.
const OTHER_LIBRARY: &str = "2910000004";

/// A home where the engine finds `SKIP_SCENE` as a scene and `WEB_PAGE` typed `page`
/// (`web`, or `scene` for a control), and where `OTHER_LIBRARY` is on another Steam library,
/// or, for a control, in the engine's own folder.
fn skip_home(label: &str, page: &str, other_library_found: bool) -> PathBuf {
    let page = format!(r#"{{"type":"{page}","title":"Electric Eye","file":"index.html"}}"#);
    let scene = r#"{"type":"scene","file":"scene.json"}"#;
    let home = workshop_home_with(label, &[(SKIP_SCENE, scene), (WEB_PAGE, &page)]);
    let library = if other_library_found {
        ".local/share/Steam/steamapps/workshop/content/431960"
    } else {
        "Games/SteamLibrary/steamapps/workshop/content/431960"
    };
    let item = home.join(library).join(OTHER_LIBRARY);
    fs::create_dir_all(&item).unwrap();
    fs::write(item.join("project.json"), scene).unwrap();
    home
}

/// A schema-4 document, as 0.4.0 wrote and as the app steps down for a service not known to
/// read 8: web wallpapers aren't allowed. `default` is its default playlist; `playlists`
/// gives each playlist's entries, a Workshop id each or "" for a still. Entry `n` of playlist
/// `p` is `p-n` with the still `/stills/p-n.png`. `tail` follows (rules, displays).
fn skip_document(default: &str, playlists: &[(&str, &[&str])], tail: &str) -> String {
    let mut document = format!(
        r#"schema_version = 4
config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
default_playlist = "{default}"
[settings]
cycle_interval_seconds = 300
cycle_enabled = false
shuffle = false
dynamics_enabled = true
display_mode = "mirrored"
theme_source_connector = ""
[renderer]
noctalia_program = "/bin/true"
mpvpaper_program = "/bin/true"
linux_wallpaperengine_program = "/bin/true"
own_scene_renderer = true
layer = "background"
video_when_hidden = "pause"
video_hardware_decode = true
video_interpolation = "off"
video_muted = true
video_volume = 100
scene_fps = 30
scene_muted = true
scene_volume = 0
scene_pause_when_covered = true
scene_scaling = ""
scene_clamp = ""
"#
    );
    for (id, entries) in playlists {
        document += &format!("[[playlists]]\nid = \"{id}\"\nname = \"{id}\"\n");
        for (index, scene) in entries.iter().enumerate() {
            let entry = format!("{id}-{}", index + 1);
            document += &format!("[[playlists.entries]]\nid = \"{entry}\"\n");
            if scene.is_empty() {
                document += "kind = \"still\"\n";
            } else {
                document += &format!("kind = \"scene\"\nscene_id = \"{scene}\"\n");
            }
            document += &format!(
                "still = \"/stills/{entry}.png\"\npalette = {{ kind = \"keep\", mode = \"keep\" }}\n"
            );
        }
    }
    document + tail
}

/// A rule that puts the playlist `pages` in force from 11:00 to 13:00.
const NOON_RULE: &str =
    "[[schedules]]\nid = \"noon\"\nplaylist = \"pages\"\nstart = \"11:00\"\nend = \"13:00\"\n";

/// A real driver over `document` with `outputs` connected and `home` where the engine looks:
/// the runtime, the engine's argument log (written only if it is started), Noctalia's call
/// log and the document's path.
fn skip_runtime(
    root: &Path,
    document: &str,
    home: &Path,
    outputs: &[&str],
) -> (Runtime<SystemDriver>, PathBuf, PathBuf, PathBuf) {
    let log = root.join("noctalia.log");
    let noctalia = root.join("noctalia");
    script(&noctalia, &format!("printf '%s\\n' \"$*\" >> {log:?}"));
    let (engine, arguments) = argument_recorder(root, "linux-wallpaperengine");
    let document = document
        .replace(
            "noctalia_program = \"/bin/true\"",
            &format!("noctalia_program = {:?}", noctalia.display().to_string()),
        )
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!(
                "linux_wallpaperengine_program = {:?}",
                engine.display().to_string()
            ),
        );
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, &document).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let (driver, _) = driver_with(
        parsed.renderer.clone(),
        outputs.iter().map(|output| live(output, None)).collect(),
    );
    let driver = driver.with_workshop_home(Some(home.to_path_buf()));
    let runtime = Runtime::new(config_path.clone(), parsed, driver, noon()).unwrap();
    (runtime, arguments, log, config_path)
}

/// What Noctalia was told to show, one `wallpaper-set` call each, in order.
fn stills_handed(log: &Path) -> Vec<String> {
    fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| line.strip_prefix("msg wallpaper-set "))
        .map(str::to_owned)
        .collect()
}

/// A skip is not a fault, and nothing is saved against the item.
fn assert_skipped_quietly(snapshot: &serde_json::Value) {
    assert_eq!(snapshot["last_error"], "", "{snapshot}");
    assert_eq!(snapshot["renderer_failed"], false, "{snapshot}");
    assert!(
        snapshot["taboo_entries"].as_array().unwrap().is_empty(),
        "never a taboo: {snapshot}"
    );
}

fn remove_skip_fixture(mut runtime: Runtime<SystemDriver>, root: &Path, home: &Path) {
    runtime.shutdown();
    fs::remove_dir_all(root).unwrap();
    fs::remove_dir_all(home).unwrap();
}

#[test]
fn a_page_the_service_refuses_is_skipped_to_the_next_entry() {
    // Review probe `k/rust_probe_p2_skip.rs` (r13 L1): All media is the page, whose type
    // became web after the compile, and a still. Before, the display sat on the page's still
    // (`entry-1`) for good, and with cycle on it came back on every turn.
    let root = directory("r13-p2-skip");
    let home = workshop_home_with(
        "r13-p2-skip-home",
        &[(
            WEB_PAGE,
            r#"{"type":"web","title":"Electric Eye","file":"index.html"}"#,
        )],
    );
    let document = stepped_down(WITH_STILL_ON);
    let (mut runtime, arguments, log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    assert_eq!(runtime.apply_current().as_deref(), Ok("playing entry-2"));
    thread::sleep(Duration::from_millis(300));
    let snapshot = status(&mut runtime, noon());
    assert_eq!(snapshot["entry_id"], "entry-2");
    assert_eq!(snapshot["kind"], "still");
    assert!(snapshot["nothing_to_play"].is_null());
    assert_skipped_quietly(&snapshot);
    assert!(
        !arguments.exists(),
        "linux-wallpaperengine was started for a web page"
    );
    // The page's still never went up: the driver is asked before anything is shown.
    let handed = stills_handed(&log);
    assert!(
        !handed.iter().any(|still| still.contains(WEB_PAGE)),
        "{handed:?}"
    );
    assert_eq!(
        handed.last().map(String::as_str),
        Some("/home/user/Pictures/wallpapers/dawn.png")
    );
    remove_skip_fixture(runtime, &root, &home);
}

#[test]
fn control_a_page_that_is_still_a_scene_plays_where_it_is() {
    let root = directory("r13-p2-skip-control");
    let home = workshop_home_with(
        "r13-p2-skip-control-home",
        &[(WEB_PAGE, r#"{"type":"scene","file":"scene.json"}"#)],
    );
    let document = stepped_down(WITH_STILL_ON);
    let (mut runtime, arguments, log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    assert_eq!(runtime.apply_current().as_deref(), Ok("playing entry-1"));
    assert!(engine_started(&arguments));
    assert!(
        stills_handed(&log)
            .iter()
            .any(|still| still.contains(WEB_PAGE))
    );
    remove_skip_fixture(runtime, &root, &home);
}

/// All media: a still, a scene on another Steam library (or, for the control, where the
/// engine looks), and a still, rotating every 5 s; the entry shown after each of four turns.
fn rotation_over_another_library(found: bool) -> (Vec<String>, bool, Vec<String>) {
    let root = directory("other-library");
    let home = skip_home("other-library-home", "web", found);
    let document = skip_document("all-media", &[("all-media", &["", OTHER_LIBRARY, ""])], "")
        .replace("cycle_enabled = false", "cycle_enabled = true")
        .replace("cycle_interval_seconds = 300", "cycle_interval_seconds = 5");
    let (mut runtime, arguments, log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    assert_eq!(
        runtime.apply_current().as_deref(),
        Ok("playing all-media-1")
    );
    let started = Instant::now();
    let mut shown = Vec::new();
    let mut started_engine = false;
    for turn in 1..=4 {
        runtime.tick(noon(), started + Duration::from_secs(6 * turn));
        let snapshot = status(&mut runtime, noon());
        if !found {
            assert_skipped_quietly(&snapshot);
        }
        shown.push(snapshot["entry_id"].as_str().unwrap().to_string());
        // Asked before the next turn can stop it again.
        started_engine |= engine_ran(&arguments, found && turn == 1);
    }
    let handed = stills_handed(&log);
    remove_skip_fixture(runtime, &root, &home);
    (shown, started_engine, handed)
}

/// Whether the engine was started: waiting for it when it is expected to be, and a
/// second when it isn't, so that a late start is still seen.
fn engine_ran(arguments: &Path, expected: bool) -> bool {
    if expected {
        engine_started(arguments)
    } else {
        thread::sleep(Duration::from_secs(1));
        arguments.exists()
    }
}

#[test]
fn a_scene_on_another_steam_library_is_skipped_and_the_rotation_moves_on() {
    // At 0.4.0 the engine didn't find such a scene, so it failed, became a taboo and the
    // rotation moved on. r13's check refused it before the engine started: no taboo, but the
    // display held its still, and the rotation came back to it on every turn. Now it is
    // skipped, and asked once: the rotation never lands on it again.
    let (shown, started, handed) = rotation_over_another_library(false);
    assert_eq!(
        shown,
        ["all-media-3", "all-media-1", "all-media-3", "all-media-1"]
    );
    assert!(!started, "linux-wallpaperengine was started for it");
    assert!(
        !handed.iter().any(|still| still.contains("all-media-2")),
        "{handed:?}"
    );
}

#[test]
fn control_a_scene_where_the_engine_looks_takes_its_turn() {
    let (shown, started, _handed) = rotation_over_another_library(true);
    assert_eq!(
        shown,
        ["all-media-2", "all-media-3", "all-media-1", "all-media-2"]
    );
    assert!(started);
}

/// `next`, `previous`, `next` and `random` twice over All media: a still, the page (typed
/// `page`) and a still. The entry after each, and whether the engine started.
fn commands_over_the_page(page: &str) -> (Vec<String>, bool) {
    let root = directory("skip-commands");
    let home = skip_home("skip-commands-home", page, false);
    let document = skip_document("all-media", &[("all-media", &["", WEB_PAGE, ""])], "");
    let (mut runtime, arguments, _log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    runtime.apply_current().unwrap();
    let mut shown = Vec::new();
    let mut started = false;
    for verb in ["next", "previous", "next", "random", "random"] {
        let reply = runtime_command(&mut runtime, noon(), verb, None);
        assert!(reply.ok, "{verb}: {}", reply.message);
        let snapshot = status(&mut runtime, noon());
        if page == "web" {
            assert_skipped_quietly(&snapshot);
        }
        shown.push(snapshot["entry_id"].as_str().unwrap().to_string());
        // Asked before the next command can stop it again.
        started |= engine_ran(&arguments, page == "scene" && shown.len() == 1);
    }
    remove_skip_fixture(runtime, &root, &home);
    (shown, started)
}

#[test]
fn next_previous_and_random_step_over_a_refused_entry() {
    let (shown, started) = commands_over_the_page("web");
    assert_eq!(
        shown,
        [
            "all-media-3",
            "all-media-1",
            "all-media-3",
            "all-media-1",
            "all-media-3"
        ]
    );
    assert!(!started);
}

#[test]
fn control_next_lands_on_a_page_that_is_a_scene() {
    let (shown, started) = commands_over_the_page("scene");
    assert_eq!(shown[0], "all-media-2");
    assert_eq!(shown[1], "all-media-1");
    assert!(started);
}

/// The display's row and the engine's start for `playlists` under `default`, at noon (when
/// `NOON_RULE` puts `pages` in force) and then at 13:00, when it no longer is; and whether
/// Noctalia was told anything at 13:00.
fn schedule_fallback(
    default: &str,
    playlists: &[(&str, &[&str])],
    page: &str,
) -> (serde_json::Value, serde_json::Value, bool, bool) {
    let root = directory("skip-schedule");
    let home = skip_home("skip-schedule-home", page, false);
    let document = skip_document(default, playlists, NOON_RULE);
    let (mut runtime, arguments, log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    runtime.apply_current().unwrap();
    let at_noon = status(&mut runtime, noon());
    // Asked before 13:00 can stop it again.
    let started = engine_ran(&arguments, page == "scene");
    let calls = stills_handed(&log).len();
    let one = noon() + chrono::TimeDelta::hours(1);
    runtime.tick(one, Instant::now());
    let at_one = status(&mut runtime, one);
    let told_again = stills_handed(&log).len() != calls;
    remove_skip_fixture(runtime, &root, &home);
    (at_noon, at_one, started, told_again)
}

#[test]
fn a_rules_playlist_whose_only_entry_is_refused_plays_the_default() {
    let (at_noon, at_one, started, told_again) = schedule_fallback(
        "all-media",
        &[("all-media", &[WEB_PAGE, ""]), ("pages", &[WEB_PAGE])],
        "web",
    );
    assert_eq!(at_noon["schedule"]["rule_id"], "noon", "{at_noon}");
    let row = &at_noon["displays"][0];
    assert_eq!(row["playlist_id"], "all-media", "{row}");
    assert_eq!(row["entry_id"], "all-media-2", "{row}");
    assert_eq!(row["route_source"], "schedule");
    assert_eq!(row["schedule_rule_id"], "noon");
    assert!(row["nothing_to_play"].is_null());
    assert_skipped_quietly(&at_noon);
    // When the window ends the default is in force, which is what already plays.
    assert!(at_one["schedule"]["rule_id"].is_null(), "{at_one}");
    assert_eq!(at_one["displays"][0]["entry_id"], "all-media-2");
    assert!(!told_again, "nothing changes on screen at 13:00");
    assert!(!started);
}

#[test]
fn control_a_rules_playlist_that_can_play_plays() {
    let (at_noon, _at_one, started, _told_again) = schedule_fallback(
        "all-media",
        &[("all-media", &[WEB_PAGE, ""]), ("pages", &[WEB_PAGE])],
        "scene",
    );
    assert_eq!(at_noon["displays"][0]["playlist_id"], "pages");
    assert_eq!(at_noon["displays"][0]["entry_id"], "pages-1");
    assert!(started);
}

#[test]
fn a_default_refused_throughout_too_gives_way_to_all_media() {
    let (at_noon, at_one, started, _told_again) = schedule_fallback(
        "favourites",
        &[
            ("all-media", &[WEB_PAGE, ""]),
            ("favourites", &[WEB_PAGE]),
            ("pages", &[WEB_PAGE]),
        ],
        "web",
    );
    for snapshot in [&at_noon, &at_one] {
        let row = &snapshot["displays"][0];
        assert_eq!(row["playlist_id"], "all-media", "{row}");
        assert_eq!(row["entry_id"], "all-media-2", "{row}");
        assert_skipped_quietly(snapshot);
    }
    assert!(!started);
}

#[test]
fn a_default_that_can_play_stands_in_before_all_media() {
    let (at_noon, _at_one, _started, _told_again) = schedule_fallback(
        "favourites",
        &[
            ("all-media", &[WEB_PAGE, ""]),
            ("favourites", &[WEB_PAGE, ""]),
            ("pages", &[WEB_PAGE]),
        ],
        "web",
    );
    let row = &at_noon["displays"][0];
    assert_eq!(row["playlist_id"], "favourites", "{row}");
    assert_eq!(row["entry_id"], "favourites-2", "{row}");
}

#[test]
fn control_a_rules_playlist_that_can_play_plays_over_a_default_that_cant() {
    let (at_noon, _at_one, started, _told_again) = schedule_fallback(
        "favourites",
        &[
            ("all-media", &[WEB_PAGE, ""]),
            ("favourites", &[WEB_PAGE]),
            ("pages", &[WEB_PAGE]),
        ],
        "scene",
    );
    assert_eq!(at_noon["displays"][0]["entry_id"], "pages-1");
    assert!(started);
}

/// A runtime whose every playlist holds only `scene`, typed web (the page) or on another
/// library: its status after the first apply and that apply's reply, then the replies to
/// play, next and random, whether the engine started and what Noctalia was told.
fn nothing_but(
    scene: &str,
    extra_still: bool,
) -> (
    serde_json::Value,
    String,
    Vec<(bool, String)>,
    bool,
    Vec<String>,
) {
    let root = directory("skip-nothing");
    let home = skip_home("skip-nothing-home", "web", false);
    let all_media: &[&str] = if extra_still { &[scene, ""] } else { &[scene] };
    let document = skip_document(
        "all-media",
        &[("all-media", all_media), ("pages", &[scene])],
        NOON_RULE,
    );
    let (mut runtime, arguments, log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    let applied = runtime.apply_current().unwrap();
    let snapshot = status(&mut runtime, noon());
    let replies = ["play", "next", "random"]
        .into_iter()
        .map(|verb| {
            let reply = runtime_command(&mut runtime, noon(), verb, None);
            (reply.ok, reply.message)
        })
        .collect();
    let started = engine_ran(&arguments, scene == SKIP_SCENE);
    let handed = stills_handed(&log);
    remove_skip_fixture(runtime, &root, &home);
    (snapshot, applied, replies, started, handed)
}

#[test]
fn with_nothing_left_that_can_play_the_display_plays_nothing_and_says_why() {
    for (scene, reason) in [
        (WEB_PAGE, "this configuration doesn't allow web wallpapers"),
        (OTHER_LIBRARY, "no wallpaper here can start"),
    ] {
        let (snapshot, applied, replies, started, handed) = nothing_but(scene, false);
        assert_eq!(applied, format!("nothing to play: {reason}"));
        assert_eq!(snapshot["nothing_to_play"], reason, "{snapshot}");
        assert!(snapshot["entry_id"].is_null(), "{snapshot}");
        let row = &snapshot["displays"][0];
        assert_eq!(row["nothing_to_play"], reason);
        assert_eq!(row["entry_name"], format!("Nothing to play: {reason}"));
        assert_eq!(row["entry_id"], "");
        assert_eq!(row["kind"], "");
        assert_eq!(row["still"], "");
        assert_eq!(row["motion_active"], false);
        assert!(row["next_cycle_in_s"].is_null(), "{row}");
        assert_skipped_quietly(&snapshot);
        // The same state an empty playlist gives: play has nothing to start, and next and
        // random have nothing to move to.
        let said = format!("nothing to play: {reason}");
        assert_eq!(
            replies,
            [(true, said.clone()), (false, said.clone()), (false, said)]
        );
        assert!(!started);
        assert!(handed.is_empty(), "{handed:?}");
    }
}

#[test]
fn a_still_beside_them_in_all_media_is_what_plays() {
    let (snapshot, applied, _replies, started, _handed) = nothing_but(WEB_PAGE, true);
    assert_eq!(applied, "playing all-media-2");
    assert!(snapshot["nothing_to_play"].is_null());
    assert!(!started);
}

#[test]
fn control_a_scene_the_engine_finds_there_plays() {
    let (snapshot, applied, _replies, started, _handed) = nothing_but(SKIP_SCENE, false);
    assert_eq!(applied, "playing pages-1");
    assert!(snapshot["nothing_to_play"].is_null());
    assert!(started);
}

/// Independent displays: DP-1 on `mixed` (the page and a still), HDMI-A-1 on `pages` (the
/// page alone), the default All media (the page and a still).
fn independent_skips(page: &str) -> (serde_json::Value, serde_json::Value, bool) {
    let root = directory("skip-independent");
    let home = skip_home("skip-independent-home", page, false);
    let document = skip_document(
        "all-media",
        &[
            ("all-media", &[WEB_PAGE, ""]),
            ("mixed", &[WEB_PAGE, ""]),
            ("pages", &[WEB_PAGE]),
        ],
        "[[displays]]\nconnector = \"DP-1\"\nplaylist = \"mixed\"\n\
         [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"pages\"\n",
    )
    .replace(
        "display_mode = \"mirrored\"",
        "display_mode = \"independent\"",
    )
    .replace(
        "theme_source_connector = \"\"",
        "theme_source_connector = \"DP-1\"",
    );
    let (mut runtime, arguments, _log, _config) =
        skip_runtime(&root, &document, &home, &["DP-1", "HDMI-A-1"]);
    runtime.apply_current().unwrap();
    let dp = display_status(&mut runtime, noon(), "DP-1");
    let hdmi = display_status(&mut runtime, noon(), "HDMI-A-1");
    let started = engine_ran(&arguments, page == "scene");
    remove_skip_fixture(runtime, &root, &home);
    (dp, hdmi, started)
}

#[test]
fn independent_displays_skip_and_fall_back_each_on_its_own() {
    let (dp, hdmi, started) = independent_skips("web");
    assert_eq!(dp["playlist_id"], "mixed", "{dp}");
    assert_eq!(dp["entry_id"], "mixed-2", "{dp}");
    assert_eq!(dp["last_error"], "");
    assert_eq!(hdmi["assigned_playlist_id"], "pages", "{hdmi}");
    assert_eq!(hdmi["playlist_id"], "all-media", "{hdmi}");
    assert_eq!(hdmi["entry_id"], "all-media-2", "{hdmi}");
    assert_eq!(hdmi["route_source"], "assignment");
    assert_eq!(hdmi["last_error"], "");
    assert!(hdmi["nothing_to_play"].is_null());
    assert!(!started);
}

#[test]
fn control_independent_displays_play_their_own_playlists() {
    let (dp, hdmi, started) = independent_skips("scene");
    assert_eq!(dp["entry_id"], "mixed-1");
    assert_eq!(hdmi["playlist_id"], "pages");
    assert_eq!(hdmi["entry_id"], "pages-1");
    assert!(started);
}

/// A mirrored All media of a still, an entry the test driver refuses at the engine's start
/// only (as a type that changed after the driver was asked), and a still, rotating; the
/// entry after each of six turns, and how often the refused one was applied.
fn rotation_past_a_late_refusal(refuse: bool) -> (Vec<String>, usize, serde_json::Value) {
    let document = skip_document("all-media", &[("all-media", &["", SKIP_SCENE, ""])], "")
        .replace("cycle_enabled = false", "cycle_enabled = true")
        .replace("cycle_interval_seconds = 300", "cycle_interval_seconds = 5");
    let (mut runtime, state) = started_runtime(loaded(&document, &[]), noon());
    if refuse {
        state
            .lock()
            .unwrap()
            .refuse_entry_ids
            .insert("all-media-2".into());
    }
    let started = Instant::now();
    let mut shown = Vec::new();
    for turn in 1..=6 {
        runtime.tick(noon(), started + Duration::from_secs(6 * turn));
        shown.push(
            status(&mut runtime, noon())["entry_id"]
                .as_str()
                .unwrap()
                .to_string(),
        );
    }
    let applied = state
        .lock()
        .unwrap()
        .applies
        .iter()
        .filter(|(entry, _)| entry == "all-media-2")
        .count();
    (shown, applied, status(&mut runtime, noon()))
}

#[test]
fn an_entry_refused_only_at_the_start_is_skipped_and_asked_once() {
    let (shown, applied, snapshot) = rotation_past_a_late_refusal(true);
    assert_eq!(
        shown,
        [
            "all-media-3",
            "all-media-1",
            "all-media-3",
            "all-media-1",
            "all-media-3",
            "all-media-1"
        ]
    );
    assert_eq!(applied, 1, "tried once, never again this generation");
    assert_skipped_quietly(&snapshot);
}

#[test]
fn control_an_entry_that_starts_takes_every_third_turn() {
    let (shown, applied, _snapshot) = rotation_past_a_late_refusal(false);
    assert_eq!(
        shown,
        [
            "all-media-2",
            "all-media-3",
            "all-media-1",
            "all-media-2",
            "all-media-3",
            "all-media-1"
        ]
    );
    assert_eq!(applied, 2);
}

/// On battery, then on AC again, over All media: the page (typed `page`) and a still. The
/// entry on battery and on AC, and whether the engine started.
fn battery_then_ac(page: &str) -> (serde_json::Value, serde_json::Value, bool) {
    use wall_in_one_service::power::PowerSource::{Ac, Battery};
    let root = directory("skip-ac");
    let home = skip_home("skip-ac-home", page, false);
    let document = skip_document("all-media", &[("all-media", &[WEB_PAGE, ""])], "")
        .replace("schema_version = 4", "schema_version = 5")
        .replace(
            "dynamics_enabled = true",
            "dynamics_enabled = true\nstop_animations_on_battery = true",
        );
    let (mut runtime, arguments, _log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    runtime.observe_power(power_observation(Battery));
    runtime.apply_current().unwrap();
    let on_battery = status(&mut runtime, noon());
    runtime.observe_power(power_observation(Ac));
    let on_ac = status(&mut runtime, noon());
    let started = engine_ran(&arguments, page == "scene");
    remove_skip_fixture(runtime, &root, &home);
    (on_battery, on_ac, started)
}

#[test]
fn a_page_found_when_motion_resumes_on_ac_is_skipped() {
    // On battery nothing starts, so nothing is refused: the page's still shows, as any
    // scene's does. Motion resuming on AC asks the driver, and the page is skipped then.
    let (on_battery, on_ac, started) = battery_then_ac("web");
    assert_eq!(on_battery["entry_id"], "all-media-1");
    assert_eq!(on_ac["entry_id"], "all-media-2", "{on_ac}");
    assert_skipped_quietly(&on_ac);
    assert!(!started);
}

#[test]
fn control_a_scene_resumes_on_ac_where_it_is() {
    let (_on_battery, on_ac, started) = battery_then_ac("scene");
    assert_eq!(on_ac["entry_id"], "all-media-1");
    assert!(started);
}

#[test]
fn turning_web_wallpapers_on_forgets_what_was_refused() {
    // The skip memory is the document's: the reload that brings the permission asks again.
    let root = directory("skip-web-on");
    let home = skip_home("skip-web-on-home", "web", false);
    let (mut runtime, arguments, _log, config_path) =
        skip_runtime(&root, &stepped_down(WITH_STILL_ON), &home, &["DP-1"]);
    assert_eq!(runtime.apply_current().as_deref(), Ok("playing entry-2"));
    let allowed = fs::read_to_string(&config_path)
        .unwrap()
        .replace("schema_version = 4", "schema_version = 8")
        .replace(
            "theme_source_connector = \"\"\n",
            "theme_source_connector = \"\"\nallow_web_wallpapers = true\n",
        );
    fs::write(&config_path, allowed).unwrap();
    assert!(runtime_command(&mut runtime, noon(), "reload", None).ok);
    assert!(runtime_command(&mut runtime, noon(), "next", None).ok);
    assert_eq!(status(&mut runtime, noon())["entry_id"], "entry-1");
    assert!(engine_started(&arguments), "allowed, the page plays");
    remove_skip_fixture(runtime, &root, &home);
}

#[test]
fn status_says_whether_the_loaded_document_allows_web_wallpapers() {
    // The app's web-off guard reads it: only the document the service holds counts.
    let (mut runtime, _state, config_path, _page) = web_runtime("status-allows-web", WITH_STILL_ON);
    assert_eq!(status(&mut runtime, noon())["allow_web_wallpapers"], true);
    fs::write(&config_path, WITH_STILL_OFF).unwrap();
    assert!(runtime_command(&mut runtime, noon(), "reload", None).ok);
    assert_eq!(status(&mut runtime, noon())["allow_web_wallpapers"], false);
    // A refused reload keeps what was loaded, and says so.
    fs::write(&config_path, WITH_STILL_ON).unwrap();
    assert!(runtime_command(&mut runtime, noon(), "reload", None).ok);
    fs::write(&config_path, "schema_version = 99\n").unwrap();
    assert!(!runtime_command(&mut runtime, noon(), "reload", None).ok);
    assert_eq!(status(&mut runtime, noon())["allow_web_wallpapers"], true);
    // And in independent mode, the same top-level field.
    let (mut independent, _) = started_runtime(loaded(&independent_config(), &[]), noon());
    let snapshot = status(&mut independent, noon());
    assert_eq!(snapshot["allow_web_wallpapers"], false);
    assert_eq!(
        raw_status(&mut independent, noon())
            .matches("\"allow_web_wallpapers\":")
            .count(),
        1,
        "top level only"
    );
}

/// Independent displays under `NOON_RULE`: DP-1 assigned `mixed`, HDMI-A-1 assigned All
/// media, `pages` the page alone (typed `page`). Each row at noon, and at 13:00 when the
/// rule's window has ended, and what Noctalia was told for HDMI-A-1 at 13:00.
fn independent_window(page: &str) -> (Vec<serde_json::Value>, Vec<String>) {
    let root = directory("skip-independent-window");
    let home = skip_home("skip-independent-window-home", page, false);
    let document = skip_document(
        "all-media",
        &[
            ("all-media", &[WEB_PAGE, ""]),
            ("mixed", &["", ""]),
            ("pages", &[WEB_PAGE]),
        ],
        &format!(
            "{NOON_RULE}[[displays]]\nconnector = \"DP-1\"\nplaylist = \"mixed\"\n\
             [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"all-media\"\n"
        ),
    )
    .replace(
        "display_mode = \"mirrored\"",
        "display_mode = \"independent\"",
    )
    .replace(
        "theme_source_connector = \"\"",
        "theme_source_connector = \"DP-1\"",
    );
    let (mut runtime, _arguments, log, _config) =
        skip_runtime(&root, &document, &home, &["DP-1", "HDMI-A-1"]);
    runtime.apply_current().unwrap();
    let mut rows = vec![
        display_status(&mut runtime, noon(), "DP-1"),
        display_status(&mut runtime, noon(), "HDMI-A-1"),
    ];
    let calls = stills_handed(&log).len();
    let one = noon() + chrono::TimeDelta::hours(1);
    runtime.tick(one, Instant::now());
    rows.push(display_status(&mut runtime, one, "DP-1"));
    rows.push(display_status(&mut runtime, one, "HDMI-A-1"));
    let hdmi_at_one = stills_handed(&log)[calls..]
        .iter()
        .filter(|call| call.starts_with("HDMI-A-1 "))
        .cloned()
        .collect();
    remove_skip_fixture(runtime, &root, &home);
    (rows, hdmi_at_one)
}

#[test]
fn independent_displays_on_a_refused_rule_use_the_default_and_its_end_changes_only_what_differs() {
    let (rows, hdmi_at_one) = independent_window("web");
    for row in &rows[..2] {
        assert_eq!(row["playlist_id"], "all-media", "{row}");
        assert_eq!(row["entry_id"], "all-media-2", "{row}");
        assert_eq!(row["schedule_rule_id"], "noon", "{row}");
        assert_eq!(row["last_error"], "", "{row}");
    }
    // At 13:00 DP-1 goes back to its own playlist; HDMI-A-1's assignment is the stand-in
    // it already plays, so nothing is shown again there.
    assert_eq!(rows[2]["playlist_id"], "mixed", "{}", rows[2]);
    assert_eq!(rows[3]["playlist_id"], "all-media", "{}", rows[3]);
    assert_eq!(rows[3]["entry_id"], "all-media-2");
    assert!(hdmi_at_one.is_empty(), "{hdmi_at_one:?}");
}

#[test]
fn control_independent_displays_on_a_rule_that_can_play_change_back_at_its_end() {
    let (rows, hdmi_at_one) = independent_window("scene");
    assert_eq!(rows[1]["playlist_id"], "pages");
    assert_eq!(rows[3]["playlist_id"], "all-media");
    assert_eq!(rows[3]["entry_id"], "all-media-1");
    assert!(!hdmi_at_one.is_empty());
}

// -- r13c: what the person is told about a refused entry, and what the walk costs ------------
//
// Review r13b S1, S2, A1 and the service Nits. Each test here uses only what b3d1398 has
// (its test driver's `motion_refused` aside, whose reason is typed now), so each fails there
// by what the service does, not by a missing name.

/// `skip_runtime`, started at `at` instead of noon.
fn skip_runtime_at(
    root: &Path,
    document: &str,
    home: &Path,
    outputs: &[&str],
    at: chrono::NaiveDateTime,
) -> (Runtime<SystemDriver>, PathBuf, PathBuf) {
    let log = root.join("noctalia.log");
    let noctalia = root.join("noctalia");
    script(&noctalia, &format!("printf '%s\\n' \"$*\" >> {log:?}"));
    let (engine, arguments) = argument_recorder(root, "linux-wallpaperengine");
    let document = document
        .replace(
            "noctalia_program = \"/bin/true\"",
            &format!("noctalia_program = {:?}", noctalia.display().to_string()),
        )
        .replace(
            "linux_wallpaperengine_program = \"/bin/true\"",
            &format!(
                "linux_wallpaperengine_program = {:?}",
                engine.display().to_string()
            ),
        );
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, &document).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let (driver, _) = driver_with(
        parsed.renderer.clone(),
        outputs.iter().map(|output| live(output, None)).collect(),
    );
    let driver = driver.with_workshop_home(Some(home.to_path_buf()));
    let runtime = Runtime::new(config_path, parsed, driver, at).unwrap();
    (runtime, arguments, log)
}

/// Whether anything in `snapshot` outside its playlist and rule inventories names `id`.
fn names_outside_inventory(snapshot: &serde_json::Value, id: &str) -> bool {
    let mut copy = snapshot.clone();
    let object = copy.as_object_mut().unwrap();
    object.remove("playlists");
    object.remove("schedules");
    copy.to_string().contains(&format!("\"{id}\""))
}

/// The probe's pick (r13b S1): `playlist-use pages` over scenes = [SKIP_SCENE], pages =
/// [WEB_PAGE] typed `page`, all-media = both; the default is `scenes`. The status after it.
fn pick_of_pages(label: &str, page: &str) -> serde_json::Value {
    let root = directory(label);
    let home = skip_home(&format!("{label}-home"), page, false);
    let document = skip_document(
        "scenes",
        &[
            ("scenes", &[SKIP_SCENE]),
            ("pages", &[WEB_PAGE]),
            ("all-media", &[SKIP_SCENE, WEB_PAGE]),
        ],
        "",
    );
    let (mut runtime, _arguments, _log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    runtime.apply_current().unwrap();
    let reply = runtime_command(&mut runtime, noon(), "playlist-use", Some("pages"));
    assert!(reply.ok, "{}", reply.message);
    let snapshot = status(&mut runtime, noon());
    remove_skip_fixture(runtime, &root, &home);
    snapshot
}

#[test]
fn a_pick_the_service_refuses_throughout_is_named_beside_its_stand_in() {
    // r13b S1: the person picked `pages`, whose one entry the service refuses; the default
    // `scenes` stands in. At b3d1398 status said `source: manual`, `playlist_id: scenes`, and
    // nothing named `pages`, so the app said "Manual override · scenes is playing".
    let snapshot = pick_of_pages("r13c-pick", "web");
    assert_eq!(snapshot["source"], "manual");
    assert!(
        names_outside_inventory(&snapshot, "pages"),
        "status presents the stand-in as the pick: {snapshot}"
    );
    // What plays is still the stand-in, as `entry_id` is its entry.
    assert_eq!(snapshot["playlist_id"], "scenes");
    assert_eq!(snapshot["stand_in_for"], "pages");
    let row = &snapshot["displays"][0];
    assert_eq!(row["playlist_id"], "scenes", "{row}");
    assert_eq!(row["entry_id"], "scenes-1", "{row}");
    assert_eq!(row["stand_in_for"], "pages", "{row}");
    assert_eq!(row["manual_override"], true);
    assert!(row["nothing_to_play"].is_null());
}

#[test]
fn control_a_pick_that_plays_names_no_stand_in() {
    let snapshot = pick_of_pages("r13c-pick-control", "scene");
    assert_eq!(snapshot["playlist_id"], "pages");
    assert!(snapshot.get("stand_in_for").is_none(), "{snapshot}");
    assert!(
        snapshot["displays"][0].get("stand_in_for").is_none(),
        "absent, not null, while a display plays its own"
    );
}

/// Independent DP-1 and HDMI-A-1, both assigned `scenes`; DP-1 picks `pages` (typed `page`)
/// with `on`. DP-1's row.
fn independent_pick_of_pages(label: &str, page: &str) -> serde_json::Value {
    let root = directory(label);
    let home = skip_home(&format!("{label}-home"), page, false);
    let document = skip_document(
        "scenes",
        &[("scenes", &[SKIP_SCENE]), ("pages", &[WEB_PAGE])],
        "[[displays]]\nconnector = \"DP-1\"\nplaylist = \"scenes\"\n\
         [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"scenes\"\n",
    )
    .replace(
        "display_mode = \"mirrored\"",
        "display_mode = \"independent\"",
    )
    .replace(
        "theme_source_connector = \"\"",
        "theme_source_connector = \"DP-1\"",
    );
    let (mut runtime, _arguments, _log, _config) =
        skip_runtime(&root, &document, &home, &["DP-1", "HDMI-A-1"]);
    runtime.apply_current().unwrap();
    let reply = runtime_command(&mut runtime, noon(), "on", Some("DP-1 playlist-use pages"));
    assert!(reply.ok, "{}", reply.message);
    let snapshot = status(&mut runtime, noon());
    remove_skip_fixture(runtime, &root, &home);
    snapshot
}

#[test]
fn an_independent_displays_refused_pick_is_named_on_its_row() {
    let snapshot = independent_pick_of_pages("r13c-independent-pick", "web");
    assert!(
        names_outside_inventory(&snapshot, "pages"),
        "DP-1's pick is presented as its stand-in: {snapshot}"
    );
    let row = route(&snapshot, "DP-1");
    assert_eq!(row["route_source"], "manual", "{row}");
    assert_eq!(row["playlist_id"], "scenes", "{row}");
    assert_eq!(row["stand_in_for"], "pages", "{row}");
    assert!(route(&snapshot, "HDMI-A-1").get("stand_in_for").is_none());
    // Not every display plays that stand-in for it, so the summary names none.
    assert!(snapshot.get("stand_in_for").is_none(), "{snapshot}");
}

#[test]
fn control_an_independent_displays_pick_that_plays_is_its_own() {
    let snapshot = independent_pick_of_pages("r13c-independent-pick-control", "scene");
    let row = route(&snapshot, "DP-1");
    assert_eq!(row["playlist_id"], "pages", "{row}");
    assert!(row.get("stand_in_for").is_none(), "{row}");
}

#[test]
fn a_rules_and_an_assignments_refused_playlist_are_named_beside_the_stand_in() {
    // The rule's: mirrored, `pages` in force at noon, All media stands in.
    let (at_noon, _at_one, _started, _told_again) = schedule_fallback(
        "all-media",
        &[("all-media", &[WEB_PAGE, ""]), ("pages", &[WEB_PAGE])],
        "web",
    );
    assert_eq!(at_noon["displays"][0]["route_source"], "schedule");
    assert_eq!(at_noon["displays"][0]["stand_in_for"], "pages", "{at_noon}");
    assert_eq!(at_noon["stand_in_for"], "pages", "{at_noon}");
    // Independent, under the same rule: each row names the rule's playlist; at 13:00
    // HDMI-A-1's own assignment, All media, is what plays, so it names none.
    let (rows, _hdmi_at_one) = independent_window("web");
    assert_eq!(rows[0]["stand_in_for"], "pages", "{}", rows[0]);
    assert_eq!(rows[1]["stand_in_for"], "pages", "{}", rows[1]);
    assert_eq!(rows[3]["route_source"], "assignment", "{}", rows[3]);
    assert!(rows[3].get("stand_in_for").is_none(), "{}", rows[3]);
    // The assignment's: independent DP-1 is assigned `pages`.
    let root = directory("r13c-assigned");
    let home = skip_home("r13c-assigned-home", "web", false);
    let document = skip_document(
        "scenes",
        &[("scenes", &[SKIP_SCENE]), ("pages", &[WEB_PAGE])],
        "[[displays]]\nconnector = \"DP-1\"\nplaylist = \"pages\"\n",
    )
    .replace(
        "display_mode = \"mirrored\"",
        "display_mode = \"independent\"",
    )
    .replace(
        "theme_source_connector = \"\"",
        "theme_source_connector = \"DP-1\"",
    );
    let (mut runtime, _arguments, _log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    runtime.apply_current().unwrap();
    let snapshot = status(&mut runtime, noon());
    remove_skip_fixture(runtime, &root, &home);
    let row = route(&snapshot, "DP-1");
    assert_eq!(row["route_source"], "assignment", "{row}");
    assert_eq!(row["assigned_playlist_id"], "pages", "{row}");
    assert_eq!(row["playlist_id"], "scenes", "{row}");
    assert_eq!(row["stand_in_for"], "pages", "{row}");
    assert_eq!(
        snapshot["stand_in_for"], "pages",
        "every display plays it: {snapshot}"
    );
}

#[test]
fn with_nothing_to_stand_in_the_row_names_its_own_playlist_and_why() {
    // No stand-in: the display plays nothing, on the playlist asked for, and says why.
    let (snapshot, _applied, _replies, _started, _handed) = nothing_but(WEB_PAGE, false);
    let row = &snapshot["displays"][0];
    assert!(row.get("stand_in_for").is_none(), "{row}");
    assert!(row["nothing_to_play"].is_string(), "{row}");
}

/// The statuses the app's tests read (`tests/test_stand_in_status.py`): a manual pick of
/// `pages` that the service refuses throughout, and one that plays. Captured here, in this
/// crate, because the package builds from `service/` alone; blessed with
/// WALL_IN_ONE_BLESS_STAND_IN_STATUS.
#[test]
fn the_apps_captured_stand_in_statuses_keep_this_services_shape() {
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/stand-in");
    let keys = |value: &serde_json::Value| {
        value
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>()
    };
    for (name, page) in [("pick-refused", "web"), ("pick-plays", "scene")] {
        let live = pick_of_pages(&format!("r13c-captured-{name}"), page);
        let path = fixtures.join(format!("{name}.json"));
        if std::env::var_os("WALL_IN_ONE_BLESS_STAND_IN_STATUS").is_some() {
            // What differs per process or per run is masked.
            let mut blessed = live.clone();
            for key in ["runtime_instance", "runtime_executable"] {
                blessed[key] = "<masked>".into();
            }
            blessed["config_path"] = "/tmp/runtime.toml".into();
            fs::create_dir_all(&fixtures).unwrap();
            fs::write(
                &path,
                serde_json::to_string_pretty(&blessed).unwrap() + "\n",
            )
            .unwrap();
        }
        let captured: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(keys(&live), keys(&captured), "{name}");
        assert_eq!(
            keys(&live["displays"][0]),
            keys(&captured["displays"][0]),
            "{name}"
        );
        for key in [
            "playlist_id",
            "playlist",
            "source",
            "stand_in_for",
            "entry_id",
        ] {
            assert_eq!(live[key], captured[key], "{name}: {key}");
            assert_eq!(
                live["displays"][0][key], captured["displays"][0][key],
                "{name}: row {key}"
            );
        }
        assert_eq!(live["playlists"], captured["playlists"], "{name}");
    }
}

#[test]
fn a_skipped_scene_on_another_library_is_listed_as_refused() {
    // r13b S2: at 0.4.0 such a scene became a taboo, and status listed it; at b3d1398 it
    // was skipped with only a journal line. Now status lists it, and why, without a taboo.
    let root = directory("r13c-told");
    let home = skip_home("r13c-told-home", "web", false);
    let document = skip_document("all-media", &[("all-media", &["", OTHER_LIBRARY, ""])], "");
    let (mut runtime, _arguments, _log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, noon(), "next", None).ok);
    let snapshot = status(&mut runtime, noon());
    remove_skip_fixture(runtime, &root, &home);
    assert_eq!(snapshot["entry_id"], "all-media-3", "skipped, as designed");
    assert!(
        snapshot.to_string().contains(OTHER_LIBRARY),
        "status says nothing about the skipped item {OTHER_LIBRARY}: {snapshot}"
    );
    assert_skipped_quietly(&snapshot);
    let refused = snapshot["refused"].as_array().unwrap();
    assert_eq!(refused.len(), 1, "{snapshot}");
    assert_eq!(refused[0]["scene_id"], OTHER_LIBRARY);
    assert_eq!(refused[0]["web_page"], false);
    let reason = refused[0]["reason"].as_str().unwrap();
    assert!(
        reason.contains(OTHER_LIBRARY) && reason.contains("never another Steam library"),
        "{reason}"
    );
    assert_eq!(snapshot["refused_omitted"], 0);
}

#[test]
fn control_a_scene_where_the_engine_looks_is_not_listed() {
    let root = directory("r13c-told-control");
    let home = skip_home("r13c-told-control-home", "web", true);
    let document = skip_document("all-media", &[("all-media", &["", OTHER_LIBRARY, ""])], "");
    let (mut runtime, _arguments, _log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, noon(), "next", None).ok);
    let snapshot = status(&mut runtime, noon());
    remove_skip_fixture(runtime, &root, &home);
    assert_eq!(snapshot["entry_id"], "all-media-2");
    // It plays: nothing names it as refused (nor did anything at b3d1398).
    assert!(!snapshot.to_string().contains(OTHER_LIBRARY), "{snapshot}");
    assert_eq!(snapshot["refused"].as_array().map_or(0, Vec::len), 0);
}

#[test]
fn a_refused_web_page_is_listed_as_one_and_a_reload_that_allows_it_forgets_it() {
    let root = directory("r13c-told-web");
    let home = skip_home("r13c-told-web-home", "web", false);
    let (mut runtime, _arguments, _log, config_path) =
        skip_runtime(&root, &stepped_down(WITH_STILL_ON), &home, &["DP-1"]);
    runtime.apply_current().unwrap();
    let refused = status(&mut runtime, noon())["refused"].clone();
    assert_eq!(refused.as_array().unwrap().len(), 1, "{refused}");
    assert_eq!(refused[0]["scene_id"], WEB_PAGE);
    assert_eq!(refused[0]["web_page"], true);
    let allowed = fs::read_to_string(&config_path)
        .unwrap()
        .replace("schema_version = 4", "schema_version = 8")
        .replace(
            "theme_source_connector = \"\"\n",
            "theme_source_connector = \"\"\nallow_web_wallpapers = true\n",
        );
    fs::write(&config_path, allowed).unwrap();
    assert!(runtime_command(&mut runtime, noon(), "reload", None).ok);
    assert_eq!(
        status(&mut runtime, noon())["refused"],
        serde_json::json!([])
    );
    remove_skip_fixture(runtime, &root, &home);
}

#[test]
fn the_refused_list_is_capped_its_reasons_bounded_and_the_rest_counted() {
    // Forty items of a type the service doesn't play, each named by a type far longer than
    // any reason status carries; then a still. All forty are walked past at the first apply.
    let long_type = "x".repeat(4096);
    let ids: Vec<String> = (0..40)
        .map(|index| format!("{}", 3_100_000_000_u64 + index))
        .collect();
    let project = format!(r#"{{"type":"{long_type}","file":"scene.json"}}"#);
    let items: Vec<(&str, &str)> = ids
        .iter()
        .map(|id| (id.as_str(), project.as_str()))
        .collect();
    let home = workshop_home_with("r13c-cap-home", &items);
    let root = directory("r13c-cap");
    let mut entries: Vec<&str> = ids.iter().map(String::as_str).collect();
    entries.push("");
    let document = skip_document("all-media", &[("all-media", &entries)], "");
    let (mut runtime, _arguments, _log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    runtime.apply_current().unwrap();
    let snapshot = status(&mut runtime, noon());
    remove_skip_fixture(runtime, &root, &home);
    assert_eq!(snapshot["entry_id"], "all-media-41");
    let refused = snapshot["refused"].as_array().unwrap();
    assert_eq!(refused.len(), 16, "{snapshot}");
    assert_eq!(snapshot["refused_omitted"], 24);
    for (row, id) in refused.iter().zip(&ids) {
        assert_eq!(
            row["scene_id"],
            id.as_str(),
            "in the order the walk found them"
        );
        let reason = row["reason"].as_str().unwrap();
        assert!(reason.len() <= 256, "{}", reason.len());
        assert!(
            reason.contains(id.as_str()) && reason.contains("[truncated]"),
            "{reason}"
        );
    }
}

/// All media of `n` scenes the test driver says, when asked first, it won't start, then a
/// still, applied once: the time the apply took, how often each scene was asked, how often
/// the runtime worked out the settings motion starts with (once a display each round of the
/// walk), and the entry that plays.
fn walk_past(n: usize) -> (Duration, Vec<usize>, usize, String) {
    let ids: Vec<String> = (0..n)
        .map(|index| format!("{}", 3_000_000_000_u64 + index as u64))
        .collect();
    let mut entries: Vec<&str> = ids.iter().map(String::as_str).collect();
    entries.push("");
    let document = skip_document("all-media", &[("all-media", &entries)], "");
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        refuse_when_asked: (1..=n).map(|index| format!("all-media-{index}")).collect(),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        loaded(&document, &[]),
        RuntimeDriver(state.clone()),
        noon(),
    )
    .unwrap();
    let started = Instant::now();
    runtime.apply_current().unwrap();
    let took = started.elapsed();
    println!("walk past {n}: one apply took {took:?}");
    let entry = status(&mut runtime, noon())["entry_id"]
        .as_str()
        .unwrap()
        .to_string();
    let state = state.lock().unwrap();
    let asked = (1..=n)
        .map(|index| {
            state
                .asked
                .get(&format!("all-media-{index}"))
                .copied()
                .unwrap_or(0)
        })
        .collect();
    (took, asked, state.inhibition_sets, entry)
}

#[test]
fn a_walk_past_a_long_run_of_refused_entries_is_one_pass() {
    // r13b A1: each skip round learned one refusal and walked the playlist again, so a run
    // of n refused entries cost n rounds of O(n) work on the service's one thread (8000 in
    // 11 s, release). Now the walk asks ahead in one pass: each item is asked once, the
    // rounds don't grow with the run, and 6000 take well under the app's status timeout.
    let n = 6000;
    let (took, asked, rounds, entry) = walk_past(n);
    assert_eq!(
        entry,
        format!("all-media-{}", n + 1),
        "past the run, to the still"
    );
    assert!(
        asked.iter().all(|count| *count == 1),
        "each asked once a walk"
    );
    assert!(
        rounds <= 8,
        "the walk went round {rounds} times for {n} refused entries: it walks again for each"
    );
    assert!(
        took < Duration::from_secs(2),
        "one apply held the service thread {took:?}"
    );
}

#[test]
fn control_a_walk_past_a_short_run_is_as_short() {
    let (_took, asked, rounds, entry) = walk_past(2);
    assert_eq!(entry, "all-media-3");
    assert_eq!(asked, [1, 1]);
    assert!(rounds <= 8, "{rounds}");
}

#[test]
fn a_page_refused_only_at_the_engines_start_gives_the_web_reason() {
    // r13b N-a: a refusal learned at the engine's start was recorded as not a web page, so a
    // page refused that way made the reason "no wallpaper here can start".
    let document = skip_document("all-media", &[("all-media", &[SKIP_SCENE])], "");
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        refuse_entry_ids: HashSet::from(["all-media-1".to_string()]),
        refuse_as_web_page: true,
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        loaded(&document, &[]),
        RuntimeDriver(state),
        noon(),
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let snapshot = status(&mut runtime, noon());
    assert_eq!(
        snapshot["nothing_to_play"], "this configuration doesn't allow web wallpapers",
        "{snapshot}"
    );
}

#[test]
fn control_an_item_refused_only_at_the_start_that_isnt_a_page_cant_start() {
    let document = skip_document("all-media", &[("all-media", &[SKIP_SCENE])], "");
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        refuse_entry_ids: HashSet::from(["all-media-1".to_string()]),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        loaded(&document, &[]),
        RuntimeDriver(state),
        noon(),
    )
    .unwrap();
    runtime.apply_current().unwrap();
    assert_eq!(
        status(&mut runtime, noon())["nothing_to_play"],
        "no wallpaper here can start"
    );
}

/// Independent DP-1 on `pages` (the page alone, typed `page`), HDMI-A-1 on `other` (the
/// scene on another Steam library alone); the default is `pages`, and there is no All
/// media. Each row, at noon.
fn each_displays_own_reason(page: &str) -> (serde_json::Value, serde_json::Value) {
    let root = directory("r13c-own-reason");
    let home = skip_home("r13c-own-reason-home", page, false);
    let document = skip_document(
        "pages",
        &[("pages", &[WEB_PAGE]), ("other", &[OTHER_LIBRARY])],
        "[[displays]]\nconnector = \"DP-1\"\nplaylist = \"pages\"\n\
         [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"other\"\n",
    )
    .replace(
        "display_mode = \"mirrored\"",
        "display_mode = \"independent\"",
    )
    .replace(
        "theme_source_connector = \"\"",
        "theme_source_connector = \"DP-1\"",
    );
    let (mut runtime, _arguments, _log, _config) =
        skip_runtime(&root, &document, &home, &["DP-1", "HDMI-A-1"]);
    let _ = runtime.apply_current();
    let rows = (
        display_status(&mut runtime, noon(), "DP-1"),
        display_status(&mut runtime, noon(), "HDMI-A-1"),
    );
    remove_skip_fixture(runtime, &root, &home);
    rows
}

#[test]
fn each_display_that_plays_nothing_gives_its_own_reason() {
    // r13b N-b: the reason was worked out over every refusal of the document, so DP-1, whose
    // only items are web pages, said "no wallpaper here can start" because HDMI-A-1's item
    // is a scene the engine won't find.
    let (dp, hdmi) = each_displays_own_reason("web");
    assert_eq!(
        dp["nothing_to_play"], "this configuration doesn't allow web wallpapers",
        "{dp}"
    );
    assert_eq!(
        hdmi["nothing_to_play"], "no wallpaper here can start",
        "{hdmi}"
    );
}

#[test]
fn control_a_page_that_is_a_scene_plays_beside_a_display_that_cant() {
    let (dp, hdmi) = each_displays_own_reason("scene");
    assert!(dp["nothing_to_play"].is_null(), "{dp}");
    // HDMI-A-1's `other` gives way to the default, `pages`, which plays.
    assert_eq!(hdmi["playlist_id"], "pages", "{hdmi}");
}

/// Mirrored All media of three stills (the default) and `pages`, the page alone (typed
/// `page`). `pages` picked, `next`, then `pages` picked `picks` times: the entry that plays
/// after `next`, and at the end.
fn picks_of_pages(page: &str, picks: usize) -> (String, String) {
    let root = directory("r13c-repick");
    let home = skip_home("r13c-repick-home", page, false);
    let document = skip_document(
        "all-media",
        &[("all-media", &["", "", ""]), ("pages", &[WEB_PAGE])],
        "",
    );
    let (mut runtime, _arguments, _log, _config) = skip_runtime(&root, &document, &home, &["DP-1"]);
    runtime.apply_current().unwrap();
    assert!(runtime_command(&mut runtime, noon(), "playlist-use", Some("pages")).ok);
    // A one-entry `pages` that plays has no next: not asserted here.
    let _ = runtime_command(&mut runtime, noon(), "next", None);
    let entry = |runtime: &mut Runtime<SystemDriver>| {
        status(runtime, noon())["entry_id"]
            .as_str()
            .unwrap()
            .to_string()
    };
    let after_next = entry(&mut runtime);
    for _ in 0..picks {
        assert!(runtime_command(&mut runtime, noon(), "playlist-use", Some("pages")).ok);
    }
    let at_end = entry(&mut runtime);
    remove_skip_fixture(runtime, &root, &home);
    (after_next, at_end)
}

#[test]
fn picking_a_playlist_known_to_be_refused_keeps_the_stand_ins_rotation() {
    // r13b N-c: picking `pages` again, already known to be refused throughout, put the
    // stand-in's rotation back to its first entry.
    let (after_next, at_end) = picks_of_pages("web", 2);
    assert_eq!(after_next, "all-media-2", "the stand-in rotates");
    assert_eq!(
        at_end, "all-media-2",
        "and a pick of what it stands in for keeps it there"
    );
}

#[test]
fn control_picking_a_playlist_that_plays_starts_it_from_its_first_entry() {
    let (_after_next, at_end) = picks_of_pages("scene", 1);
    assert_eq!(at_end, "pages-1");
}

/// A rule puts `pages` (the scene on another Steam library alone: never asked before the
/// rule starts) in force from 11:00; All media of a still and SKIP_SCENE is the default,
/// in `mode`. Started at 10:00; the row at 11:00, and what Noctalia and the engine were
/// told when the rule started.
fn a_rule_starting_on_an_unasked_refused_playlist(
    mode: &str,
    other_library_found: bool,
) -> (serde_json::Value, usize, usize) {
    let root = directory("r13c-rule-start");
    let home = skip_home("r13c-rule-start-home", "web", other_library_found);
    let mut tail = String::from(
        "[[schedules]]\nid = \"noon\"\nplaylist = \"pages\"\nstart = \"11:00\"\nend = \"13:00\"\n",
    );
    if mode == "independent" {
        tail += "[[displays]]\nconnector = \"DP-1\"\nplaylist = \"all-media\"\n";
    }
    let document = skip_document(
        "all-media",
        &[
            ("all-media", &[SKIP_SCENE, ""]),
            ("pages", &[OTHER_LIBRARY]),
        ],
        &tail,
    )
    .replace(
        "display_mode = \"mirrored\"",
        &format!("display_mode = \"{mode}\""),
    )
    .replace(
        "theme_source_connector = \"\"",
        if mode == "independent" {
            "theme_source_connector = \"DP-1\""
        } else {
            "theme_source_connector = \"\""
        },
    );
    let ten = noon() - chrono::TimeDelta::hours(2);
    let (mut runtime, arguments, log) = skip_runtime_at(&root, &document, &home, &["DP-1"], ten);
    runtime.apply_current().unwrap();
    // The engine's first start, recorded whole: a start read as none here would read as a
    // start again below.
    let starts = recorded_arguments(&arguments, 1).matches("--bg").count();
    assert_eq!(starts, 1, "{mode}: the stand-in's engine never started");
    let calls = stills_handed(&log).len();
    let eleven = ten + chrono::TimeDelta::hours(1);
    runtime.tick(eleven, Instant::now());
    thread::sleep(Duration::from_millis(300));
    let row = status(&mut runtime, eleven)["displays"][0].clone();
    let told = stills_handed(&log).len() - calls;
    let restarted = fs::read_to_string(&arguments)
        .unwrap_or_default()
        .matches("--bg")
        .count()
        - starts;
    remove_skip_fixture(runtime, &root, &home);
    (row, told, restarted)
}

/// r13b N-d: the rule's playlist went through a transition, the refusal was learned by
/// applying it, and the stand-in (what already played) was applied again: its still handed
/// again and its engine started again.
fn the_stand_in_isnt_started_again_at_the_rules_start(mode: &str) {
    let (row, told, restarted) = a_rule_starting_on_an_unasked_refused_playlist(mode, false);
    assert_eq!(
        told, 0,
        "{mode}: Noctalia was told again at the rule's start"
    );
    assert_eq!(
        restarted, 0,
        "{mode}: the stand-in's engine was started again"
    );
    assert_eq!(row["schedule_rule_id"], "noon", "{mode}: {row}");
    assert_eq!(row["playlist_id"], "all-media", "{mode}: {row}");
    assert_eq!(row["entry_id"], "all-media-1", "{mode}: {row}");
    assert_eq!(row["stand_in_for"], "pages", "{mode}: {row}");
}

#[test]
fn a_rule_starting_on_a_refused_playlist_doesnt_start_the_mirrored_stand_in_again() {
    the_stand_in_isnt_started_again_at_the_rules_start("mirrored");
}

#[test]
fn a_rule_starting_on_a_refused_playlist_doesnt_start_an_independent_stand_in_again() {
    the_stand_in_isnt_started_again_at_the_rules_start("independent");
}

#[test]
fn control_a_rule_starting_on_a_playlist_that_plays_changes_the_display() {
    for mode in ["mirrored", "independent"] {
        let (row, told, _restarted) = a_rule_starting_on_an_unasked_refused_playlist(mode, true);
        assert_eq!(row["playlist_id"], "pages", "{mode}: {row}");
        assert_eq!(told, 1, "{mode}");
    }
}

#[test]
fn a_shuffled_walk_from_a_random_jump_past_refused_entries_is_one_pass_too() {
    // A shuffled cursor with nothing it can start after its entry deals the eligible ones
    // again, some never asked: the walk's next round asks along that new deal, so the deal
    // costs a round, not a round for each entry in it.
    let n = 200;
    let ids: Vec<String> = (0..n)
        .map(|index| format!("{}", 3_200_000_000_u64 + index as u64))
        .collect();
    let mut entries = vec![""];
    entries.extend(ids.iter().map(String::as_str));
    let document = skip_document("all-media", &[("all-media", &entries)], "")
        .replace("shuffle = false", "shuffle = true");
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        refuse_when_asked: (2..=n + 1)
            .map(|index| format!("all-media-{index}"))
            .collect(),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        loaded(&document, &[]),
        RuntimeDriver(state.clone()),
        noon(),
    )
    .unwrap();
    runtime.apply_current().unwrap();
    assert_eq!(status(&mut runtime, noon())["entry_id"], "all-media-1");
    state.lock().unwrap().inhibition_sets = 0;
    // Somewhere into the run of entries not asked yet.
    let reply = runtime_command(&mut runtime, noon(), "random", None);
    assert!(reply.ok, "{}", reply.message);
    assert_eq!(
        status(&mut runtime, noon())["entry_id"],
        "all-media-1",
        "the still"
    );
    let state = state.lock().unwrap();
    assert!(
        (2..=n + 1).all(|index| state
            .asked
            .get(&format!("all-media-{index}"))
            .copied()
            .unwrap_or(0)
            <= 1),
        "each asked at most once"
    );
    assert!(
        state.inhibition_sets <= 8,
        "the walk went round {} times",
        state.inhibition_sets
    );
}

// -- r13d B1: what status says of a refused entry is fitted to the reply, not budgeted --------
//
// Review r13c B1: 3433e14 charged `stand_in_for` and `refused` to the document's status budget,
// so a near-ceiling document 0.4.0 published, which b3d1398 loads, was refused by the unit's
// start check, and nothing played. The budget is 0.4.0's again, and the two are fitted when the
// reply is built: they give way before anything a 0.4.0 reply carries (`Runtime::status_reply`).

/// `status_ceiling_document`, mirrored and with motion on, beside a twin of BIG, ASK: its
/// `refused` scenes instead of BIG's still, and an id and a name exactly as long and as full of
/// quotes as BIG's. `taboo` of its 64 taboo playlists keep their record.
fn refusal_ceiling_document(refused: usize, taboo: usize) -> String {
    let quotes = |count: usize| "\\\"".repeat(count);
    let mut ask = format!(
        "[[playlists]]\nid = \"{}ASK\"\nname = \"{}Ask\"\n",
        quotes(210),
        quotes(117)
    );
    for index in 0..refused {
        ask.push_str(&format!(
            "[[playlists.entries]]\nid = \"{}{index:03}\"\nkind = \"scene\"\n\
             scene_id = \"{}\"\nstill = \"/tmp/s.png\"\n\
             palette = {{ kind = \"keep\", mode = \"keep\" }}\n",
            quotes(240),
            3_000_000_000_u64 + index as u64,
        ));
    }
    let mut document = status_ceiling_document()
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "display_mode = \"independent\"",
            "display_mode = \"mirrored\"",
        )
        .replacen(
            "[[playlists]]\nid = \"t00",
            &format!("{ask}[[playlists]]\nid = \"t00"),
            1,
        );
    for index in taboo..64 {
        let record = format!(
            "taboo = {{ reason = \"{}\", source = \"{}\" }}\n",
            quotes(512),
            quotes(64)
        );
        let at = document
            .find(&format!("[[playlists]]\nid = \"t{index:02}"))
            .unwrap();
        let start = at + document[at..].find(&record).unwrap();
        document.replace_range(start..start + record.len(), "");
    }
    document
}

/// The status of `refusal_ceiling_document(refused, taboo)` after a pick of BIG or ASK (`pick`),
/// with 64 published monitors and both renderers missing at the longest path; the driver
/// refuses every one of ASK's scenes. The snapshot, and its size on the wire.
fn refusal_ceiling_status(pick: &str, refused: usize, taboo: usize) -> (serde_json::Value, usize) {
    let monitors: Vec<Output> = (0..64)
        .map(|index| Output {
            name: format!("DP-{index}"),
            enabled: true,
            make: "Dell Inc.".into(),
            model: "DELL U2720Q".into(),
            width: 2560,
            height: 1440,
            scale: 1.5,
            mode_width: 3840,
            mode_height: 2160,
            refresh_millihz: Some(59_997),
            physical_width_mm: 597,
            physical_height_mm: 336,
            ..Output::default()
        })
        .collect();
    RUNTIME_DRIVER_PUBLISHED.with(|cell| cell.set(Box::leak(monitors.into_boxed_slice())));
    let at = noon();
    let parsed = Config::from_bytes(refusal_ceiling_document(refused, taboo).into_bytes()).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(
            (0..64)
                .map(|index| format!("{}L{index:02}", "\"".repeat(253)))
                .collect(),
        ),
        missing_renderers: longest_missing_renderers(),
        refuse_when_asked: (0..refused)
            .map(|index| format!("{}{index:03}", "\"".repeat(240)))
            .collect(),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        config_path_with_the_references_room(),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    with_the_references_executable(&mut runtime);
    runtime.apply_current().unwrap();
    runtime.tick(at, Instant::now());
    let picked = format!("{}{pick}", "\"".repeat(210));
    let reply = runtime_command(&mut runtime, at, "playlist-use", Some(&picked));
    assert!(reply.ok, "{}", reply.message);
    let response = runtime_command(&mut runtime, at, "status", None);
    RUNTIME_DRIVER_PUBLISHED.with(|cell| cell.set(&[]));
    assert!(response.ok, "{}", response.message);
    let mut encoded = Vec::new();
    write_response(&mut encoded, &response).unwrap();
    let snapshot: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    assert_eq!(snapshot["displays"].as_array().unwrap().len(), 128);
    assert_eq!(snapshot["playlists"].as_array().unwrap().len(), 66);
    (snapshot, encoded.len())
}

/// The rows, and the summary, that name the playlist a stand-in plays for.
fn stand_ins(snapshot: &serde_json::Value) -> usize {
    snapshot["displays"]
        .as_array()
        .unwrap()
        .iter()
        .chain(std::iter::once(snapshot))
        .filter(|row| row.get("stand_in_for").is_some())
        .count()
}

/// What a 0.4.0 reply carries that a status at the ceiling may cut.
const CUT_AT_THE_CEILING: [&str; 4] = [
    "taboo_entries",
    "taboo_entries_omitted",
    "outputs",
    "missing_renderers",
];

#[test]
fn refused_rows_and_stand_ins_never_displace_what_a_0_4_0_reply_carries() {
    // 20 of ASK's scenes are refused, so BIG stands in on all 128 rows and the summary. With 64
    // taboo records, a 0.4.0 reply already leaves out `outputs`, `missing_renderers`' paths and
    // two records (lean); with 32 it is whole. Either way the 0.4.1 fields get no room before
    // 0.4.0's: what was cut, and what wasn't, is exactly what the same status without them cuts
    // (a pick of BIG). The stand-ins, about 112 KB, don't fit; what room is left goes back to
    // `refused` rows (review r13e L6: at 040b3aa none, beside 109 KB to spare at 32 records).
    let mut checked = 0;
    for taboo in [64, 32] {
        let (plain, _) = refusal_ceiling_status("BIG", 20, taboo);
        let (shown, encoded) = refusal_ceiling_status("ASK", 20, taboo);
        assert!(encoded <= MAX_RESPONSE_BYTES, "{taboo}: {encoded} bytes");
        assert_eq!(stand_ins(&plain), 0);
        for row in shown["displays"].as_array().unwrap() {
            assert_eq!(row["playlist_id"], plain["playlist_id"], "{taboo}");
            assert_eq!(row["entry_id"], plain["displays"][0]["entry_id"], "{taboo}");
        }
        for key in CUT_AT_THE_CEILING {
            assert_eq!(
                shown[key], plain[key],
                "{taboo}: the 0.4.1 fields pushed {key} out"
            );
        }
        assert_eq!(stand_ins(&shown), 0, "{taboo}");
        assert_eq!(shown["stand_in_for_omitted"], true, "{taboo}");
        let listed = shown
            .get("refused")
            .map_or(0, |rows| rows.as_array().unwrap().len());
        assert_eq!(
            listed as u64 + shown["refused_omitted"].as_u64().unwrap(),
            20,
            "{taboo}: every refused item is listed or counted"
        );
        // As many rows as fit: one more (about 1.1 KB on the wire) would not. Some come back,
        // with this build's strings no more than 1 KB beyond the band's reference.
        let beyond = build_beyond_the_reference(&shown);
        if beyond <= 1_000 {
            checked += 1;
            assert!(listed >= 1, "{taboo}: no refused row given back");
        } else {
            eprintln!(
                "{taboo} records: NOT CHECKED that a refused row comes back: this build's \
                 strings take {beyond} bytes beyond the reference's"
            );
        }
        if listed < 16 {
            assert!(
                MAX_RESPONSE_BYTES - encoded < 1_200,
                "{taboo}: {encoded} bytes, {listed} rows"
            );
        }
    }
    assert_some_checked("a refused row given back", checked);
    let (at_64, _) = refusal_ceiling_status("BIG", 20, 64);
    // Two records out, with this build's strings no more than 1 KB beyond the reference's.
    assert_some_checked(
        "the ceiling's two records out",
        usize::from(build_beyond_the_reference(&at_64) <= 1_000),
    );
    assert_eq!(
        at_64["taboo_entries_omitted"], 2,
        "the ceiling cuts 0.4.0's fields"
    );
    assert!(
        at_64["taboo_entries_omitted"].as_u64().unwrap() > 0,
        "the ceiling cuts 0.4.0's fields"
    );
    assert_eq!(at_64["outputs"], serde_json::json!([]));
    let (at_32, _) = refusal_ceiling_status("BIG", 20, 32);
    assert_eq!(at_32["taboo_entries_omitted"], 0, "a whole 0.4.0 reply");
    assert_eq!(at_32["outputs"].as_array().unwrap().len(), 64);
}

#[test]
fn at_the_limit_refused_rows_go_before_the_stand_ins_and_each_is_counted() {
    // With 29 taboo records, everything 0.4.0 carries fits, and every `stand_in_for`, but not
    // 16 `refused` rows: the last listed go, and `refused_omitted` counts them. (With 28, since
    // a lean reply leaves out what says nothing, all 16 fit.)
    let (shown, encoded) = refusal_ceiling_status("ASK", 20, 29);
    assert!(encoded <= MAX_RESPONSE_BYTES, "{encoded} bytes");
    assert_eq!(shown["taboo_entries"].as_array().unwrap().len(), 29);
    assert_eq!(shown["taboo_entries_omitted"], 0);
    assert_eq!(shown["outputs"].as_array().unwrap().len(), 64);
    assert!(
        shown["missing_renderers"][0]["path"]
            .as_str()
            .unwrap()
            .contains("[truncated]"),
        "{}",
        shown["missing_renderers"]
    );
    let ask = format!("{}ASK", "\"".repeat(210));
    assert_eq!(stand_ins(&shown), 129);
    assert_eq!(shown["stand_in_for"], ask.as_str());
    for row in shown["displays"].as_array().unwrap() {
        assert_eq!(row["stand_in_for"], ask.as_str());
    }
    let listed = shown["refused"].as_array().unwrap();
    assert!(
        (1..16).contains(&listed.len()),
        "{} rows: not at the limit",
        listed.len()
    );
    // The first learned, in order.
    for (index, row) in listed.iter().enumerate() {
        assert_eq!(
            row["scene_id"],
            (3_000_000_000_u64 + index as u64).to_string()
        );
        assert!(row["reason"].as_str().unwrap().len() <= 256);
    }
    assert_eq!(
        listed.len() as u64 + shown["refused_omitted"].as_u64().unwrap(),
        20
    );
    // Only as many as needed went: one more row (about 1.1 KB on the wire) would not fit.
    assert!(
        MAX_RESPONSE_BYTES - encoded < 1_200,
        "left out more than needed: {encoded} bytes"
    );
}

// -- r13d N-8: an independent row's stand-in is named as of the decision it shows ------------

/// `independent_window`'s document and runtime, started at `at`: independent DP-1 assigned
/// `mixed`, HDMI-A-1 assigned All media, and `NOON_RULE` putting `pages`, the page alone
/// (refused), in force from 11:00 to 13:00 for both.
fn rule_boundary_runtime(
    label: &str,
    at: chrono::NaiveDateTime,
) -> (Runtime<SystemDriver>, PathBuf, PathBuf) {
    let root = directory(label);
    let home = skip_home(&format!("{label}-home"), "web", false);
    let document = skip_document(
        "all-media",
        &[
            ("all-media", &[WEB_PAGE, ""]),
            ("mixed", &["", ""]),
            ("pages", &[WEB_PAGE]),
        ],
        &format!(
            "{NOON_RULE}[[displays]]\nconnector = \"DP-1\"\nplaylist = \"mixed\"\n\
             [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"all-media\"\n"
        ),
    )
    .replace(
        "display_mode = \"mirrored\"",
        "display_mode = \"independent\"",
    )
    .replace(
        "theme_source_connector = \"\"",
        "theme_source_connector = \"DP-1\"",
    );
    let (mut runtime, _arguments, _log) =
        skip_runtime_at(&root, &document, &home, &["DP-1", "HDMI-A-1"], at);
    runtime.apply_current().unwrap();
    (runtime, root, home)
}

#[test]
fn an_independent_rows_stand_in_is_named_as_of_the_decision_it_shows() {
    // Review r13c N-8: a row's `stand_in_for` asked the schedule again at the status's own
    // moment, while `playlist_id`, `route_source` and `schedule_rule_id` are the route's last
    // decision. Between a rule's boundary and the tick that applies it the row said two things.
    let one = noon() + chrono::TimeDelta::hours(1);
    let (mut runtime, root, home) = rule_boundary_runtime("r13d-rule-end", noon());
    // 13:00, the rule's end, not applied yet: both still play All media for `pages`.
    let before = status(&mut runtime, one);
    runtime.tick(one, Instant::now());
    let after = status(&mut runtime, one);
    remove_skip_fixture(runtime, &root, &home);
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = route(&before, connector);
        assert_eq!(row["schedule_rule_id"], "noon", "{row}");
        assert_eq!(row["playlist_id"], "all-media", "{row}");
        assert_eq!(row["stand_in_for"], "pages", "{row}");
    }
    assert_eq!(before["stand_in_for"], "pages", "{before}");
    // Applied: DP-1 plays its own playlist, and HDMI-A-1's own is All media.
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = route(&after, connector);
        assert_eq!(row["route_source"], "assignment", "{row}");
        assert!(row.get("stand_in_for").is_none(), "{row}");
    }

    // 11:00, the rule's start, not applied yet: HDMI-A-1 still plays its own assignment.
    let eleven = noon() - chrono::TimeDelta::hours(1);
    let (mut runtime, root, home) =
        rule_boundary_runtime("r13d-rule-start", eleven - chrono::TimeDelta::seconds(1));
    let before = status(&mut runtime, eleven);
    remove_skip_fixture(runtime, &root, &home);
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = route(&before, connector);
        assert_eq!(row["route_source"], "assignment", "{row}");
        assert!(row.get("stand_in_for").is_none(), "{row}");
    }
    assert_eq!(route(&before, "HDMI-A-1")["playlist_id"], "all-media");
}

// -- r13d N3: a mirrored row's provenance is the decision it shows ---------------------------

/// `rule_boundary_runtime`'s document left mirrored, started at `at`: DP-1 assigned `mixed`,
/// HDMI-A-1 assigned All media, and `rules` (`NOON_RULE` puts `pages`, refused, in force).
fn mirrored_rule_runtime(
    label: &str,
    rules: &str,
    at: chrono::NaiveDateTime,
) -> (Runtime<SystemDriver>, PathBuf, PathBuf, PathBuf) {
    let root = directory(label);
    let home = skip_home(&format!("{label}-home"), "web", false);
    let document = skip_document(
        "all-media",
        &[
            ("all-media", &[WEB_PAGE, ""]),
            ("mixed", &["", ""]),
            ("pages", &[WEB_PAGE]),
        ],
        &format!(
            "{rules}[[displays]]\nconnector = \"DP-1\"\nplaylist = \"mixed\"\n\
             [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"all-media\"\n"
        ),
    );
    let (mut runtime, _arguments, log) =
        skip_runtime_at(&root, &document, &home, &["DP-1", "HDMI-A-1"], at);
    runtime.apply_current().unwrap();
    (runtime, root, home, log)
}

#[test]
fn a_mirrored_rows_provenance_is_the_decision_it_shows() {
    // Review r13d N3: a mirrored row's `route_source` and `schedule_rule_id` asked the schedule
    // again at the status's own moment, while its `playlist_id` and `stand_in_for` are the last
    // decision. Between a rule's boundary and the tick that applies it the row said two things.
    let one = noon() + chrono::TimeDelta::hours(1);
    let (mut runtime, root, home, _log) = mirrored_rule_runtime("r13e-n3-end", NOON_RULE, noon());
    let at_noon = status(&mut runtime, noon());
    // 13:00, the rule's end, not applied yet: both still play All media for `pages`.
    let before = status(&mut runtime, one);
    runtime.tick(one, Instant::now());
    let after = status(&mut runtime, one);
    remove_skip_fixture(runtime, &root, &home);
    for connector in ["DP-1", "HDMI-A-1"] {
        // The control: at noon both say the rule, and the stand-in for its playlist.
        let row = route(&at_noon, connector);
        assert_eq!(row["route_source"], "schedule", "{row}");
        assert_eq!(row["schedule_rule_id"], "noon", "{row}");
        assert_eq!(row["stand_in_for"], "pages", "{row}");
        let row = route(&before, connector);
        assert_eq!(row["playlist_id"], "all-media", "{row}");
        assert_eq!(row["stand_in_for"], "pages", "{row}");
        assert_eq!(row["route_source"], "schedule", "{connector}: {row}");
        assert_eq!(row["schedule_rule_id"], "noon", "{connector}: {row}");
        // Applied: each plays its own assignment, which no rule chose.
        let row = route(&after, connector);
        assert_eq!(row["route_source"], "assignment", "{row}");
        assert!(row["schedule_rule_id"].is_null(), "{row}");
        assert!(row.get("stand_in_for").is_none(), "{row}");
    }
    assert_eq!(route(&after, "DP-1")["playlist_id"], "mixed");
    // What the schedule says now is still asked at the status's moment.
    assert!(before["schedule"]["rule_id"].is_null(), "{before}");

    // 11:00, the rule's start, not applied yet: each still plays its own assignment.
    let eleven = noon() - chrono::TimeDelta::hours(1);
    let (mut runtime, root, home, _log) = mirrored_rule_runtime(
        "r13e-n3-start",
        NOON_RULE,
        eleven - chrono::TimeDelta::seconds(1),
    );
    let before = status(&mut runtime, eleven);
    remove_skip_fixture(runtime, &root, &home);
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = route(&before, connector);
        assert_eq!(row["route_source"], "assignment", "{connector}: {row}");
        assert!(row["schedule_rule_id"].is_null(), "{connector}: {row}");
    }
    assert_eq!(route(&before, "DP-1")["playlist_id"], "mixed");
    assert_eq!(before["schedule"]["rule_id"], "noon", "{before}");
}

#[test]
fn a_mirrored_rule_handing_over_to_another_of_the_same_playlist_is_named_at_the_tick() {
    // N3's guard: provenance is stored now, so a hand-over that changes nothing on screen (a
    // rule ending onto another of the same playlist) must still change the rule rows name,
    // and without telling Noctalia again.
    let rules = "[[schedules]]\nid = \"first\"\nplaylist = \"mixed\"\nstart = \"11:00\"\nend = \"12:00\"\n\
                 [[schedules]]\nid = \"second\"\nplaylist = \"mixed\"\nstart = \"12:00\"\nend = \"13:00\"\n";
    let half_past = noon() - chrono::TimeDelta::minutes(30);
    let (mut runtime, root, home, log) =
        mirrored_rule_runtime("r13e-n3-handover", rules, half_past);
    let first = status(&mut runtime, half_past);
    let told = fs::read_to_string(&log).unwrap().lines().count();
    runtime.tick(noon(), Instant::now());
    let second = status(&mut runtime, noon());
    let told_again = fs::read_to_string(&log).unwrap().lines().count() != told;
    remove_skip_fixture(runtime, &root, &home);
    for connector in ["DP-1", "HDMI-A-1"] {
        assert_eq!(route(&first, connector)["schedule_rule_id"], "first");
        let row = route(&second, connector);
        assert_eq!(row["route_source"], "schedule", "{row}");
        assert_eq!(row["schedule_rule_id"], "second", "{row}");
        assert_eq!(row["playlist_id"], "mixed", "{row}");
    }
    assert!(!told_again, "nothing changes on screen at noon");
}

#[test]
fn a_picks_rows_name_no_rule_in_either_mode() {
    // A pick is no rule's decision. An independent route says so (`schedule_rule_id` null);
    // a mirrored row named the rule the schedule had in force, which isn't what plays.
    for mode in ["mirrored", "independent"] {
        let label = format!("r13e-n3-pick-{mode}");
        let (mut runtime, root, home, _log) = if mode == "mirrored" {
            mirrored_rule_runtime(&label, NOON_RULE, noon())
        } else {
            let (runtime, root, home) = rule_boundary_runtime(&label, noon());
            (runtime, root, home, PathBuf::new())
        };
        let reply = runtime_command(&mut runtime, noon(), "playlist-use", Some("mixed"));
        assert!(reply.ok, "{mode}: {}", reply.message);
        let picked = status(&mut runtime, noon());
        remove_skip_fixture(runtime, &root, &home);
        for connector in ["DP-1", "HDMI-A-1"] {
            let row = route(&picked, connector);
            assert_eq!(row["route_source"], "manual", "{mode}: {row}");
            assert_eq!(row["playlist_id"], "mixed", "{mode}: {row}");
            assert!(row["schedule_rule_id"].is_null(), "{mode}: {row}");
        }
        // What following the schedule again would give is still said.
        assert_eq!(picked["schedule"]["rule_id"], "noon", "{mode}: {picked}");
    }
}

// -- r13d L5: every document the budget passes gets a status reply ----------------------------
//
// The budget counts a row's connector, its two playlists and its entry, but not the
// `schedule_rule_id` every row carries: a rule's id of 256 quotes is about 1 KB on the wire, on
// each of 128 rows. A document at the budget's edge then had no reply small enough to send, not
// even with every taboo record, `outputs` and `missing_renderers` gone, and every status query
// got EOF. The reply now leaves the rows' rule ids null, then every diagnostic empty, and says
// so (`Runtime::abridged`).

/// The review's corner: 578 playlists and 512 rules (511 small, never in force, months and
/// weekdays full; the last always in force, its id `rule_id`, TOML-escaped), 64 detached
/// assignments with 256-byte connectors of quotes, and BIG (id, name, entry and the still `/`
/// and `still` quotes) full of quotes, played on every row.
fn floor_corner(mode: &str, rule_id: &str, still: usize) -> String {
    let quotes = |count: usize| "\\\"".repeat(count);
    let big = format!("{}B", quotes(255));
    // Independent mode needs a theme source: the first assignment's connector.
    let theme = if mode == "independent" {
        format!("{}D00", quotes(253))
    } else {
        String::new()
    };
    let mut document = floor_corner_head(mode, &big, &theme)
        + &format!(
            "[[playlists]]\nid = \"{big}\"\nname = \"{}N\"\n[[playlists.entries]]\nid = \"{}E\"\n\
             kind = \"still\"\nstill = \"/{}\"\npalette = {{ kind = \"keep\", mode = \"keep\" }}\n",
            quotes(119),
            quotes(255),
            quotes(still),
        );
    for index in 0..577 {
        document.push_str(&format!(
            "[[playlists]]\nid = \"{index:03}\"\nname = \"n{index:03}\"\n\
             [[playlists.entries]]\nid = \"e\"\nkind = \"still\"\nstill = \"/s.png\"\n\
             palette = {{ kind = \"keep\", mode = \"keep\" }}\n"
        ));
    }
    for index in 0..511 {
        document.push_str(&format!(
            "[[schedules]]\nid = \"r{index:03}\"\nplaylist = \"{:03}\"\n\
             months = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]\nweekdays = [0, 1, 2, 3, 4, 5, 6]\n\
             start = \"00:00\"\nend = \"00:01\"\n",
            index % 577
        ));
    }
    document.push_str(&format!(
        "[[schedules]]\nid = \"{rule_id}\"\nplaylist = \"{big}\"\n"
    ));
    for index in 0..64 {
        document.push_str(&format!(
            "[[displays]]\nconnector = \"{}D{index:02}\"\nplaylist = \"{big}\"\n",
            quotes(253),
        ));
    }
    document
}

fn floor_corner_head(mode: &str, default: &str, theme: &str) -> String {
    format!(
        r#"schema_version = 4
config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
default_playlist = "{default}"
[settings]
cycle_interval_seconds = 300
cycle_enabled = false
shuffle = false
dynamics_enabled = false
display_mode = "{mode}"
theme_source_connector = "{theme}"
[renderer]
noctalia_program = "/bin/true"
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
"#
    )
}

/// `count` characters, each `"` or `\`: `index` in binary, after `lead`. TOML-escaped. A
/// string of them is four times its length on the wire, as the budget counts every string.
fn quoted_text(lead: &str, index: usize, count: usize) -> String {
    let mut text = lead.to_string();
    for bit in (0..count).rev() {
        text.push(if index >> bit & 1 == 1 { '\\' } else { '"' });
    }
    text.replace('\\', "\\\\").replace('"', "\\\"")
}

/// The same ceilings, with every string the budget counts made of quotes and backslashes,
/// so that none takes less on the wire than the budget counts (the review's corner has short
/// plain ids, which do): what is left over is the budget's reserve less the structure. Rules
/// have no time window (one counts 10 bytes four times, on the wire once) and the small ones
/// are switched off; the one in force has `rule_chars` backslashes for an id.
fn quoted_structure_corner(mode: &str, rule_chars: usize, still: usize) -> String {
    let quotes = |count: usize| "\\\"".repeat(count);
    let big = quotes(64);
    let theme = if mode == "independent" {
        format!("{}{}", quotes(250), quoted_text("", 0, 6))
    } else {
        String::new()
    };
    let months =
        "months = [10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9]\nweekdays = [0, 1, 2, 3, 4, 5, 6]\n";
    let mut document = floor_corner_head(mode, &big, &theme)
        + &format!(
            "[[playlists]]\nid = \"{big}\"\nname = \"{}\"\n[[playlists.entries]]\nid = \"{}\"\n\
             kind = \"still\"\nstill = \"/{}\"\npalette = {{ kind = \"keep\", mode = \"keep\" }}\n",
            "\\\\".repeat(32),
            quotes(64),
            quotes(still),
        );
    for index in 0..577 {
        document.push_str(&format!(
            "[[playlists]]\nid = \"{}\"\nname = \"{}\"\n\
             [[playlists.entries]]\nid = \"e\"\nkind = \"still\"\nstill = \"/s.png\"\n\
             palette = {{ kind = \"keep\", mode = \"keep\" }}\n",
            quoted_text("\"", index, 10),
            quoted_text("\\", index, 10),
        ));
    }
    for index in 0..511 {
        document.push_str(&format!(
            "[[schedules]]\nid = \"{}\"\nplaylist = \"{}\"\nenabled = false\n{months}",
            quoted_text("\"", index, 10),
            quoted_text("\"", index % 577, 10),
        ));
    }
    document.push_str(&format!(
        "[[schedules]]\nid = \"{}\"\nplaylist = \"{big}\"\n{months}",
        "\\\\".repeat(rule_chars)
    ));
    for index in 0..64 {
        document.push_str(&format!(
            "[[displays]]\nconnector = \"{}{}\"\nplaylist = \"{big}\"\n",
            quotes(250),
            quoted_text("", index, 6)
        ));
    }
    document
}

/// `corner(still)` with the longest still the budget passes: a document at its very edge.
fn at_the_budgets_edge(corner: impl Fn(usize) -> String) -> String {
    let (mut fits, mut over) = (0_usize, 4000_usize);
    if let Err(error) = Config::from_bytes(corner(0).into_bytes()) {
        panic!("the corner itself is refused: {error}");
    }
    assert!(Config::from_bytes(corner(over).into_bytes()).is_err());
    while over - fits > 1 {
        let middle = (fits + over) / 2;
        if Config::from_bytes(corner(middle).into_bytes()).is_ok() {
            fits = middle;
        } else {
            over = middle;
        }
    }
    corner(fits)
}

/// The status of `document` with `outputs` live outputs of 256 quotes (each a row beside the 64
/// assignments) and nothing taboo, refused or missing: at the ceiling its reply is the floor.
/// With `failing_entry`, every apply of that entry fails, so every row and the summary carry
/// the error. The snapshot, its size on the wire, and whether `write_response` sends it.
fn floor_status(
    document: &str,
    outputs: usize,
    failing_entry: Option<&str>,
) -> (serde_json::Value, usize, Result<(), String>) {
    floor_status_at(
        document,
        outputs,
        failing_entry,
        Path::new("/tmp/runtime.toml"),
    )
}

/// `floor_status` with the document at `config_path`, which status echoes (the budget doesn't
/// count it).
fn floor_status_at(
    document: &str,
    outputs: usize,
    failing_entry: Option<&str>,
    config_path: &Path,
) -> (serde_json::Value, usize, Result<(), String>) {
    let at = noon();
    let parsed = Config::from_bytes(document.as_bytes().to_vec()).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(
            (0..outputs)
                .map(|index| {
                    let mut name = "\"".repeat(249);
                    name.push('\\');
                    for bit in (0..6).rev() {
                        name.push(if index >> bit & 1 == 1 { '\\' } else { '"' });
                    }
                    name
                })
                .collect(),
        ),
        fail_entry_id: failing_entry.map(str::to_string),
        fail_entry_attempts_remaining: usize::MAX,
        ..RuntimeDriverState::default()
    }));
    let mut runtime =
        Runtime::new(config_path.to_path_buf(), parsed, RuntimeDriver(state), at).unwrap();
    let applied = runtime.apply_current();
    assert_eq!(applied.is_ok(), failing_entry.is_none(), "{applied:?}");
    runtime.tick(at, Instant::now());
    let response = runtime_command(&mut runtime, at, "status", None);
    assert!(response.ok, "{}", response.message);
    let mut encoded = Vec::new();
    let written = write_response(&mut encoded, &response);
    let wire = serde_json::to_vec(&response).unwrap().len() + 1;
    let snapshot = serde_json::from_str(&response.message).unwrap();
    (snapshot, wire, written)
}

/// What a 0.4.0 client reads of the configuration: every row, playlist and rule.
fn assert_inventory_whole(snapshot: &serde_json::Value, rows: usize, label: &str) {
    assert_eq!(
        snapshot["displays"].as_array().unwrap().len(),
        rows,
        "{label}"
    );
    assert_eq!(
        snapshot["playlists"].as_array().unwrap().len(),
        578,
        "{label}"
    );
    assert_eq!(
        snapshot["schedules"].as_array().unwrap().len(),
        512,
        "{label}"
    );
}

/// What `value` takes on the wire as compact JSON inside a reply's message (each of its
/// characters escaped once more).
fn json_wire_len(value: &serde_json::Value) -> usize {
    escaped_len(&serde_json::to_string(value).unwrap())
}

/// What one of `Runtime::abridged`'s units, a rule id or an error, adds beside the `null` or
/// `""` left in its place (4 bytes on the wire either).
fn row_unit_len(text: &str) -> usize {
    json_wire_len(&serde_json::Value::from(text)) - 4
}

/// What a flag `"key":true` takes on the wire.
fn flag_wire_len(key: &str) -> usize {
    escaped_len(&format!(",\"{key}\":true"))
}

/// The room a reply would have with every row's `schedule_rule_id` and `last_error`, and the
/// summary's own diagnostics, left out (so both flags set), worked out from one that kept some:
/// its room, plus what each it kept takes, less the flags it didn't carry. The status budget's
/// residue, whatever was given back.
fn room_with_row_units_out(snapshot: &serde_json::Value, wire: usize) -> i64 {
    let mut room = MAX_RESPONSE_BYTES as i64 - wire as i64;
    let (mut ids, mut errors) = (false, false);
    for row in snapshot["displays"].as_array().unwrap() {
        if let Some(id) = row["schedule_rule_id"].as_str() {
            room += row_unit_len(id) as i64;
            ids = true;
        }
        let error = row["last_error"].as_str().unwrap();
        if !error.is_empty() {
            room += row_unit_len(error) as i64;
            errors = true;
        }
    }
    for key in ["last_error", "output_discovery_error"] {
        let error = snapshot[key].as_str().unwrap();
        if !error.is_empty() {
            room += row_unit_len(error) as i64;
            errors = true;
        }
    }
    if ids && snapshot.get("schedule_rule_id_omitted").is_none() {
        room -= flag_wire_len("schedule_rule_id_omitted") as i64;
    }
    if errors && snapshot.get("diagnostics_omitted").is_none() {
        room -= flag_wire_len("diagnostics_omitted") as i64;
    }
    room
}

/// That a reply left out of its rows only as much as it had to (`Runtime::abridged`, review
/// r13e L6), and says so: each rule id or error it left out would take more than the room it
/// has (less that unit's flag, when it is the only one of its kind out), and a connected row
/// lost one only once every other row had. Every row is the in-force rule's (`rule_id`); the
/// errors left out are at least as long as each one kept.
fn assert_row_units_left_out_wouldnt_fit(
    label: &str,
    snapshot: &serde_json::Value,
    wire: usize,
    rule_id: &str,
) {
    let room = MAX_RESPONSE_BYTES - wire;
    let rows = snapshot["displays"].as_array().unwrap();
    let ids_out: Vec<&serde_json::Value> = rows
        .iter()
        .filter(|row| row["schedule_rule_id"].is_null())
        .collect();
    let errors_out: Vec<&serde_json::Value> = rows
        .iter()
        .filter(|row| row["renderer_failed"] == true && row["last_error"] == "")
        .collect();
    let summary_out = snapshot["renderer_failed"] == true && snapshot["last_error"] == "";
    assert_eq!(
        snapshot.get("schedule_rule_id_omitted").is_some(),
        !ids_out.is_empty(),
        "{label}: the rule ids' flag"
    );
    assert_eq!(
        snapshot.get("diagnostics_omitted").is_some(),
        !errors_out.is_empty() || summary_out,
        "{label}: the diagnostics' flag"
    );
    for row in rows {
        assert_eq!(row["route_source"], "schedule", "{label}");
        if !row["schedule_rule_id"].is_null() {
            assert_eq!(row["schedule_rule_id"], rule_id, "{label}");
        }
    }
    let flag_back = |out: usize, key: &str| if out == 1 { flag_wire_len(key) } else { 0 };
    if !ids_out.is_empty() {
        let cost = row_unit_len(rule_id)
            .saturating_sub(flag_back(ids_out.len(), "schedule_rule_id_omitted"));
        assert!(
            room < cost,
            "{label}: {room} bytes to spare beside {} rule ids left out of {cost} each",
            ids_out.len()
        );
    }
    let kept_error = rows
        .iter()
        .filter_map(|row| row["last_error"].as_str())
        .map(row_unit_len)
        .max()
        .unwrap_or(0);
    if !errors_out.is_empty() && kept_error > 0 {
        let flag = if summary_out {
            0
        } else {
            flag_back(errors_out.len(), "diagnostics_omitted")
        };
        assert!(
            room < kept_error.saturating_sub(flag),
            "{label}: {room} bytes to spare beside {} errors left out of {kept_error} or more",
            errors_out.len()
        );
    }
    // Every row has the rule's id; a row has an error when its renderer failed.
    for (out, what, had) in [
        (
            &ids_out,
            "rule id",
            (|_| true) as fn(&serde_json::Value) -> bool,
        ),
        (&errors_out, "error", |row| row["renderer_failed"] == true),
    ] {
        if out.iter().any(|row| row["connected"] == true) {
            assert!(
                rows.iter()
                    .filter(|row| row["connected"] == false && had(row))
                    .all(|row| out.iter().any(|left| left["connector"] == row["connector"])),
                "{label}: a connected row's {what} went before another's"
            );
        }
    }
}

#[test]
fn a_status_whose_rows_rule_ids_dont_fit_leaves_out_only_as_many_as_it_must() {
    // The review's corner, at the budget's edge, with a 256-byte rule id of quotes in force
    // (about 21 KB over the limit at 2adc5b4, and 15 KB at 0.4.0, which sent nothing), in both
    // modes. The rows' ids go one at a time, the detached rows' first, only until the reply
    // fits (review r13e L6; at 040b3aa every one went, with about 109 KB to spare). Every
    // playlist, rule and row is still there, and the rule is still said to be in force.
    let long = format!("{}R", "\\\"".repeat(255));
    let rule = format!("{}R", "\"".repeat(255));
    for mode in ["mirrored", "independent"] {
        let label = format!("{mode}, 256-byte rule id");
        let document = at_the_budgets_edge(|still| floor_corner(mode, &long, still));
        let (snapshot, wire, written) = floor_status(&document, 64, None);
        println!("{label}: {wire} bytes on the wire");
        assert!(
            written.is_ok() && wire <= MAX_RESPONSE_BYTES,
            "{label}: the reply is {wire} bytes: {written:?}"
        );
        assert_inventory_whole(&snapshot, 128, &label);
        let kept = snapshot["displays"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["schedule_rule_id"] == rule.as_str())
            .count();
        assert!(
            (64..128).contains(&kept),
            "{label}: {kept} rows kept the id"
        );
        assert_row_units_left_out_wouldnt_fit(&label, &snapshot, wire, &rule);
        let rule = snapshot["schedules"]
            .as_array()
            .unwrap()
            .last()
            .unwrap()
            .clone();
        assert_eq!(rule["in_force"], true, "{label}: {}", rule["id"]);
    }
}

#[test]
fn a_status_whose_rows_rule_ids_fit_keeps_them() {
    // The control: the same corners with a one-byte rule id (BIG's still takes the bytes).
    for mode in ["mirrored", "independent"] {
        let document = at_the_budgets_edge(|still| floor_corner(mode, "R", still));
        let (snapshot, wire, written) = floor_status(&document, 64, None);
        println!("{mode}, 1-byte rule id: {wire} bytes on the wire");
        assert!(written.is_ok(), "{mode}: {wire} bytes");
        assert_inventory_whole(&snapshot, 128, mode);
        assert!(snapshot.get("schedule_rule_id_omitted").is_none(), "{mode}");
        for row in snapshot["displays"].as_array().unwrap() {
            assert_eq!(row["schedule_rule_id"], "R", "{mode}");
        }
    }
}

#[test]
fn with_its_rows_units_left_out_the_worst_structure_has_room_to_spare() {
    // The worst structure the budget passes: every ceiling, and no string shorter on the wire
    // than the budget counts it. With every row's rule id and error left out its reply has room
    // to spare for the summary's own diagnostics (`last_error` is at most 12 KiB, four times
    // that escaped), whatever was then given back; with a one-byte rule id it fits as it is.
    for mode in ["mirrored", "independent"] {
        for rule_chars in [256, 1] {
            let label = format!("{mode}, {rule_chars}-byte rule id");
            let document =
                at_the_budgets_edge(|still| quoted_structure_corner(mode, rule_chars, still));
            let (snapshot, wire, written) = floor_status(&document, 64, None);
            let room = room_with_row_units_out(&snapshot, wire);
            println!("{label}: {wire} bytes on the wire, {room} with the rows' units out");
            assert!(written.is_ok(), "{label}: {wire} bytes");
            assert_inventory_whole(&snapshot, 128, &label);
            assert_eq!(
                snapshot.get("schedule_rule_id_omitted").is_some(),
                rule_chars == 256,
                "{label}"
            );
            assert!(
                room > 4 * 12 * 1024 + 4 * 1024,
                "{label}: {room} bytes to spare"
            );
        }
    }
}

#[test]
fn a_status_whose_diagnostics_dont_fit_leaves_out_as_few_as_it_must() {
    // The other text the budget doesn't count on every row: a row's `last_error` (up to 384
    // bytes each, which the budget's diagnostic reserve counts once, not four times). Every
    // apply of BIG's entry, whose id is 256 quotes, fails, so all 128 rows carry the error.
    // Only as many rows' errors go as must, the detached rows' first, and the one-byte rule ids
    // stay: they are the shortest units (review r13e N6: at 040b3aa all 128 went first, for
    // 128 bytes, and then every error too, with 150 KB to spare).
    let document = at_the_budgets_edge(|still| floor_corner("mirrored", "R", still));
    let entry = format!("{}E", "\"".repeat(255));
    let (snapshot, wire, written) = floor_status(&document, 64, Some(&entry));
    println!("every row failing: {wire} bytes on the wire");
    assert!(written.is_ok(), "{wire} bytes");
    assert_inventory_whole(&snapshot, 128, "errors");
    assert_eq!(snapshot["diagnostics_omitted"], true);
    assert!(
        snapshot.get("schedule_rule_id_omitted").is_none(),
        "no rule id was in the way"
    );
    assert_eq!(
        snapshot["renderer_failed"], true,
        "what failed is still said"
    );
    assert!(
        snapshot["last_error"]
            .as_str()
            .unwrap()
            .contains("is borked"),
        "the summary's own error is the last to go"
    );
    let rows = snapshot["displays"].as_array().unwrap();
    let kept = rows
        .iter()
        .filter(|row| !row["last_error"].as_str().unwrap().is_empty())
        .count();
    assert!((1..128).contains(&kept), "{kept} rows kept their error");
    for row in rows {
        assert_eq!(row["renderer_failed"], true);
    }
    assert_row_units_left_out_wouldnt_fit("errors", &snapshot, wire, "R");

    // The control: the same failure on 8 live displays, which leaves room for every error.
    let (snapshot, _, written) = floor_status(&document, 8, Some(&entry));
    assert!(written.is_ok());
    assert!(snapshot.get("diagnostics_omitted").is_none());
    assert!(snapshot.get("schedule_rule_id_omitted").is_none());
    assert!(
        snapshot["last_error"]
            .as_str()
            .unwrap()
            .contains("is borked")
    );
    for row in snapshot["displays"].as_array().unwrap() {
        assert!(!row["last_error"].as_str().unwrap().is_empty(), "{row}");
    }
}

// -- r13e: a reply that leaves out a stand-in says so ----------------------------------------

#[test]
fn a_reply_that_leaves_out_a_stand_in_says_so() {
    // At the ceiling, with 64 taboo records, every `stand_in_for` gives way: the app can't tell
    // from the rows that BIG plays for ASK, so the reply says it left them out.
    let (dropped, encoded) = refusal_ceiling_status("ASK", 20, 64);
    assert!(encoded <= MAX_RESPONSE_BYTES, "{encoded} bytes");
    assert_eq!(stand_ins(&dropped), 0);
    assert_eq!(dropped["stand_in_for_omitted"], true);
    // Controls: with 28 records every stand-in fits; a pick of BIG has none to leave out.
    let (kept, _) = refusal_ceiling_status("ASK", 20, 28);
    assert_eq!(stand_ins(&kept), 129);
    assert!(
        kept.get("stand_in_for_omitted").is_none(),
        "nothing was left out"
    );
    let (none, _) = refusal_ceiling_status("BIG", 20, 64);
    assert_eq!(stand_ins(&none), 0);
    assert!(
        none.get("stand_in_for_omitted").is_none(),
        "nothing stands in"
    );
    // And far below the ceiling, a stand-in is named, with no flag.
    let (mut runtime, root, home) = rule_boundary_runtime("r13e-stand-in-flag", noon());
    let snapshot = status(&mut runtime, noon());
    remove_skip_fixture(runtime, &root, &home);
    assert_eq!(route(&snapshot, "DP-1")["stand_in_for"], "pages");
    assert!(snapshot.get("stand_in_for_omitted").is_none(), "{snapshot}");
}

#[test]
fn at_the_ceiling_only_as_many_taboo_records_go_as_must() {
    // Review r13d N8: the taboo records kept are counted by measuring them, not by building
    // the reply again for each count tried. One more than are kept would not fit.
    // (61 kept at 040b3aa: a lean reply leaves out what says nothing, room for one more.)
    let (snapshot, encoded) = refusal_ceiling_status("BIG", 20, 64);
    let kept = snapshot["taboo_entries"].as_array().unwrap();
    println!(
        "{} kept, {} bytes to spare, this build's strings {} bytes beyond the reference's",
        kept.len(),
        MAX_RESPONSE_BYTES - encoded,
        build_beyond_the_reference(&snapshot)
    );
    // 62 with build strings as long as the band's reference (about 3 KB was left beside a
    // 4 KB record, so 1 KB either way keeps it).
    assert_some_checked(
        "62 records kept",
        usize::from(build_beyond_the_reference(&snapshot) <= 1_000),
    );
    assert_eq!(kept.len(), 62);
    assert_eq!(snapshot["taboo_entries_omitted"], 2);
    // A record's bytes on the wire: its JSON, each quote and backslash escaped once more.
    let record = serde_json::to_string(&kept[0]).unwrap();
    let on_wire = record.len() + record.matches(['"', '\\']).count();
    assert!(
        MAX_RESPONSE_BYTES - encoded < on_wire + 1,
        "{} bytes to spare beside a {on_wire}-byte record",
        MAX_RESPONSE_BYTES - encoded
    );
}

// -- r13e N7: what is left with the rows' units out still fits, whatever else is at its longest -
//
// The review's T2: the worst structure (`quoted_structure_corner`, a 256-byte rule id, both
// modes, at the budget's edge) beside every string the budget doesn't count at its longest at
// once: a `config_path` of 4,096 quotes, a mirrored `theme_source_connector` of 256 quotes and
// backslashes, timing on every live row, every row failing or retrying, 64 live outputs of 256
// quotes. What has to fit beside that is `runtime_executable` at its worst.

/// `quoted_structure_corner` with knobs: BIG's entry id `entry_quotes` quotes (64 there), the
/// in-force rule with a time window (`window`: 00:00-23:59, so every live row has a schedule
/// change to report), rotation on (`cycle`), in mirrored mode a theme source of 256 quotes and
/// backslashes (`mirrored_theme`; the budget doesn't count it), and a second entry of 256
/// quotes for the rotation to fail on (`second_entry`).
fn unbudgeted_corner(mode: &str, still: usize, extras: Unbudgeted) -> String {
    let quotes = |count: usize| "\\\"".repeat(count);
    let big = quotes(64);
    let entry_quotes = if extras.failing && !extras.retrying {
        256
    } else {
        64
    };
    let theme = if mode == "independent" || extras.mirrored_theme {
        format!("{}{}", quotes(250), quoted_text("", 0, 6))
    } else {
        String::new()
    };
    let months =
        "months = [10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9]\nweekdays = [0, 1, 2, 3, 4, 5, 6]\n";
    let mut document = floor_corner_head(mode, &big, &theme)
        + &format!(
            "[[playlists]]\nid = \"{big}\"\nname = \"{}\"\n[[playlists.entries]]\nid = \"{}\"\n\
             kind = \"still\"\nstill = \"/{}\"\npalette = {{ kind = \"keep\", mode = \"keep\" }}\n",
            "\\\\".repeat(32),
            quotes(entry_quotes),
            quotes(still),
        );
    if extras.retrying {
        document.push_str(&format!(
            "[[playlists.entries]]\nid = \"{}\"\nkind = \"still\"\nstill = \"/s.png\"\n\
             palette = {{ kind = \"keep\", mode = \"keep\" }}\n",
            quotes(256)
        ));
    }
    for index in 0..577 {
        document.push_str(&format!(
            "[[playlists]]\nid = \"{}\"\nname = \"{}\"\n\
             [[playlists.entries]]\nid = \"e\"\nkind = \"still\"\nstill = \"/s.png\"\n\
             palette = {{ kind = \"keep\", mode = \"keep\" }}\n",
            quoted_text("\"", index, 10),
            quoted_text("\\", index, 10),
        ));
    }
    for index in 0..511 {
        document.push_str(&format!(
            "[[schedules]]\nid = \"{}\"\nplaylist = \"{}\"\nenabled = false\n{months}",
            quoted_text("\"", index, 10),
            quoted_text("\"", index % 577, 10),
        ));
    }
    document.push_str(&format!(
        "[[schedules]]\nid = \"{}\"\nplaylist = \"{big}\"\n{months}{}",
        "\\\\".repeat(256),
        if extras.window {
            "start = \"00:00\"\nend = \"23:59\"\n"
        } else {
            ""
        }
    ));
    for index in 0..64 {
        document.push_str(&format!(
            "[[displays]]\nconnector = \"{}{}\"\nplaylist = \"{big}\"\n",
            quotes(250),
            quoted_text("", index, 6)
        ));
    }
    if extras.cycle || extras.retrying {
        document = document.replacen("cycle_enabled = false", "cycle_enabled = true", 1);
    }
    document
}

/// Which of the strings the budget doesn't count `unbudgeted_corner` and
/// `unbudgeted_residue` put at their longest.
#[derive(Clone, Copy, Debug)]
struct Unbudgeted {
    long_path: bool,
    window: bool,
    cycle: bool,
    mirrored_theme: bool,
    failing: bool,
    /// Rotation on, onto a second entry that fails: every live row retrying.
    retrying: bool,
}

const UNBUDGETED_NONE: Unbudgeted = Unbudgeted {
    long_path: false,
    window: false,
    cycle: false,
    mirrored_theme: false,
    failing: false,
    retrying: false,
};

const UNBUDGETED_ALL: Unbudgeted = Unbudgeted {
    long_path: true,
    window: true,
    cycle: true,
    mirrored_theme: true,
    failing: true,
    retrying: false,
};

/// The longest config path `Runtime::new` takes: 4,096 bytes, components of 255 quotes
/// between slashes. Status echoes it; the budget doesn't count it.
fn longest_config_path() -> PathBuf {
    let mut path = String::new();
    while path.len() < 4096 {
        path.push('/');
        let left = 4096 - path.len();
        path.push_str(&"\"".repeat(left.min(255)));
    }
    assert_eq!(path.len(), 4096);
    PathBuf::from(path)
}

/// `runtime_executable` at its worst on the wire: `current_exe()` reads /proc/self/exe, at most
/// 4,095 bytes, components of at most 255; a control character is `\u00XX` in the JSON, 7
/// bytes on the wire. 16 slashes and 4,079 control characters.
const WORST_EXECUTABLE_WIRE: usize = 16 + 4079 * 7;

/// The status of `unbudgeted_corner(mode, ..)` at the budget's edge with 64 live outputs of
/// 256 quotes, every row failing or retrying as `extras` says: the snapshot, its size on the
/// wire, whether it is sent, and the room it would have with the rows' units out
/// (`room_with_row_units_out`).
fn unbudgeted_residue(
    mode: &str,
    extras: Unbudgeted,
) -> (serde_json::Value, usize, Result<(), String>, i64) {
    let document = at_the_budgets_edge(|still| unbudgeted_corner(mode, still, extras));
    let failing = extras.failing || extras.retrying;
    let entry = "\"".repeat(if extras.retrying || extras.failing {
        256
    } else {
        64
    });
    let at = noon();
    let parsed = Config::from_bytes(document.into_bytes()).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(
            (0..64)
                .map(|index| {
                    let mut name = "\"".repeat(249);
                    name.push('\\');
                    for bit in (0..6).rev() {
                        name.push(if index >> bit & 1 == 1 { '\\' } else { '"' });
                    }
                    name
                })
                .collect(),
        ),
        fail_entry_id: failing.then(|| entry.clone()),
        fail_entry_attempts_remaining: usize::MAX,
        ..RuntimeDriverState::default()
    }));
    let path = if extras.long_path {
        longest_config_path()
    } else {
        PathBuf::from("/tmp/runtime.toml")
    };
    let mut runtime = Runtime::new(path, parsed, RuntimeDriver(state), at).unwrap();
    let applied = runtime.apply_current();
    assert_eq!(applied.is_ok(), !failing || extras.retrying, "{applied:?}");
    runtime.tick(at, Instant::now());
    if extras.retrying {
        // The rotation is due: it moves to the failing entry, which fails, and retries.
        runtime.tick(at, Instant::now() + Duration::from_secs(301));
    }
    let response = runtime_command(&mut runtime, at, "status", None);
    assert!(response.ok, "{}", response.message);
    let mut encoded = Vec::new();
    let written = write_response(&mut encoded, &response);
    let wire = serde_json::to_vec(&response).unwrap().len() + 1;
    let snapshot: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    let room = room_with_row_units_out(&snapshot, wire);
    let rows = snapshot["displays"].as_array().unwrap();
    println!(
        "[{mode}] {extras:?}: rows {}, timed {}, retrying {}, wire {wire}, room with the rows' \
         units out {room}, written {written:?}",
        rows.len(),
        rows.iter()
            .filter(|row| !row["route_change_at"].is_null() || !row["next_cycle_at"].is_null())
            .count(),
        rows.iter()
            .filter(|row| !row["automatic_retry"].is_null())
            .count(),
    );
    (snapshot, wire, written, room)
}

/// What the worst `runtime_executable` takes on the wire beyond this test binary's own, which
/// every reply measured here carries.
fn worst_executable_extra() -> i64 {
    let now = std::env::current_exe()
        .ok()
        .and_then(|path| path.to_str().map(escaped_len))
        .unwrap_or(0);
    WORST_EXECUTABLE_WIRE as i64 - now as i64
}

#[test]
fn with_every_unbudgeted_string_at_its_longest_the_rows_units_out_leave_room_for_the_executable() {
    // Review r13e T2 and N7. What is left with every row's rule id and error out is the
    // budget's residue: it must hold the worst `runtime_executable` beside everything else the
    // budget doesn't count at its longest, or a document the budget passes could get no reply.
    let extra = worst_executable_extra();
    for mode in ["mirrored", "independent"] {
        for extras in [
            UNBUDGETED_ALL,
            Unbudgeted {
                failing: false,
                retrying: true,
                ..UNBUDGETED_ALL
            },
        ] {
            let (snapshot, wire, written, room) = unbudgeted_residue(mode, extras);
            assert!(
                written.is_ok(),
                "[{mode}] {extras:?}: {wire} bytes: no reply"
            );
            assert_inventory_whole(&snapshot, 128, mode);
            assert!(
                room >= extra,
                "[{mode}] {extras:?}: {room} bytes with the rows' units out, but the worst \
                 runtime_executable needs {extra} more"
            );
            println!(
                "[{mode}] {extras:?}: {} bytes to spare beside the worst runtime_executable",
                room - extra
            );
        }
    }
}

#[test]
fn control_without_the_unbudgeted_strings_the_residue_is_larger() {
    // Each extra on its own (said, not judged), then none: the room the residue has is what
    // they take less.
    for mode in ["mirrored", "independent"] {
        let (_, _, _, all) = unbudgeted_residue(mode, UNBUDGETED_ALL);
        for extras in [
            Unbudgeted {
                long_path: true,
                ..UNBUDGETED_NONE
            },
            Unbudgeted {
                window: true,
                cycle: true,
                ..UNBUDGETED_NONE
            },
            Unbudgeted {
                mirrored_theme: mode == "mirrored",
                ..UNBUDGETED_NONE
            },
            Unbudgeted {
                failing: true,
                ..UNBUDGETED_NONE
            },
            Unbudgeted {
                retrying: true,
                ..UNBUDGETED_NONE
            },
        ] {
            let _ = unbudgeted_residue(mode, extras);
        }
        let (_, _, written, none) = unbudgeted_residue(mode, UNBUDGETED_NONE);
        assert!(written.is_ok(), "{mode}");
        assert!(
            none > all,
            "[{mode}] {none} bytes without the extras, {all} with them"
        );
    }
}

// -- r13e L6: a document 0.4.0 answered gets at least what 0.4.0 sent -------------------------
//
// The review's band: its corner (`floor_corner`) at the budget's edge, a rule in force whose id
// is quotes, 64 short taboo records ("band 000" and on), 64 live outputs of 256 bytes. At
// 040b3aa steps 6 and 7 left out every rule id and error at once, and gave nothing back, where
// 0.4.0 had sent every one of them and 27 to 41 records; and 0.4.1's null fields took the room
// of about 30 records 0.4.0 had sent. `BAND_AT_0_4_0` is what d159a20 (0.4.0) sent for the same
// documents, from the same text run in a d159a20 tree (r13f's probe, 2026-10-25).

/// `floor_corner` with `records` short taboo records on its small playlists.
fn band_corner(mode: &str, rule_id: &str, still: usize, records: usize) -> String {
    let mut document = floor_corner(mode, rule_id, still);
    for index in 0..records {
        let block = format!(
            "[[playlists]]\nid = \"{index:03}\"\nname = \"n{index:03}\"\n\
             [[playlists.entries]]\nid = \"e\"\nkind = \"still\"\nstill = \"/s.png\"\n"
        );
        let at = document.find(&block).unwrap() + block.len();
        document.insert_str(
            at,
            &format!("taboo = {{ reason = \"band {index:03}\", source = \"review\" }}\n"),
        );
    }
    document
}

/// One of the band's documents: its mode, the length of the in-force rule's id (a `R` after
/// quotes), its records, and the status asked: every row failing on BIG's entry, with that many
/// quotes in the live outputs' names (otherwise 254); `published` monitors; and both renderers
/// missing at the longest path.
#[derive(Clone, Copy, Debug)]
struct BandCorner {
    mode: &'static str,
    rule_chars: usize,
    records: usize,
    failing_quoted: Option<usize>,
    published: usize,
    missing: bool,
}

/// What a band reply carried: rows' rule ids and errors, the summary's error, taboo records,
/// `missing_renderers` rows and those with a path, `outputs`, and how many bytes it had to
/// spare.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct BandSent {
    rule_ids: usize,
    errors: usize,
    summary_error: bool,
    taboo: usize,
    missing: usize,
    missing_paths: usize,
    outputs: usize,
    spare: usize,
}

const fn band(mode: &'static str, rule_chars: usize, records: usize) -> BandCorner {
    BandCorner {
        mode,
        rule_chars,
        records,
        failing_quoted: None,
        published: 0,
        missing: false,
    }
}

const fn failing_band(mode: &'static str, quoted: usize) -> BandCorner {
    BandCorner {
        failing_quoted: Some(quoted),
        ..band(mode, 1, 0)
    }
}

const fn sent(rule_ids: usize, errors: usize, taboo: usize, spare: usize) -> BandSent {
    BandSent {
        rule_ids,
        errors,
        summary_error: errors > 0,
        taboo,
        missing: 0,
        missing_paths: 0,
        outputs: 0,
        spare,
    }
}

/// What d159a20 sent for each corner (every row: 128 rows, and every playlist and rule).
const BAND_AT_0_4_0: [(BandCorner, BandSent); 13] = [
    (band("mirrored", 1, 64), sent(128, 0, 64, 103_690)),
    (band("mirrored", 210, 64), sent(128, 0, 41, 10)),
    (band("mirrored", 214, 64), sent(128, 0, 30, 75)),
    (band("mirrored", 220, 64), sent(128, 0, 17, 6)),
    (band("mirrored", 226, 64), sent(128, 0, 1, 6)),
    (band("mirrored", 226, 0), sent(128, 0, 0, 201)),
    (band("independent", 210, 64), sent(128, 0, 30, 120)),
    (band("independent", 216, 64), sent(128, 0, 17, 51)),
    (band("independent", 222, 64), sent(128, 0, 1, 51)),
    (failing_band("mirrored", 48), sent(128, 128, 0, 908)),
    (failing_band("independent", 180), sent(128, 64, 0, 951)),
    (
        BandCorner {
            missing: true,
            ..band("mirrored", 212, 64)
        },
        sent(128, 0, 35, 140),
    ),
    (
        BandCorner {
            published: 1,
            missing: true,
            ..band("independent", 214, 64)
        },
        BandSent {
            missing: 2,
            ..sent(128, 0, 19, 41)
        },
    ),
];

/// What every reply in `BAND_AT_0_4_0` carried that depends on how its test binary was built
/// and where: that binary's path (`runtime_executable`), `install_kind` (`source` in a dev
/// shell, `nix` in the package's build) and `runtime_version`; and the configuration's path,
/// `config_path`. A band reply here says the reference's binary path whatever binary runs it
/// (`with_the_references_executable`), and sets its configuration's path to make up for the
/// other two (`config_path_with_the_references_room`). A reply that doesn't carries its own,
/// and has as much less room as they take more (`build_beyond_the_reference`).
const BAND_REFERENCE_BUILD: [(&str, &str); 4] = [
    (
        "runtime_executable",
        "/home/goober/Documents/wall-in-one/.project-notes/wt/cargo-target-r13f-d159a20/debug/deps/service-5e4e8374fbeba07c",
    ),
    ("install_kind", "source"),
    ("runtime_version", "0.4.0"),
    ("config_path", "/tmp/runtime.toml"),
];

/// How many bytes on the wire `snapshot` takes beyond the reference replies in what depends on
/// its build (`BAND_REFERENCE_BUILD`): the room it has less than they had, through no change of
/// the reply's. 0 for a band reply here, whose configuration path makes up for this build's
/// strings, unless they take more than 15 bytes beyond the reference's.
fn build_beyond_the_reference(snapshot: &serde_json::Value) -> i64 {
    BAND_REFERENCE_BUILD
        .iter()
        .map(|(key, reference)| {
            json_wire_len(&snapshot[*key]) as i64
                - json_wire_len(&serde_json::Value::from(*reference)) as i64
        })
        .sum()
}

/// Make `runtime` say the band reference's binary path as its own (`runtime_executable`),
/// whatever binary runs it and wherever it was built: the room a reply has then doesn't depend
/// on that path.
fn with_the_references_executable<D: WallpaperDriver>(runtime: &mut Runtime<D>) {
    runtime.set_runtime_executable_for_tests(Some(BAND_REFERENCE_BUILD[0].1));
}

/// How many bytes on the wire this test binary's build strings take beyond the band
/// reference's, its path said as the reference's (`with_the_references_executable`): its
/// `install_kind` and `runtime_version`, measured once from a reply at the reference's
/// configuration path. Negative in the package's build, which says `nix`.
fn this_builds_strings_beyond_the_reference() -> i64 {
    static BEYOND: OnceLock<i64> = OnceLock::new();
    *BEYOND.get_or_init(|| {
        let (mut runtime, _) =
            default_rule_runtime(default_rule_document("all-media"), &[], noon());
        with_the_references_executable(&mut runtime);
        let snapshot = status(&mut runtime, noon());
        assert_eq!(snapshot["config_path"], "/tmp/runtime.toml");
        build_beyond_the_reference(&snapshot)
    })
}

/// A configuration path as much shorter than the reference's `/tmp/runtime.toml` as this
/// build's other strings take more on the wire, or longer as they take less (each of its bytes
/// is one on the wire), so that a reply here, saying the reference's binary path, has the
/// reference's room to the byte. No path is shorter than `/r`: strings more than 15 bytes
/// beyond the reference's would leave the rest less room all the same, which an install kind
/// and a version never take.
fn config_path_with_the_references_room() -> PathBuf {
    let reference = "/tmp/runtime.toml".len() as i64;
    let length = reference - this_builds_strings_beyond_the_reference();
    if length >= reference {
        config_path_longer_by(usize::try_from(length - reference).unwrap())
    } else {
        PathBuf::from(format!("/{}", "r".repeat(length.max(2) as usize - 1)))
    }
}

/// One of the band's published monitors.
fn band_monitor(index: usize) -> Output {
    Output {
        name: format!("DP-{index}"),
        enabled: true,
        make: "Dell Inc.".into(),
        model: "U2720Q".into(),
        width: 2560,
        height: 1440,
        scale: 1.5,
        mode_width: 3840,
        mode_height: 2160,
        refresh_millihz: Some(59_997),
        physical_width_mm: 597,
        physical_height_mm: 336,
        ..Output::default()
    }
}

/// How much smaller a lean reply is than 0.4.0's for the same content, for a document with no
/// overrides file: `loaded_overrides_sha256` and `overrides_ignored` (60 bytes) less the three
/// schemas 0.4.1 lists beside 0.4.0's two (6).
const LEAN_UNDER_0_4_0: usize = 54;

/// The status of `corner` at the budget's edge, its snapshot and size on the wire.
fn band_status(corner: BandCorner) -> (serde_json::Value, usize) {
    let rule = format!("{}R", "\\\"".repeat(corner.rule_chars - 1));
    let document =
        at_the_budgets_edge(|still| band_corner(corner.mode, &rule, still, corner.records));
    let monitors: Vec<Output> = (0..corner.published).map(band_monitor).collect();
    RUNTIME_DRIVER_PUBLISHED.with(|cell| cell.set(Box::leak(monitors.into_boxed_slice())));
    let at = noon();
    let quoted = corner.failing_quoted.unwrap_or(254);
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(
            (0..64)
                .map(|index| {
                    format!(
                        "{}{}{index:02}",
                        "\"".repeat(quoted),
                        "x".repeat(254 - quoted)
                    )
                })
                .collect(),
        ),
        fail_entry_id: corner
            .failing_quoted
            .map(|_| format!("{}E", "\"".repeat(255))),
        fail_entry_attempts_remaining: usize::MAX,
        missing_renderers: if corner.missing {
            longest_missing_renderers()
        } else {
            Vec::new()
        },
        ..RuntimeDriverState::default()
    }));
    let parsed = Config::from_bytes(document.into_bytes()).unwrap();
    let mut runtime = Runtime::new(
        config_path_with_the_references_room(),
        parsed,
        RuntimeDriver(state),
        at,
    )
    .unwrap();
    with_the_references_executable(&mut runtime);
    let _ = runtime.apply_current();
    runtime.tick(at, Instant::now());
    let response = runtime_command(&mut runtime, at, "status", None);
    RUNTIME_DRIVER_PUBLISHED.with(|cell| cell.set(&[]));
    assert!(response.ok, "{}", response.message);
    let mut encoded = Vec::new();
    let written = write_response(&mut encoded, &response);
    let wire = serde_json::to_vec(&response).unwrap().len() + 1;
    assert!(written.is_ok(), "{corner:?}: {wire} bytes: no reply");
    let snapshot: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    assert_inventory_whole(&snapshot, 128, &format!("{corner:?}"));
    (snapshot, wire)
}

/// What `snapshot` (`wire` bytes on the wire) carried, as `BandSent` counts it.
fn band_sent(snapshot: &serde_json::Value, wire: usize) -> BandSent {
    let rows = snapshot["displays"].as_array().unwrap();
    let missing = snapshot["missing_renderers"].as_array().unwrap();
    BandSent {
        rule_ids: rows
            .iter()
            .filter(|row| !row["schedule_rule_id"].is_null())
            .count(),
        errors: rows.iter().filter(|row| row["last_error"] != "").count(),
        summary_error: snapshot["last_error"] != "",
        taboo: snapshot["taboo_entries"].as_array().unwrap().len(),
        missing: missing.len(),
        missing_paths: missing.iter().filter(|row| row["path"] != "").count(),
        outputs: snapshot["outputs"].as_array().unwrap().len(),
        spare: MAX_RESPONSE_BYTES - wire,
    }
}

/// That `tip` sent at least what `reference` (0.4.0) did, in the documented order of what is
/// kept longest: every row's rule id and error 0.4.0 sent, and the summary's error, then the
/// taboo records; `missing_renderers` and `outputs` only where 0.4.0 kept no more records
/// (none of them is ever kept at a record's expense, at 0.4.0 or now).
fn assert_at_least_what_0_4_0_sent(label: &str, tip: BandSent, reference: BandSent) {
    assert!(
        tip.rule_ids >= reference.rule_ids,
        "{label}: {tip:?} beside 0.4.0's {reference:?}"
    );
    assert!(
        tip.errors >= reference.errors,
        "{label}: {tip:?} beside 0.4.0's {reference:?}"
    );
    assert!(
        tip.summary_error >= reference.summary_error,
        "{label}: {tip:?} beside 0.4.0's {reference:?}"
    );
    let order = |sent: BandSent| (sent.taboo, sent.missing, sent.missing_paths, sent.outputs);
    assert!(
        order(tip) >= order(reference),
        "{label}: {tip:?} beside 0.4.0's {reference:?}"
    );
}

/// Whether a reply built here has at least the room 0.4.0's had at a corner (`reference`), the
/// lean saving counted: where it has less, through its build alone, it is not compared, and
/// says so on stderr.
fn comparable_to_0_4_0(label: &str, snapshot: &serde_json::Value, reference: &BandSent) -> bool {
    let beyond = build_beyond_the_reference(snapshot);
    let comparable = beyond <= (reference.spare + LEAN_UNDER_0_4_0) as i64;
    if !comparable {
        eprintln!(
            "{label}: NOT COMPARED with 0.4.0: this build's strings take {beyond} bytes beyond \
             the reference's, more than its {} bytes to spare and the lean saving",
            reference.spare
        );
    }
    comparable
}

/// That a check skipped where this build leaves too little room (each skip said on stderr) was
/// made at least once (`checked` times): a test that checked nothing fails rather than pass.
fn assert_some_checked(what: &str, checked: usize) {
    assert!(
        checked > 0,
        "{what}: skipped every time: this build's strings take {} bytes beyond the reference's, \
         which a configuration path can't make up for past 15",
        this_builds_strings_beyond_the_reference()
    );
}

/// The rule every band reply holds whatever room this build leaves it: nothing of what it left
/// out would fit, rows' rule ids and errors, taboo records, `missing_renderers` and `outputs`.
fn assert_band_rule(label: &str, corner: BandCorner, snapshot: &serde_json::Value, wire: usize) {
    let rule = format!("{}R", "\"".repeat(corner.rule_chars - 1));
    assert_row_units_left_out_wouldnt_fit(label, snapshot, wire, &rule);
    assert_given_back_where_it_fits(label, corner, snapshot, wire);
}

/// Each corner of `corners` holds the rule (`assert_band_rule`), and, where this build has the
/// room 0.4.0 had, sends at least what 0.4.0 sent; at least one is compared.
fn assert_band_at_least_0_4_0(corners: std::ops::Range<usize>) {
    let mut compared = 0;
    for (corner, reference) in &BAND_AT_0_4_0[corners.clone()] {
        let label = format!("{corner:?}");
        let (snapshot, wire) = band_status(*corner);
        assert_band_rule(&label, *corner, &snapshot, wire);
        if !comparable_to_0_4_0(&label, &snapshot, reference) {
            continue;
        }
        compared += 1;
        let tip = band_sent(&snapshot, wire);
        println!("{label}: 0.4.0 {reference:?}; now {tip:?}");
        assert_at_least_what_0_4_0_sent(&label, tip, *reference);
        // 0.4.0 sent every row's id and error here, so nothing of them is left out now.
        assert!(
            snapshot.get("schedule_rule_id_omitted").is_none(),
            "{label}"
        );
        assert!(snapshot.get("diagnostics_omitted").is_none(), "{label}");
    }
    assert_some_checked(&format!("band corners {corners:?} beside 0.4.0"), compared);
}

#[test]
fn a_band_document_0_4_0_answered_gets_at_least_what_0_4_0_sent_mirrored() {
    // Review r13e L6. 0.4.0's tightest corners had 6 bytes to spare; a lean reply carrying what
    // it carried is 54 bytes smaller. At 040b3aa the 210-byte id kept 11 records of 0.4.0's 41,
    // the 220- and 226-byte ids none of the rule ids.
    assert_band_at_least_0_4_0(1..6);
}

#[test]
fn a_band_document_0_4_0_answered_gets_at_least_what_0_4_0_sent_independent() {
    assert_band_at_least_0_4_0(6..9);
}

#[test]
fn a_failing_band_document_0_4_0_answered_gets_every_error_0_4_0_sent() {
    // Every row failing, a one-byte rule id: 0.4.0 sent all 128 rows' errors and ids with 908
    // and 951 bytes to spare; at 040b3aa the mirrored corner sent none of either.
    assert_band_at_least_0_4_0(9..11);
}

/// That a band reply gave back, after its records, whatever still fits, in the documented
/// order: as many records as fit (`assert_next_record_wouldnt_fit`), then `missing_renderers`'
/// rows without their paths (their paths, 1 KB each, never fit here), then the published
/// monitors, each left out only where it would take more than the room left.
fn assert_given_back_where_it_fits(
    label: &str,
    corner: BandCorner,
    snapshot: &serde_json::Value,
    wire: usize,
) {
    assert_next_record_wouldnt_fit(
        label,
        snapshot,
        wire,
        &band_record(corner.records.saturating_sub(1)),
    );
    let room = MAX_RESPONSE_BYTES - wire;
    let sent = band_sent(snapshot, wire);
    assert_eq!(sent.missing_paths, 0, "{label}: {sent:?}");
    if corner.missing && sent.missing == 0 {
        let rows = serde_json::json!([
            { "program": "mpvpaper", "path": "", "reason": "not-found" },
            { "program": "linux-wallpaperengine", "path": "", "reason": "not-found" },
        ]);
        let cost = json_wire_len(&rows) - 2;
        assert!(
            room < cost,
            "{label}: {room} bytes left beside {cost} of missing renderers: {sent:?}"
        );
    }
    if corner.published > 0 && sent.outputs == 0 {
        let monitors: Vec<Output> = (0..corner.published).map(band_monitor).collect();
        let cost = json_wire_len(&serde_json::to_value(&monitors).unwrap()) - 2;
        assert!(
            room < cost,
            "{label}: {room} bytes left beside {cost} of outputs: {sent:?}"
        );
    }
}

#[test]
fn what_is_left_beside_the_records_goes_back_to_missing_renderers_and_outputs() {
    // The give-back after the records: two missing renderers (their 1 KB paths never fit, the
    // rows without them do) beside the band's records, and one published monitor. Where 0.4.0
    // had no room left for them (212-byte id) a lean reply has; where it had, and the extra
    // room takes one more record instead (214-byte id, independent), the record goes first, as
    // at 0.4.0. Whatever room this build leaves, nothing left out would fit, and nothing came
    // back at a record's expense.
    assert_band_at_least_0_4_0(11..13);
    let mut compared = 0;
    for (corner, reference) in &BAND_AT_0_4_0[11..13] {
        let label = format!("{corner:?}");
        let (snapshot, wire) = band_status(*corner);
        let sent = band_sent(&snapshot, wire);
        println!("{label}: 0.4.0 {reference:?}; now {sent:?}");
        // With at least the reference's room: something more than 0.4.0 sent (the two rows, or
        // one more record), in the documented order. With less, it may not fit, and a reply
        // that sent nothing more says nothing either way.
        let more = (sent.taboo, sent.missing) > (reference.taboo, reference.missing);
        let beyond = build_beyond_the_reference(&snapshot);
        if beyond > 0 && !more {
            eprintln!(
                "{label}: NOT COMPARED for more than 0.4.0 sent: this build's strings take \
                 {beyond} bytes beyond the reference's"
            );
            continue;
        }
        compared += 1;
        assert!(more, "{label}: nothing came back: {sent:?}");
    }
    assert_some_checked("more than 0.4.0 sent beside the records", compared);
}

#[test]
fn control_a_band_document_far_from_the_limit_is_whole_and_says_its_nulls() {
    // A one-byte rule id with 64 records: whole at 0.4.0 and now, with 0.4.1's fields as they
    // are below the limit (null, false, [] and 0), and no flag.
    assert_band_at_least_0_4_0(0..1);
    let (snapshot, wire) = band_status(BAND_AT_0_4_0[0].0);
    assert_eq!(band_sent(&snapshot, wire).taboo, 64);
    for (key, value) in [
        ("nothing_to_play", serde_json::Value::Null),
        ("allow_web_wallpapers", false.into()),
        ("refused", serde_json::json!([])),
        ("refused_omitted", 0.into()),
        ("loaded_overrides_sha256", serde_json::Value::Null),
        ("overrides_ignored", serde_json::Value::Null),
    ] {
        assert_eq!(snapshot.get(key), Some(&value), "{key}");
    }
    for row in snapshot["displays"].as_array().unwrap() {
        assert_eq!(row.get("nothing_to_play"), Some(&serde_json::Value::Null));
        assert_eq!(row.get("entry_name"), Some(&serde_json::Value::Null));
    }
}

#[test]
fn a_reply_at_its_limit_leaves_out_first_what_says_nothing() {
    // Review r13e L6: the fields 0.4.1 added took, while they said nothing, the room of about
    // 30 of the records 0.4.0 sent. At the limit they are left out first (absent reads as null,
    // false, [] or 0, and no flag says so), with 0.4.0's two null overrides fields, so a reply
    // carrying what 0.4.0's carried is smaller than 0.4.0's though it lists three more schemas.
    // Every row failing, with no records: what it carries is 0.4.0's whatever this build's
    // own strings take, short of 962 bytes more.
    let corner = BAND_AT_0_4_0[9];
    let (snapshot, wire) = band_status(corner.0);
    let tip = band_sent(&snapshot, wire);
    assert!(comparable_to_0_4_0("lean", &snapshot, &corner.1));
    assert_eq!(
        BandSent { spare: 0, ..tip },
        BandSent {
            spare: 0,
            ..corner.1
        }
    );
    for key in [
        "nothing_to_play",
        "allow_web_wallpapers",
        "refused",
        "refused_omitted",
        "loaded_overrides_sha256",
        "overrides_ignored",
        "stand_in_for_omitted",
        "schedule_rule_id_omitted",
        "diagnostics_omitted",
    ] {
        assert!(snapshot.get(key).is_none(), "{key}: {}", snapshot[key]);
    }
    assert_eq!(
        snapshot["supported_config_schemas"],
        serde_json::json!([4, 5, 6, 7, 8])
    );
    for row in snapshot["displays"].as_array().unwrap() {
        assert!(row.get("nothing_to_play").is_none(), "{row}");
        assert!(row.get("entry_name").is_none(), "{row}");
    }
    // The same content as 0.4.0's, LEAN_UNDER_0_4_0 bytes smaller, this build's strings aside.
    let beyond = build_beyond_the_reference(&snapshot);
    assert_eq!(
        tip.spare as i64 + beyond,
        (corner.1.spare + LEAN_UNDER_0_4_0) as i64,
        "{tip:?}"
    );
}

#[test]
fn a_lean_reply_that_fits_whole_keeps_its_outputs() {
    // The lean whole reply is measured from the whole one and built only when it fits. Here the
    // whole reply, nulls and all, is over by less than what the nulls take, so the lean one is
    // the reply, with the compositor's snapshot: were the saving ever measured short, the
    // fitting would go on without `outputs`, and nothing else would tell.
    let corner = BandCorner {
        published: 1,
        ..band("mirrored", 216, 0)
    };
    let (snapshot, wire) = band_status(corner);
    let tip = band_sent(&snapshot, wire);
    assert_eq!((tip.rule_ids, tip.outputs), (128, 1), "{tip:?}");
    for flag in [
        "stand_in_for_omitted",
        "schedule_rule_id_omitted",
        "diagnostics_omitted",
    ] {
        assert!(snapshot.get(flag).is_none(), "{flag}");
    }
    let rows = snapshot["displays"].as_array().unwrap();
    assert!(
        rows.iter().all(|row| row.get("entry_name").is_none()),
        "not lean"
    );
    // With its nulls (each a `,"key":value` on the wire) it would not have fit.
    let null = |key: &str, value: &str| escaped_len(&format!(",\"{key}\":{value}"));
    let nulls = rows.len() * (null("nothing_to_play", "null") + null("entry_name", "null"))
        + null("nothing_to_play", "null")
        + null("allow_web_wallpapers", "false")
        + null("refused", "[]")
        + null("refused_omitted", "0")
        + null("loaded_overrides_sha256", "null")
        + null("overrides_ignored", "null");
    assert!(
        tip.spare < nulls,
        "{} bytes to spare beside {nulls} of nulls",
        tip.spare
    );
}

// -- r13e L6 and N6: past what 0.4.0 answered, only as much goes as must ----------------------

/// The taboo record a band document's playlist `index` carries, as status lists it.
fn band_record(index: usize) -> serde_json::Value {
    serde_json::json!({
        "playlist_id": format!("{index:03}"),
        "playlist": format!("n{index:03}"),
        "entry_id": "e",
        "kind": "still",
        "scene_id": null,
        "reason": format!("band {index:03}"),
        "source": "review",
        "durable": true,
        "observed_config_epoch": 1,
    })
}

/// That no taboo record left out would fit in the room `snapshot` has: listing one more adds
/// it (each as long as `newest`, the first listed, which a band's records all are), a comma
/// after the first, and makes `taboo_entries_omitted` no longer. With none listed, the first
/// would be `newest`.
fn assert_next_record_wouldnt_fit(
    label: &str,
    snapshot: &serde_json::Value,
    wire: usize,
    newest: &serde_json::Value,
) {
    let listed = snapshot["taboo_entries"].as_array().unwrap();
    let omitted = snapshot["taboo_entries_omitted"].as_u64().unwrap();
    if omitted == 0 {
        // Every record is listed: none is left out.
        return;
    }
    if let Some(first) = listed.first() {
        assert_eq!(first, newest, "{label}: the newest record first");
    }
    let comma = usize::from(!listed.is_empty());
    let digits = |count: u64| count.to_string().len();
    let cost = json_wire_len(newest) + comma - (digits(omitted) - digits(omitted - 1));
    assert!(
        MAX_RESPONSE_BYTES - wire < cost,
        "{label}: {} bytes to spare beside a {cost}-byte record left out",
        MAX_RESPONSE_BYTES - wire
    );
}

#[test]
fn past_the_band_only_as_many_rule_ids_go_as_must_and_records_come_back() {
    // 0.4.0 sent nothing for these (EOF): the floor itself is over. At 040b3aa every rule id
    // went, with about 109 KB to spare. Now as few go as must, the detached rows' first; what
    // room is left goes back to the records.
    for corner in [
        band("mirrored", 228, 64),
        band("mirrored", 256, 64),
        band("independent", 224, 64),
        band("independent", 256, 0),
    ] {
        let label = format!("{corner:?}");
        let (snapshot, wire) = band_status(corner);
        let tip = band_sent(&snapshot, wire);
        println!("{label}: {tip:?}");
        let rule = format!("{}R", "\"".repeat(corner.rule_chars - 1));
        assert!(tip.rule_ids >= 64, "{label}: {tip:?}");
        assert!(tip.rule_ids < 128, "{label}: {tip:?}");
        assert_row_units_left_out_wouldnt_fit(&label, &snapshot, wire, &rule);
        assert_next_record_wouldnt_fit(
            &label,
            &snapshot,
            wire,
            &band_record(corner.records.saturating_sub(1)),
        );
    }
}

#[test]
fn past_the_band_only_as_many_errors_go_as_must_and_one_byte_rule_ids_stay() {
    // Every row failing, 0.4.0 again sent nothing. At 040b3aa every rule id and every error
    // went, with 110 to 150 KB to spare. Now the longest go first, which are the errors: the
    // one-byte rule ids stay (review r13e N6), and so does the summary's error.
    for corner in [
        failing_band("mirrored", 54),
        failing_band("mirrored", 252),
        failing_band("independent", 186),
        failing_band("independent", 252),
    ] {
        let label = format!("{corner:?}");
        let (snapshot, wire) = band_status(corner);
        let tip = band_sent(&snapshot, wire);
        println!("{label}: {tip:?}");
        assert_eq!(tip.rule_ids, 128, "{label}: {tip:?}");
        assert!(tip.summary_error, "{label}: {tip:?}");
        assert!(tip.errors > 0, "{label}: {tip:?}");
        assert_eq!(snapshot["diagnostics_omitted"], true, "{label}");
        assert_row_units_left_out_wouldnt_fit(&label, &snapshot, wire, "R");
    }
}

// -- r13f N12: a kind's flag pays for its own last units ---------------------------------------
//
// The rows' rule ids are given back the shortest first while they fit, every row's error being
// out (each longer than an id). The last one out takes its 565 bytes, but giving it back also
// takes `schedule_rule_id_omitted` (34 bytes on the wire) out of the reply. At f127a31 a flag
// was given back only once its kind was whole, so that id stayed out beside 548 bytes of room
// and its own flag, and the app hedged every rule row for it.

/// A configuration path `pad` bytes longer than `/tmp/runtime.toml`, each a plain byte, one on
/// the wire: status echoes it, and the budget doesn't count it.
fn config_path_longer_by(pad: usize) -> PathBuf {
    let mut path = String::from("/tmp/");
    let mut left = pad;
    while left > 0 {
        if left == 1 {
            path.push('p');
            break;
        }
        let run = (left - 1).min(199);
        path.push_str(&"p".repeat(run));
        path.push('/');
        left -= run + 1;
    }
    path.push_str("runtime.toml");
    assert_eq!(path.len(), "/tmp/runtime.toml".len() + pad);
    PathBuf::from(path)
}

#[test]
fn the_last_rule_id_out_comes_back_where_its_own_flag_pays_for_it() {
    let flag = flag_wire_len("schedule_rule_id_omitted");
    let failing = format!("{}E", "\"".repeat(255));
    // The corner: every row failing on BIG's entry (its error, about 1.3 KB, goes before any
    // id), a rule id of `chars` quotes. Measured once, it says where the room it has with every
    // unit out puts the last id `delta` bytes past what it and its flag take: the summary's
    // own error first, then 127 ids, then the last, with `unit - flag + delta` bytes left.
    // The path moves that room byte by byte. Whatever this build's own strings take, some id
    // length puts it within the path's reach.
    let mut chars = 142;
    let (document, rule, base) = loop {
        let toml = format!("{}R", "\\\"".repeat(chars - 1));
        let rule = format!("{}R", "\"".repeat(chars - 1));
        let document = at_the_budgets_edge(|still| floor_corner("mirrored", &toml, still));
        let (snapshot, wire, written) = floor_status(&document, 64, Some(&failing));
        assert!(written.is_ok(), "{chars}: {wire} bytes: no reply");
        let summary = row_unit_len(snapshot["last_error"].as_str().unwrap()) as i64;
        let unit = row_unit_len(&rule) as i64;
        // The path's length at which the last id is `delta` bytes past fitting with its flag.
        let base = room_with_row_units_out(&snapshot, wire) - summary - 128 * unit + flag as i64;
        println!(
            "{chars}-quote rule id: {unit} bytes each, the path at {base} puts the last id at its flag's edge"
        );
        if base < flag as i64 + 1 {
            chars -= 1;
        } else if base > 3_000 {
            chars += 1;
        } else {
            break (document, rule, base);
        }
        assert!(
            (100..=256).contains(&chars),
            "no rule id puts the window within reach"
        );
    };
    let unit = row_unit_len(&rule);
    for (delta, back) in [
        (0, true),
        (flag as i64 - 1, true),
        (-1, false),
        (flag as i64, true),
    ] {
        let label = format!(
            "{} bytes of room before the last id",
            unit as i64 - flag as i64 + delta
        );
        let path = config_path_longer_by(usize::try_from(base - delta).unwrap());
        let (snapshot, wire, written) = floor_status_at(&document, 64, Some(&failing), &path);
        assert!(written.is_ok(), "{label}: {wire} bytes: no reply");
        assert_inventory_whole(&snapshot, 128, &label);
        let rows = snapshot["displays"].as_array().unwrap();
        let ids_out = rows
            .iter()
            .filter(|row| row["schedule_rule_id"].is_null())
            .count();
        let errors_out = rows.iter().filter(|row| row["last_error"] == "").count();
        println!(
            "{label}: {ids_out} ids out, {errors_out} errors out, {} bytes to spare",
            MAX_RESPONSE_BYTES - wire
        );
        assert_eq!(errors_out, 128, "{label}: an error came back");
        assert_eq!(snapshot["diagnostics_omitted"], true, "{label}");
        assert!(
            snapshot["last_error"]
                .as_str()
                .unwrap()
                .contains("is borked"),
            "{label}: the summary's own error is the last to go"
        );
        if back {
            assert_eq!(
                ids_out, 0,
                "{label}: a {unit}-byte rule id left out (and schedule_rule_id_omitted set), \
                 though it fits once its {flag}-byte flag is gone"
            );
            assert!(
                snapshot.get("schedule_rule_id_omitted").is_none(),
                "{label}"
            );
            // Measured, not tried: the reply has exactly the room that was left.
            assert_eq!(
                MAX_RESPONSE_BYTES - wire,
                usize::try_from(delta).unwrap(),
                "{label}"
            );
        } else {
            // The control: the id and its flag together are a byte over.
            assert_eq!(ids_out, 1, "{label}");
            assert_eq!(snapshot["schedule_rule_id_omitted"], true, "{label}");
            assert_eq!(MAX_RESPONSE_BYTES - wire, unit - flag - 1, "{label}");
        }
        assert_row_units_left_out_wouldnt_fit(&label, &snapshot, wire, &rule);
    }
}

// -- r13e N9: in mirrored mode a rule naming the default reaches displays assigned elsewhere ---
//
// DP-1 is assigned `mixed`, HDMI-A-1 `all-media` (the default). A rule `eve` names `all-media`
// from 16:00 to 17:00. In mirrored mode a rule in force plays its playlist on every display, so
// at 16:00 DP-1 changes to all-media, and at 17:00 back to mixed. The playlist in force is the
// default either way, so the tick took it for a change of provenance only and applied nothing:
// DP-1's row said all-media while its screen showed mixed, and the other way round after 17:00.

fn default_rule_document(rule_playlist: &str) -> String {
    let mut document = String::from(
        r#"schema_version = 4
config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
default_playlist = "all-media"
[settings]
cycle_interval_seconds = 300
cycle_enabled = false
shuffle = false
dynamics_enabled = false
display_mode = "mirrored"
theme_source_connector = ""
[renderer]
noctalia_program = "/bin/true"
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
"#,
    );
    for (playlist, entries) in [("all-media", 2), ("mixed", 2), ("pages", 1)] {
        document.push_str(&format!(
            "[[playlists]]\nid = \"{playlist}\"\nname = \"{playlist}\"\n"
        ));
        for index in 1..=entries {
            document.push_str(&format!(
                "[[playlists.entries]]\nid = \"{playlist}-{index}\"\nkind = \"still\"\n\
                 still = \"/stills/{playlist}-{index}.png\"\n\
                 palette = {{ kind = \"keep\", mode = \"keep\" }}\n"
            ));
        }
    }
    document.push_str(&format!(
        "[[schedules]]\nid = \"eve\"\nplaylist = \"{rule_playlist}\"\nstart = \"16:00\"\n\
         end = \"17:00\"\n[[displays]]\nconnector = \"DP-1\"\nplaylist = \"mixed\"\n\
         [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"all-media\"\n"
    ));
    document
}

fn on_the_third(hour: u32, minute: u32) -> chrono::NaiveDateTime {
    NaiveDate::from_ymd_opt(2026, 8, 3)
        .unwrap()
        .and_hms_opt(hour, minute, 0)
        .unwrap()
}

/// What each output was last given, by the driver's own log.
fn screen_of(state: &Arc<Mutex<RuntimeDriverState>>) -> std::collections::BTreeMap<String, String> {
    let state = state.lock().unwrap();
    state
        .applied_outputs
        .iter()
        .cloned()
        .zip(state.applies.iter().map(|(entry, _)| entry.clone()))
        .collect()
}

/// Where a display's row and its screen disagree on the entry it shows.
fn rows_off_screen(
    label: &str,
    runtime: &mut Runtime<RuntimeDriver>,
    state: &Arc<Mutex<RuntimeDriverState>>,
    at: chrono::NaiveDateTime,
) -> Vec<String> {
    let snapshot = status(runtime, at);
    let screen = screen_of(state);
    ["DP-1", "HDMI-A-1"]
        .into_iter()
        .filter_map(|connector| {
            let row = route(&snapshot, connector);
            (screen.get(connector).map(String::as_str) != row["entry_id"].as_str()).then(|| {
                format!(
                    "{label}: {connector}'s row says {} ({} by {}), the screen has {:?}",
                    row["entry_id"],
                    row["playlist_id"],
                    row["route_source"],
                    screen.get(connector)
                )
            })
        })
        .collect()
}

fn default_rule_runtime(
    document: String,
    refused: &[&str],
    start: chrono::NaiveDateTime,
) -> (Runtime<RuntimeDriver>, Arc<Mutex<RuntimeDriverState>>) {
    let parsed = Config::from_bytes(document.into_bytes()).unwrap();
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        refuse_when_asked: refused.iter().map(|entry| entry.to_string()).collect(),
        ..RuntimeDriverState::default()
    }));
    let mut runtime = Runtime::new(
        PathBuf::from("/tmp/runtime.toml"),
        parsed,
        RuntimeDriver(state.clone()),
        start,
    )
    .unwrap();
    runtime.apply_current().unwrap();
    (runtime, state)
}

#[test]
fn a_mirrored_rule_naming_the_default_reaches_displays_assigned_elsewhere() {
    let mut problems = Vec::new();
    // Into the rule: started at 15:00, ticked at 16:00.
    let (mut runtime, state) =
        default_rule_runtime(default_rule_document("all-media"), &[], on_the_third(15, 0));
    problems.extend(rows_off_screen(
        "15:00",
        &mut runtime,
        &state,
        on_the_third(15, 0),
    ));
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    problems.extend(rows_off_screen(
        "16:00",
        &mut runtime,
        &state,
        on_the_third(16, 0),
    ));
    assert_eq!(
        screen_of(&state).get("DP-1").map(String::as_str),
        Some("all-media-1"),
        "the rule's playlist is on DP-1"
    );
    // Out of it: started at 16:30 inside it, ticked at 17:00 and 17:30.
    let (mut runtime, state) = default_rule_runtime(
        default_rule_document("all-media"),
        &[],
        on_the_third(16, 30),
    );
    problems.extend(rows_off_screen(
        "16:30 start",
        &mut runtime,
        &state,
        on_the_third(16, 30),
    ));
    runtime.tick(on_the_third(17, 0), Instant::now() + Duration::from_secs(1));
    problems.extend(rows_off_screen(
        "17:00",
        &mut runtime,
        &state,
        on_the_third(17, 0),
    ));
    runtime.tick(
        on_the_third(17, 30),
        Instant::now() + Duration::from_secs(2),
    );
    problems.extend(rows_off_screen(
        "17:30",
        &mut runtime,
        &state,
        on_the_third(17, 30),
    ));
    assert!(problems.is_empty(), "{problems:#?}");
    assert_eq!(
        screen_of(&state).get("DP-1").map(String::as_str),
        Some("mixed-1"),
        "DP-1's own playlist is back"
    );
}

#[test]
fn a_mirrored_rule_whose_stand_in_is_the_default_reaches_displays_assigned_elsewhere() {
    // The same through a stand-in: the rule names `pages`, whose one entry (a scene) the
    // driver refuses, so the default (all-media) stands in for it; what plays is the default
    // either way, and DP-1, on `mixed`, has to move all the same.
    let document = default_rule_document("pages")
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "id = \"pages-1\"\nkind = \"still\"\n",
            "id = \"pages-1\"\nkind = \"scene\"\nscene_id = \"3000000001\"\n",
        );
    let (mut runtime, state) = default_rule_runtime(document, &["pages-1"], on_the_third(15, 0));
    let mut problems = rows_off_screen("15:00", &mut runtime, &state, on_the_third(15, 0));
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    let snapshot = status(&mut runtime, on_the_third(16, 0));
    assert_eq!(route(&snapshot, "DP-1")["stand_in_for"], "pages");
    problems.extend(rows_off_screen(
        "16:00",
        &mut runtime,
        &state,
        on_the_third(16, 0),
    ));
    assert!(problems.is_empty(), "{problems:#?}");
}

#[test]
fn control_a_mirrored_rule_naming_another_playlist_reaches_displays_assigned_elsewhere() {
    // A rule naming `pages`, which plays: the playlist in force changes, which was always
    // applied.
    let (mut runtime, state) =
        default_rule_runtime(default_rule_document("pages"), &[], on_the_third(15, 0));
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    let mut problems = rows_off_screen("16:00", &mut runtime, &state, on_the_third(16, 0));
    runtime.tick(on_the_third(17, 0), Instant::now() + Duration::from_secs(2));
    problems.extend(rows_off_screen(
        "17:00",
        &mut runtime,
        &state,
        on_the_third(17, 0),
    ));
    assert!(problems.is_empty(), "{problems:#?}");
}

#[test]
fn a_mirrored_rule_naming_the_default_that_changes_nothing_on_screen_applies_nothing() {
    // The other side of N9: where every display already shows the default (DP-1 assigned to it
    // too), the rule coming into force and going out changes only what decides; nothing is
    // applied again.
    let document = default_rule_document("all-media").replace(
        "connector = \"DP-1\"\nplaylist = \"mixed\"",
        "connector = \"DP-1\"\nplaylist = \"all-media\"",
    );
    let (mut runtime, state) = default_rule_runtime(document, &[], on_the_third(15, 0));
    let applies = state.lock().unwrap().applies.len();
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    let snapshot = status(&mut runtime, on_the_third(16, 0));
    assert_eq!(route(&snapshot, "DP-1")["schedule_rule_id"], "eve");
    runtime.tick(on_the_third(17, 0), Instant::now() + Duration::from_secs(2));
    let snapshot = status(&mut runtime, on_the_third(17, 0));
    assert!(route(&snapshot, "DP-1")["schedule_rule_id"].is_null());
    assert_eq!(
        state.lock().unwrap().applies.len(),
        applies,
        "applied again"
    );
}

// -- A mirrored hand-over applies every display -------------------------------------------------
//
// Applying a mirrored display is break before make: its renderer stops and starts again. A
// hand-over, a rotation step, a command, a reload or a hotplug applies every mirrored display,
// as 0.4.0 does (r13f N13's keeping was taken out again: review r13g R-L1 and R-L2).

/// The outputs the driver applied (stopped and started) since `before` applies.
fn applied_since(state: &Arc<Mutex<RuntimeDriverState>>, before: usize) -> Vec<String> {
    state.lock().unwrap().applied_outputs[before..].to_vec()
}

fn applies_so_far(state: &Arc<Mutex<RuntimeDriverState>>) -> usize {
    state.lock().unwrap().applied_outputs.len()
}

/// A real driver over `default_rule_document` with videos and motion on, HDMI-A-1 assigned
/// pages (one entry): a Noctalia stand-in that keeps what each output shows in
/// `root/shell/<output>`, as `msg wallpaper-set <output> <path>` sets it, and an mpvpaper
/// stand-in that logs each launch on a line of `root/launches` and keeps running. The runtime,
/// the document's path, and those two places.
fn shell_runtime(label: &str) -> (Runtime<SystemDriver>, PathBuf, PathBuf, PathBuf) {
    let root = directory(label);
    let shell = root.join("shell");
    fs::create_dir_all(&shell).unwrap();
    let noctalia = root.join("noctalia");
    script(
        &noctalia,
        &format!(
            "if [ \"$1\" = msg ] && [ \"$2\" = wallpaper-set ] && [ $# -eq 4 ]; then \
             printf '%s' \"$4\" > {:?}/\"$3\"; fi",
            shell
        ),
    );
    let launches = root.join("launches");
    let mpvpaper = root.join("mpvpaper");
    script(
        &mpvpaper,
        &format!("printf '%s\\n' \"$*\" >> {launches:?}\nexec sleep 30"),
    );
    let document = default_rule_document("all-media")
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "kind = \"still\"\n",
            "kind = \"video\"\nmotion = \"/motion.mp4\"\n",
        )
        .replace(
            "noctalia_program = \"/bin/true\"",
            &format!("noctalia_program = {:?}", noctalia.display().to_string()),
        )
        .replace(
            "mpvpaper_program = \"/bin/true\"",
            &format!("mpvpaper_program = {:?}", mpvpaper.display().to_string()),
        )
        .replace(
            "connector = \"HDMI-A-1\"\nplaylist = \"all-media\"",
            "connector = \"HDMI-A-1\"\nplaylist = \"pages\"",
        );
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, &document).unwrap();
    let parsed = Config::load(&config_path).unwrap();
    let source = SwitchableOutputs::default();
    source.set(vec![live("DP-1", None), live("HDMI-A-1", None)]);
    let driver = SystemDriver::with_output_source(
        parsed.renderer.clone(),
        Path::new("/wall-in-one-runtime.sock"),
        Box::new(source),
    );
    let mut runtime = Runtime::new(config_path.clone(), parsed, driver, noon()).unwrap();
    let applied = without_text_file_busy(|| runtime.apply_current());
    assert!(applied.is_ok(), "{applied:?}");
    (runtime, config_path, shell, launches)
}

/// What the Noctalia stand-in shows on `output`, if anything.
fn shown_by_the_shell(shell: &Path, output: &str) -> Option<String> {
    fs::read_to_string(shell.join(output)).ok()
}

/// The outputs mpvpaper was launched on, in order, once `count` launches are logged.
fn launched_on(launches: &Path, count: usize) -> Vec<String> {
    recorded_lines(launches, count)
        .lines()
        .map(|line| {
            ["DP-1", "HDMI-A-1"]
                .into_iter()
                .find(|output| line.split(' ').any(|word| word == *output))
                .unwrap_or("?")
                .to_string()
        })
        .collect()
}

/// The shell loses what every output shows: restarted with its state gone, or another
/// program's wallpaper set meanwhile.
fn the_shell_forgets(shell: &Path) {
    for output in ["DP-1", "HDMI-A-1"] {
        let _ = fs::remove_file(shell.join(output));
    }
}

#[test]
fn control_a_mirrored_display_a_command_moves_is_handed_its_new_still() {
    // DP-1, which `next` moves, is applied whole: the shell shows its new still however much
    // it lost, and its mpvpaper starts again.
    let (mut runtime, config_path, shell, launches) = shell_runtime("r13g-moved-still");
    launched_on(&launches, 2);
    assert_eq!(
        shown_by_the_shell(&shell, "DP-1"),
        Some("/stills/mixed-1.png".to_string())
    );
    the_shell_forgets(&shell);
    let reply = runtime_command(&mut runtime, noon(), "next", None);
    assert!(reply.ok, "{}", reply.message);
    assert_eq!(
        shown_by_the_shell(&shell, "DP-1"),
        Some("/stills/mixed-2.png".to_string())
    );
    assert_eq!(launched_on(&launches, 3)[2], "DP-1");
    runtime.shutdown();
    fs::remove_dir_all(config_path.parent().unwrap()).unwrap();
}

#[test]
fn control_a_mirrored_display_whose_renderer_failed_is_started_again_at_a_boundary() {
    // A renderer that exited since the last apply: the hand-over applies every display,
    // HDMI-A-1 too, as Play would, and the failure is cleared.
    let (mut runtime, state) =
        default_rule_runtime(default_rule_document("all-media"), &[], on_the_third(15, 0));
    state
        .lock()
        .unwrap()
        .failures
        .push("HDMI-A-1: the motion renderer exited".into());
    runtime.tick(
        on_the_third(15, 30),
        Instant::now() + Duration::from_secs(1),
    );
    assert_eq!(
        status(&mut runtime, on_the_third(15, 30))["renderer_failed"],
        true
    );
    let before = applies_so_far(&state);
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(2));
    let mut applied = applied_since(&state, before);
    applied.sort();
    assert_eq!(applied, ["DP-1", "HDMI-A-1"]);
    let snapshot = status(&mut runtime, on_the_third(16, 0));
    assert_eq!(snapshot["renderer_failed"], false);
    let problems = rows_off_screen("16:00", &mut runtime, &state, on_the_third(16, 0));
    assert!(problems.is_empty(), "{problems:#?}");
}

#[test]
fn control_a_mirrored_startup_retry_after_a_taboo_applies_the_entry_it_moves_to() {
    // The startup retry has no baseline of its own (it is its own candidate), and its displays
    // failed: after three failed attempts on DP-1's entry it is marked taboo, the cursor moves,
    // and the next entry is applied.
    let (mut runtime, state) = {
        let parsed = Config::from_bytes(default_rule_document("all-media").into_bytes()).unwrap();
        let state = Arc::new(Mutex::new(RuntimeDriverState {
            connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
            fail_entry_id: Some("mixed-1".into()),
            fail_entry_attempts_remaining: 3,
            ..RuntimeDriverState::default()
        }));
        let runtime = Runtime::new(
            PathBuf::from("/tmp/runtime.toml"),
            parsed,
            RuntimeDriver(state.clone()),
            on_the_third(12, 0),
        )
        .unwrap();
        (runtime, state)
    };
    let at = on_the_third(12, 0);
    let started = Instant::now();
    assert!(runtime.apply_current().is_err());
    assert!(runtime.schedule_initial_apply_retry(started));
    for seconds in [2, 4, 6] {
        runtime.tick(at, started + Duration::from_secs(seconds));
    }
    let snapshot = status(&mut runtime, at);
    assert_eq!(snapshot["taboo_entries"][0]["entry_id"], "mixed-1");
    assert_eq!(snapshot["renderer_failed"], false, "{snapshot}");
    assert_eq!(
        screen_of(&state).get("DP-1").map(String::as_str),
        Some("mixed-2")
    );
    let problems = rows_off_screen("12:00", &mut runtime, &state, at);
    assert!(problems.is_empty(), "{problems:#?}");
}

/// The r13e sequence's document: rules noon (pages), aft-a and aft-b (mixed, back to back),
/// eve (the default, all-media) and late, with DP-1 assigned mixed and HDMI-A-1 all-media.
fn mirrored_sequence_document(generation: char, rename_second: bool) -> String {
    let rules = format!(
        "[[schedules]]\nid = \"noon\"\nplaylist = \"pages\"\nstart = \"11:00\"\nend = \"13:00\"\n\
         [[schedules]]\nid = \"aft-a\"\nplaylist = \"mixed\"\nstart = \"13:00\"\nend = \"14:00\"\n\
         [[schedules]]\nid = \"{}\"\nplaylist = \"mixed\"\nstart = \"14:00\"\nend = \"15:00\"\n\
         [[schedules]]\nid = \"eve\"\nplaylist = \"all-media\"\nstart = \"16:00\"\nend = \"17:00\"\n\
         [[schedules]]\nid = \"late\"\nplaylist = \"late\"\nstart = \"18:00\"\nend = \"19:00\"\n",
        if rename_second { "aft-c" } else { "aft-b" }
    );
    skip_document(
        "all-media",
        &[
            ("all-media", &["", ""]),
            ("mixed", &["", ""]),
            ("pages", &["", ""]),
            ("late", &[""]),
        ],
        &format!(
            "{rules}[[displays]]\nconnector = \"DP-1\"\nplaylist = \"mixed\"\n\
             [[displays]]\nconnector = \"HDMI-A-1\"\nplaylist = \"all-media\"\n"
        ),
    )
    .replace(
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        &generation.to_string().repeat(64),
    )
}

#[test]
fn a_mirrored_sequence_keeps_every_row_and_its_screen_on_one_decision() {
    // Review r13e T6, 15 steps: start inside a rule, a hotplug, a pick, Follow schedule, a
    // reload while picked and one inside the rule, ticks across boundaries (a hand-over to a
    // rule of the same playlist, named and not applied again), a reload renaming the rule in
    // force, the default's own rule (N9: 16:00 and 17:00), and a failing transition, whose
    // rows show the baseline while it retries.
    let root = directory("r13e-n3-sequence");
    let config_path = root.join("runtime.toml");
    fs::write(&config_path, mirrored_sequence_document('a', false)).unwrap();
    let day = on_the_third;
    let state = Arc::new(Mutex::new(RuntimeDriverState {
        connected_outputs: Some(vec!["DP-1".into(), "HDMI-A-1".into()]),
        ..RuntimeDriverState::default()
    }));
    let parsed = Config::load(&config_path).unwrap();
    let mut runtime = Runtime::new(
        config_path.clone(),
        parsed,
        RuntimeDriver(state.clone()),
        day(11, 30),
    )
    .unwrap();
    runtime.apply_current().unwrap();
    let mut clock = Instant::now();
    let mut problems: Vec<String> = Vec::new();
    let step = |label: &str,
                runtime: &mut Runtime<RuntimeDriver>,
                at: chrono::NaiveDateTime,
                expected: &[(&str, &str, &str, &str)],
                screen_should_follow: bool|
     -> Vec<String> {
        let mut problems = Vec::new();
        let snapshot = status(runtime, at);
        let screen = screen_of(&state);
        for (connector, source, rule, playlist) in expected {
            let row = route(&snapshot, connector);
            let said = (
                row["route_source"].as_str().unwrap(),
                row["schedule_rule_id"].as_str().unwrap_or("null"),
                row["playlist_id"].as_str().unwrap(),
            );
            if said != (*source, *rule, *playlist) {
                problems.push(format!(
                    "{label}: {connector} says {said:?}, the decision is {source}/{rule}/{playlist}"
                ));
            }
            if screen_should_follow
                && screen.get(*connector).map(String::as_str) != row["entry_id"].as_str()
            {
                problems.push(format!(
                    "{label}: {connector}'s row names entry {} but the screen has {:?}",
                    row["entry_id"],
                    screen.get(*connector)
                ));
            }
        }
        problems
    };
    let both = |source: &'static str, rule: &'static str, playlist: &'static str| {
        [
            ("DP-1", source, rule, playlist),
            ("HDMI-A-1", source, rule, playlist),
        ]
    };
    let assignments = [
        ("DP-1", "assignment", "null", "mixed"),
        ("HDMI-A-1", "assignment", "null", "all-media"),
    ];
    let start = both("schedule", "noon", "pages");
    problems.extend(step("start 11:30", &mut runtime, day(11, 30), &start, true));
    // Hotplug inside the rule.
    state.lock().unwrap().connected_outputs =
        Some(vec!["DP-1".into(), "DP-2".into(), "HDMI-A-1".into()]);
    clock += Duration::from_secs(6);
    runtime.tick(day(11, 31), clock);
    let hotplugged = [
        ("DP-2", "schedule", "noon", "pages"),
        ("DP-1", "schedule", "noon", "pages"),
    ];
    problems.extend(step(
        "hotplug 11:31",
        &mut runtime,
        day(11, 31),
        &hotplugged,
        true,
    ));
    assert!(runtime_command(&mut runtime, day(11, 32), "playlist-use", Some("mixed")).ok);
    let picked = both("manual", "null", "mixed");
    problems.extend(step("pick 11:32", &mut runtime, day(11, 32), &picked, true));
    assert!(runtime_command(&mut runtime, day(11, 33), "schedule-follow", None).ok);
    problems.extend(step(
        "follow 11:33",
        &mut runtime,
        day(11, 33),
        &start,
        true,
    ));
    assert!(runtime_command(&mut runtime, day(11, 34), "playlist-use", Some("mixed")).ok);
    fs::write(&config_path, mirrored_sequence_document('b', false)).unwrap();
    let reply = runtime_command(&mut runtime, day(11, 35), "reload", None);
    assert!(reply.ok, "{}", reply.message);
    problems.extend(step(
        "reload picked 11:35",
        &mut runtime,
        day(11, 35),
        &picked,
        true,
    ));
    assert!(runtime_command(&mut runtime, day(11, 36), "schedule-follow", None).ok);
    fs::write(&config_path, mirrored_sequence_document('c', false)).unwrap();
    assert!(runtime_command(&mut runtime, day(11, 37), "reload", None).ok);
    problems.extend(step(
        "reload in noon 11:37",
        &mut runtime,
        day(11, 37),
        &start,
        true,
    ));
    clock += Duration::from_secs(1);
    runtime.tick(day(13, 0), clock);
    let aft_a = both("schedule", "aft-a", "mixed");
    problems.extend(step("tick 13:00", &mut runtime, day(13, 0), &aft_a, true));
    let applies = state.lock().unwrap().applies.len();
    clock += Duration::from_secs(1);
    runtime.tick(day(14, 0), clock);
    let aft_b = both("schedule", "aft-b", "mixed");
    problems.extend(step("tick 14:00", &mut runtime, day(14, 0), &aft_b, true));
    if state.lock().unwrap().applies.len() != applies {
        problems.push("14:00: a hand-over to a rule of the same playlist applied again".into());
    }
    fs::write(&config_path, mirrored_sequence_document('d', true)).unwrap();
    assert!(runtime_command(&mut runtime, day(14, 30), "reload", None).ok);
    let aft_c = both("schedule", "aft-c", "mixed");
    problems.extend(step(
        "reload rename 14:30",
        &mut runtime,
        day(14, 30),
        &aft_c,
        true,
    ));
    if state.lock().unwrap().applies.len() != applies {
        problems.push("14:30: a reload renaming the rule in force applied again".into());
    }
    for (label, at, expected) in [
        ("tick 15:00", day(15, 0), &assignments),
        (
            "tick 16:00 (the default's rule)",
            day(16, 0),
            &both("schedule", "eve", "all-media"),
        ),
        ("tick 17:00", day(17, 0), &assignments),
    ] {
        clock += Duration::from_secs(1);
        runtime.tick(at, clock);
        problems.extend(step(label, &mut runtime, at, expected, true));
    }
    // A transition that fails: the rows say the baseline while it retries.
    {
        let mut recorded = state.lock().unwrap();
        recorded.fail_entry_id = Some("late-1".into());
        recorded.fail_entry_attempts_remaining = usize::MAX;
    }
    for attempt in 1..=3 {
        clock += Duration::from_secs(3);
        runtime.tick(day(18, 0), clock);
        let label = format!("tick 18:00, attempt {attempt}");
        problems.extend(step(&label, &mut runtime, day(18, 0), &assignments, false));
    }
    // `late`'s one entry is taboo now: nothing of it can play, the baseline stays.
    clock += Duration::from_secs(3);
    runtime.tick(day(18, 1), clock);
    problems.extend(step(
        "tick 18:01",
        &mut runtime,
        day(18, 1),
        &assignments,
        true,
    ));
    state.lock().unwrap().fail_entry_id = None;
    clock += Duration::from_secs(3);
    runtime.tick(day(19, 0), clock);
    problems.extend(step(
        "tick 19:00",
        &mut runtime,
        day(19, 0),
        &assignments,
        true,
    ));
    fs::remove_dir_all(&root).unwrap();
    assert!(problems.is_empty(), "{problems:#?}");
}

// -- review r13g R-L1 and R-L2: a mirrored display shows what its row says ---------------------
//
// At 2a17a02 a mirrored display kept its renderer where the runtime inferred, from what it knew
// then, that its target hadn't changed. A refusal learned at a rule's boundary (R-L1), or the
// second apply round after a refusal at the start (R-L2), then kept a display on a renderer
// its row didn't name, or on none, and blamed a good item for another's refusal. Every display
// is applied again, as at f127a31; these are the review's probes.

/// `default_rule_document(rule)` with motion on, DP-1 assigned `dp1` and HDMI-A-1 `hdmi`, and
/// pages-1 a Workshop scene, which the driver refuses when asked (as a real one does for an
/// item that became a web page, or was unsubscribed, since the document was compiled).
fn refusable_pages_document(rule: &str, dp1: &str, hdmi: &str) -> String {
    default_rule_document(rule)
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "id = \"pages-1\"\nkind = \"still\"\n",
            "id = \"pages-1\"\nkind = \"scene\"\nscene_id = \"3000000001\"\n",
        )
        .replace(
            "connector = \"DP-1\"\nplaylist = \"mixed\"",
            &format!("connector = \"DP-1\"\nplaylist = \"{dp1}\""),
        )
        .replace(
            "connector = \"HDMI-A-1\"\nplaylist = \"all-media\"",
            &format!("connector = \"HDMI-A-1\"\nplaylist = \"{hdmi}\""),
        )
}

/// R-L1: DP-1 plays pages (pages-1, a scene), HDMI-A-1 mixed; rule eve puts pages on every
/// display at 16:00. When pages-1 is refused before then, the boundary's ask-ahead learns it,
/// and pages plays its stand-in, the default. What the boundary found wrong at 16:00, what it
/// applied, and whether DP-1 still runs motion.
fn refused_ahead_of_a_boundary(refuse: bool) -> (Vec<String>, Vec<String>, bool) {
    let (mut runtime, state) = default_rule_runtime(
        refusable_pages_document("pages", "pages", "mixed"),
        &[],
        on_the_third(15, 0),
    );
    let mut problems = rows_off_screen("15:00", &mut runtime, &state, on_the_third(15, 0));
    assert!(state.lock().unwrap().active_outputs.contains("DP-1"));
    if refuse {
        state
            .lock()
            .unwrap()
            .refuse_when_asked
            .insert("pages-1".into());
    }
    let before = applies_so_far(&state);
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    problems.extend(rows_off_screen(
        "16:00",
        &mut runtime,
        &state,
        on_the_third(16, 0),
    ));
    let applied = applied_since(&state, before);
    let motion = state.lock().unwrap().active_outputs.contains("DP-1");
    (problems, applied, motion)
}

#[test]
fn a_display_whose_entry_is_refused_ahead_of_a_boundary_is_not_kept_on_its_old_renderer() {
    let (problems, applied, motion) = refused_ahead_of_a_boundary(true);
    assert!(problems.is_empty(), "{problems:#?}");
    assert!(applied.contains(&"DP-1".to_string()), "{applied:?}");
    assert!(!motion, "pages-1's motion still runs on DP-1");
}

#[test]
fn control_without_the_refusal_dp1_plays_pages_1_through_the_boundary() {
    let (problems, _, motion) = refused_ahead_of_a_boundary(false);
    assert!(problems.is_empty(), "{problems:#?}");
    assert!(motion);
}

#[test]
fn a_display_refused_ahead_of_a_boundary_is_not_kept_on_its_old_renderer_when_the_rule_ends() {
    // R-L1, an hour on: at 17:00 eve ends, DP-1's own playlist (pages) is again its stand-in.
    let (mut runtime, state) = default_rule_runtime(
        refusable_pages_document("pages", "pages", "mixed"),
        &[],
        on_the_third(15, 0),
    );
    state
        .lock()
        .unwrap()
        .refuse_when_asked
        .insert("pages-1".into());
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    let before = applies_so_far(&state);
    runtime.tick(on_the_third(17, 0), Instant::now() + Duration::from_secs(2));
    let applied = applied_since(&state, before);
    let problems = rows_off_screen("17:00", &mut runtime, &state, on_the_third(17, 0));
    assert!(
        problems.is_empty(),
        "17:00 applied {applied:?}: {problems:#?}"
    );
    assert!(!state.lock().unwrap().active_outputs.contains("DP-1"));
}

/// DP-1 plays pages (pages-1, a scene), HDMI-A-1 the default; rule eve puts pages on every
/// display at 16:00, and a pick of the default follows at 16:05. What was found wrong at the
/// boundary, and after the pick, and whether DP-1 still runs motion then.
fn refused_at_a_boundary_then_a_pick(refuse: bool) -> (Vec<String>, Vec<String>, bool) {
    let (mut runtime, state) = default_rule_runtime(
        refusable_pages_document("pages", "pages", "all-media"),
        &[],
        on_the_third(15, 0),
    );
    if refuse {
        state
            .lock()
            .unwrap()
            .refuse_when_asked
            .insert("pages-1".into());
    }
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    let at_boundary = rows_off_screen("16:00", &mut runtime, &state, on_the_third(16, 0));
    let reply = runtime_command(
        &mut runtime,
        on_the_third(16, 5),
        "playlist-use",
        Some("all-media"),
    );
    assert!(reply.ok, "{}", reply.message);
    let after_pick = rows_off_screen("pick", &mut runtime, &state, on_the_third(16, 5));
    let motion = state.lock().unwrap().active_outputs.contains("DP-1");
    (at_boundary, after_pick, motion)
}

#[test]
fn a_pick_after_a_boundary_that_learned_a_refusal_moves_the_display_off_the_refused_entry() {
    let (_, after_pick, motion) = refused_at_a_boundary_then_a_pick(true);
    assert!(after_pick.is_empty(), "{after_pick:#?}");
    assert!(!motion, "pages-1's motion still runs on DP-1");
}

#[test]
fn control_without_the_refusal_the_boundary_and_the_pick_move_dp1() {
    let (at_boundary, after_pick, _) = refused_at_a_boundary_then_a_pick(false);
    assert!(at_boundary.is_empty(), "{at_boundary:#?}");
    assert!(after_pick.is_empty(), "{after_pick:#?}");
}

/// R-L2: DP-1 plays mixed (mixed-1, a video, or a scene with `mixed_1_scene`; then mixed-2, a
/// scene), HDMI-A-1 pages (one video). `next` moves mixed to mixed-2, which the driver refuses
/// at its start when `refuse` (its item changed between the ask and the start), so mixed moves
/// back to mixed-1 for a second apply round. What was found wrong, whether DP-1 runs motion,
/// and the status.
fn refused_at_its_start(
    refuse: bool,
    mixed_1_scene: bool,
) -> (Vec<String>, bool, serde_json::Value) {
    let mixed_1 = if mixed_1_scene {
        "id = \"mixed-1\"\nkind = \"scene\"\nscene_id = \"3000000011\"\n"
    } else {
        "id = \"mixed-1\"\nkind = \"video\"\nmotion = \"/motion.mp4\"\n"
    };
    let document = default_rule_document("all-media")
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace("id = \"mixed-1\"\nkind = \"still\"\n", mixed_1)
        .replace(
            "id = \"mixed-2\"\nkind = \"still\"\n",
            "id = \"mixed-2\"\nkind = \"scene\"\nscene_id = \"3000000012\"\n",
        )
        .replace(
            "id = \"pages-1\"\nkind = \"still\"\n",
            "id = \"pages-1\"\nkind = \"video\"\nmotion = \"/motion.mp4\"\n",
        )
        .replace(
            "connector = \"HDMI-A-1\"\nplaylist = \"all-media\"",
            "connector = \"HDMI-A-1\"\nplaylist = \"pages\"",
        );
    let at = on_the_third(12, 0);
    let (mut runtime, state) = default_rule_runtime(document, &[], at);
    assert!(state.lock().unwrap().active_outputs.contains("DP-1"));
    if refuse {
        state
            .lock()
            .unwrap()
            .refuse_entry_ids
            .insert("mixed-2".into());
    }
    let reply = runtime_command(&mut runtime, at, "next", None);
    assert!(reply.ok, "{}", reply.message);
    let problems = rows_off_screen("next", &mut runtime, &state, at);
    let motion = state.lock().unwrap().active_outputs.contains("DP-1");
    (problems, motion, status(&mut runtime, at))
}

#[test]
fn a_display_refused_at_its_start_that_moves_back_to_its_video_is_started_again() {
    let (problems, motion, snapshot) = refused_at_its_start(true, false);
    assert_eq!(route(&snapshot, "DP-1")["entry_id"], "mixed-1");
    assert!(motion, "DP-1 shows mixed-1 with no motion");
    assert!(problems.is_empty(), "{problems:#?}");
}

#[test]
fn a_display_moved_back_is_not_blamed_for_the_refusal_its_first_round_entry_got() {
    let (problems, motion, snapshot) = refused_at_its_start(true, true);
    let refused = snapshot["refused"].to_string();
    assert!(
        !refused.contains("mixed-1") && !refused.contains("3000000011"),
        "mixed-1 recorded as refused with mixed-2's refusal: {refused}"
    );
    assert!(refused.contains("3000000012"), "{refused}");
    assert_eq!(
        route(&snapshot, "DP-1")["entry_id"],
        "mixed-1",
        "{snapshot}"
    );
    assert!(motion, "DP-1 has no motion");
    assert!(problems.is_empty(), "{problems:#?}");
}

#[test]
fn control_without_the_refusal_next_moves_dp1_to_mixed_2() {
    let (problems, motion, snapshot) = refused_at_its_start(false, false);
    assert_eq!(route(&snapshot, "DP-1")["entry_id"], "mixed-2");
    assert!(motion);
    assert!(problems.is_empty(), "{problems:#?}");
}

// -- review r13g R-L3: a boundary that learns the shown entry is refused applies --------------
//
// The ask-ahead at a mirrored rule's boundary can learn that an entry a display shows is
// refused. Before, what every display showed was then judged with that knowledge on both
// sides, before and after, so the boundary could find nothing changed and commit provenance
// only: the display kept the refused entry's renderer. It is applied now, and moves on as any
// apply does: to its playlist's next entry, or, with none, to the default.

#[test]
fn a_boundary_that_learns_the_shown_entry_is_refused_moves_the_display_to_the_default() {
    // DP-1 on pages (one entry, pages-1, refused), HDMI-A-1 on the default; eve names pages.
    // `control_without_the_refusal_the_boundary_and_the_pick_move_dp1` is the control.
    let (at_boundary, _, _) = refused_at_a_boundary_then_a_pick(true);
    assert!(at_boundary.is_empty(), "{at_boundary:#?}");
}

/// Both displays on the default, all-media, whose first entry is a scene that the driver
/// refuses when asked if `refuse`; eve names the default. Each display's row and screen at
/// 16:00, what the boundary applied, and whether DP-1 runs motion.
fn refused_shown_entry_at_the_defaults_rule(
    refuse: bool,
) -> (Vec<String>, serde_json::Value, Vec<String>, bool) {
    let document = default_rule_document("all-media")
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "id = \"all-media-1\"\nkind = \"still\"\n",
            "id = \"all-media-1\"\nkind = \"scene\"\nscene_id = \"3000000021\"\n",
        )
        .replace(
            "connector = \"DP-1\"\nplaylist = \"mixed\"",
            "connector = \"DP-1\"\nplaylist = \"all-media\"",
        );
    let (mut runtime, state) = default_rule_runtime(document, &[], on_the_third(15, 0));
    assert!(state.lock().unwrap().active_outputs.contains("DP-1"));
    if refuse {
        state
            .lock()
            .unwrap()
            .refuse_when_asked
            .insert("all-media-1".into());
    }
    let before = applies_so_far(&state);
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    let problems = rows_off_screen("16:00", &mut runtime, &state, on_the_third(16, 0));
    let motion = state.lock().unwrap().active_outputs.contains("DP-1");
    (
        problems,
        status(&mut runtime, on_the_third(16, 0)),
        applied_since(&state, before),
        motion,
    )
}

#[test]
fn a_boundary_that_learns_the_shown_entry_is_refused_skips_to_the_next_entry() {
    let (problems, snapshot, applied, motion) = refused_shown_entry_at_the_defaults_rule(true);
    assert!(problems.is_empty(), "{problems:#?}");
    for connector in ["DP-1", "HDMI-A-1"] {
        assert_eq!(
            route(&snapshot, connector)["entry_id"],
            "all-media-2",
            "{connector} still shows the refused all-media-1; applied {applied:?}"
        );
    }
    assert!(!motion, "all-media-1's motion still runs on DP-1");
}

#[test]
fn control_without_the_refusal_the_defaults_rule_applies_nothing() {
    let (problems, snapshot, applied, motion) = refused_shown_entry_at_the_defaults_rule(false);
    assert!(problems.is_empty(), "{problems:#?}");
    assert_eq!(route(&snapshot, "DP-1")["entry_id"], "all-media-1");
    assert_eq!(route(&snapshot, "DP-1")["schedule_rule_id"], "eve");
    assert_eq!(applied, Vec::<String>::new());
    assert!(motion);
}

// -- review r13i: R-L3 per independent route ------------------------------------------------
//
// An independent route's ask-ahead at a rule's boundary asks about the rule's playlist. A
// refusal is kept by item, so what it learns can be that the entry the display shows, the same
// item in another playlist, is refused. With the rule's playlist then standing in for by the
// default, which the route already plays, the route kept its cursor on that entry, and the
// boundary changed only what status says: the display kept the refused entry's renderer. It is
// applied now, and moves on as any apply does: to the next entry.

/// Independent DP-1 and HDMI-A-1 on the default, all-media, whose first entry is a scene; eve
/// names pages, whose one entry is a scene the driver refuses when asked: the same item as
/// all-media-1's when `shared`, another otherwise. Each display's row and screen at 16:00, the
/// status, what the boundary applied, and whether DP-1 runs motion.
fn refused_at_an_independent_boundary(
    shared: bool,
) -> (Vec<String>, serde_json::Value, Vec<String>, bool) {
    let pages_1 = if shared { "3000000031" } else { "3000000032" };
    let document = default_rule_document("pages")
        .replace(
            "display_mode = \"mirrored\"",
            "display_mode = \"independent\"",
        )
        .replace(
            "theme_source_connector = \"\"",
            "theme_source_connector = \"DP-1\"",
        )
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "id = \"all-media-1\"\nkind = \"still\"\n",
            "id = \"all-media-1\"\nkind = \"scene\"\nscene_id = \"3000000031\"\n",
        )
        .replace(
            "id = \"pages-1\"\nkind = \"still\"\n",
            &format!("id = \"pages-1\"\nkind = \"scene\"\nscene_id = \"{pages_1}\"\n"),
        )
        .replace(
            "connector = \"DP-1\"\nplaylist = \"mixed\"",
            "connector = \"DP-1\"\nplaylist = \"all-media\"",
        );
    let (mut runtime, state) = default_rule_runtime(document, &[], on_the_third(15, 0));
    assert!(state.lock().unwrap().active_outputs.contains("DP-1"));
    state
        .lock()
        .unwrap()
        .refuse_when_asked
        .insert("pages-1".into());
    let before = applies_so_far(&state);
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    let problems = rows_off_screen("16:00", &mut runtime, &state, on_the_third(16, 0));
    let motion = state.lock().unwrap().active_outputs.contains("DP-1");
    (
        problems,
        status(&mut runtime, on_the_third(16, 0)),
        applied_since(&state, before),
        motion,
    )
}

#[test]
fn an_independent_boundary_that_learns_the_shown_entry_is_refused_skips_to_the_next_entry() {
    let (problems, snapshot, applied, motion) = refused_at_an_independent_boundary(true);
    assert!(problems.is_empty(), "{problems:#?}");
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = route(&snapshot, connector);
        assert_eq!(row["stand_in_for"], "pages", "{row}");
        assert_eq!(
            row["entry_id"], "all-media-2",
            "{connector} still shows the refused all-media-1; applied {applied:?}"
        );
    }
    assert!(!motion, "all-media-1's motion still runs on DP-1");
}

#[test]
fn control_an_independent_boundary_that_learns_another_refusal_applies_nothing() {
    // pages-1 is another item: what the displays show isn't refused, and the stand-in they
    // already play isn't started again.
    let (problems, snapshot, applied, motion) = refused_at_an_independent_boundary(false);
    assert!(problems.is_empty(), "{problems:#?}");
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = route(&snapshot, connector);
        assert_eq!(row["stand_in_for"], "pages", "{row}");
        assert_eq!(row["entry_id"], "all-media-1", "{row}");
        assert_eq!(row["schedule_rule_id"], "eve", "{row}");
    }
    assert_eq!(applied, Vec::<String>::new());
    assert!(motion);
}

// -- review r13i R-N6: a failure after a skip is blamed on the entry the apply put up -------
//
// A mirrored automatic attempt took what it would blame before its apply. When the apply
// stepped over a refused entry and then failed without a failure of its own (the pause), the
// refused entry it never started was blamed, and after three attempts marked taboo; the entry
// the apply put up wasn't named.

/// The taboo entries' ids, and where a row and its screen disagree.
fn taboo_and_rows(
    label: &str,
    runtime: &mut Runtime<RuntimeDriver>,
    state: &Arc<Mutex<RuntimeDriverState>>,
    at: chrono::NaiveDateTime,
) -> (Vec<String>, serde_json::Value, Vec<String>) {
    let problems = rows_off_screen(label, runtime, state, at);
    let snapshot = status(runtime, at);
    let taboo = snapshot["taboo_entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["entry_id"].as_str().unwrap().to_string())
        .collect();
    (taboo, snapshot, problems)
}

/// Mirrored, both displays on the default all-media (all-media-1 a scene), paused; eve names
/// the default. At 16:00 the ask-ahead learns all-media-1 is refused and the apply steps over
/// it to all-media-2. With `fail_pause` the pause can't be set, through three attempts.
fn paused_at_a_mirrored_boundary_after_a_skip(
    fail_pause: bool,
) -> (Vec<String>, serde_json::Value, Vec<String>) {
    let document = default_rule_document("all-media")
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "id = \"all-media-1\"\nkind = \"still\"\n",
            "id = \"all-media-1\"\nkind = \"scene\"\nscene_id = \"3000000021\"\n",
        )
        .replace(
            "connector = \"DP-1\"\nplaylist = \"mixed\"",
            "connector = \"DP-1\"\nplaylist = \"all-media\"",
        );
    let (mut runtime, state) = default_rule_runtime(document, &[], on_the_third(15, 0));
    let reply = runtime_command(&mut runtime, on_the_third(15, 5), "pause", None);
    assert!(reply.ok, "{}", reply.message);
    {
        let mut recorded = state.lock().unwrap();
        recorded.refuse_when_asked.insert("all-media-1".into());
        recorded.fail_pause = fail_pause;
    }
    let start = Instant::now();
    for seconds in [1, 3, 5, 7, 9] {
        runtime.tick(on_the_third(16, 0), start + Duration::from_secs(seconds));
    }
    taboo_and_rows("16:00", &mut runtime, &state, on_the_third(16, 0))
}

#[test]
fn a_pause_failure_after_a_skip_is_blamed_on_the_entry_put_up_not_the_one_skipped() {
    let (taboo, snapshot, _) = paused_at_a_mirrored_boundary_after_a_skip(true);
    assert_eq!(
        taboo,
        vec!["all-media-2".to_string()],
        "all-media-1, refused and never started, blamed for the pause: {}",
        snapshot["taboo_entries"]
    );
}

#[test]
fn control_without_the_pause_failure_the_boundary_skips_and_marks_nothing() {
    let (taboo, snapshot, problems) = paused_at_a_mirrored_boundary_after_a_skip(false);
    assert!(problems.is_empty(), "{problems:#?}");
    assert!(taboo.is_empty(), "{taboo:?}");
    assert_eq!(route(&snapshot, "DP-1")["entry_id"], "all-media-2");
}

/// The path from before the range: a rule ending with assignments asks nothing ahead, so DP-1
/// goes back to mixed with its cursor on mixed-1 (a scene), which the apply finds refused and
/// steps over to mixed-2. With `fail_pause` the pause can't be set, through three attempts.
fn paused_at_a_mirrored_rules_end_after_a_skip(
    fail_pause: bool,
) -> (Vec<String>, serde_json::Value, Vec<String>) {
    let document = default_rule_document("pages")
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "id = \"mixed-1\"\nkind = \"still\"\n",
            "id = \"mixed-1\"\nkind = \"scene\"\nscene_id = \"3000000041\"\n",
        );
    let (mut runtime, state) = default_rule_runtime(document, &[], on_the_third(15, 0));
    runtime.tick(on_the_third(16, 0), Instant::now() + Duration::from_secs(1));
    let reply = runtime_command(&mut runtime, on_the_third(16, 5), "pause", None);
    assert!(reply.ok, "{}", reply.message);
    {
        let mut recorded = state.lock().unwrap();
        recorded.refuse_when_asked.insert("mixed-1".into());
        recorded.fail_pause = fail_pause;
    }
    let start = Instant::now() + Duration::from_secs(2);
    for seconds in [1, 3, 5, 7, 9] {
        runtime.tick(on_the_third(17, 0), start + Duration::from_secs(seconds));
    }
    taboo_and_rows("17:00", &mut runtime, &state, on_the_third(17, 0))
}

#[test]
fn a_pause_failure_after_a_skip_at_a_rules_end_is_blamed_on_the_entries_put_up() {
    let (taboo, snapshot, _) = paused_at_a_mirrored_rules_end_after_a_skip(true);
    assert!(
        !taboo.contains(&"mixed-1".to_string()),
        "mixed-1, refused and never started, blamed for the pause: {}",
        snapshot["taboo_entries"]
    );
    assert!(taboo.contains(&"mixed-2".to_string()), "{taboo:?}");
}

#[test]
fn control_without_the_pause_failure_the_rules_end_skips_and_marks_nothing() {
    let (taboo, snapshot, problems) = paused_at_a_mirrored_rules_end_after_a_skip(false);
    assert!(problems.is_empty(), "{problems:#?}");
    assert!(taboo.is_empty(), "{taboo:?}");
    assert_eq!(route(&snapshot, "DP-1")["entry_id"], "mixed-2");
}

/// Independent DP-1 and HDMI-A-1, DP-1 assigned to all-media: all-media-1 and pages-1 are the
/// scene 3000000031; eve names pages.
fn independent_shared_item_document() -> String {
    default_rule_document("pages")
        .replace(
            "display_mode = \"mirrored\"",
            "display_mode = \"independent\"",
        )
        .replace(
            "theme_source_connector = \"\"",
            "theme_source_connector = \"DP-1\"",
        )
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "id = \"all-media-1\"\nkind = \"still\"\n",
            "id = \"all-media-1\"\nkind = \"scene\"\nscene_id = \"3000000031\"\n",
        )
        .replace(
            "id = \"pages-1\"\nkind = \"still\"\n",
            "id = \"pages-1\"\nkind = \"scene\"\nscene_id = \"3000000031\"\n",
        )
        .replace(
            "connector = \"DP-1\"\nplaylist = \"mixed\"",
            "connector = \"DP-1\"\nplaylist = \"all-media\"",
        )
}

/// The independent twin: DP-1 and HDMI-A-1 on the default all-media, paused, whose all-media-1
/// and all-media-2 are scenes of two items; eve names pages, whose pages-1 is all-media-1's
/// item, refused when asked. At 16:00 each route's ask-ahead learns it is refused and the apply
/// steps over all-media-1 to all-media-2. With `fail_pause` the pause can't be set, through
/// three attempts.
fn paused_at_an_independent_boundary_after_a_skip(
    fail_pause: bool,
) -> (Vec<String>, serde_json::Value, Vec<String>) {
    let document = independent_shared_item_document().replace(
        "id = \"all-media-2\"\nkind = \"still\"\n",
        "id = \"all-media-2\"\nkind = \"scene\"\nscene_id = \"3000000032\"\n",
    );
    let (mut runtime, state) = default_rule_runtime(document, &[], on_the_third(15, 0));
    let reply = runtime_command(&mut runtime, on_the_third(15, 5), "pause", None);
    assert!(reply.ok, "{}", reply.message);
    {
        let mut recorded = state.lock().unwrap();
        recorded.refuse_when_asked.insert("pages-1".into());
        recorded.fail_pause = fail_pause;
    }
    let start = Instant::now();
    for seconds in [1, 3, 5, 7, 9] {
        runtime.tick(on_the_third(16, 0), start + Duration::from_secs(seconds));
    }
    taboo_and_rows("16:00", &mut runtime, &state, on_the_third(16, 0))
}

#[test]
fn an_independent_pause_failure_after_a_skip_is_blamed_on_the_entry_put_up() {
    let (taboo, snapshot, _) = paused_at_an_independent_boundary_after_a_skip(true);
    assert_eq!(
        taboo,
        vec!["all-media-2".to_string()],
        "entries of the refused item, never started, blamed for the pause: {}",
        snapshot["taboo_entries"]
    );
}

#[test]
fn control_without_the_pause_failure_an_independent_boundary_skips_and_marks_nothing() {
    let (taboo, snapshot, problems) = paused_at_an_independent_boundary_after_a_skip(false);
    assert!(problems.is_empty(), "{problems:#?}");
    assert!(taboo.is_empty(), "{taboo:?}");
    for connector in ["DP-1", "HDMI-A-1"] {
        assert_eq!(route(&snapshot, connector)["entry_id"], "all-media-2");
    }
}

// -- review r13l R-L-h: a boundary off a refused entry doesn't restore it -------------------
//
// A boundary applied because the entry a display shows is refused started its move with that
// entry as the baseline to restore. A failed attempt "restored" it by an apply that stepped over
// the refused entry onto the failing one again: the row said the previous wallpaper was
// restored, and after the third failure the display was left on the failing entry, taboo, with
// the refused entry's still on screen and the entry after never tried. It now moves on as
// hotplug and startup do. An ordinary boundary still restores its baseline.

/// Independent DP-1 and HDMI-A-1 on the default all-media, with a third entry; eve names pages,
/// whose pages-1 is all-media-1's item. With `refuse` it is refused when asked, and the boundary
/// moves DP-1 off all-media-1 onto all-media-2; without, onto pages-1. That entry's still fails
/// on DP-1 `failures` times. DP-1's row after the boundary's first attempt and its screen then,
/// the status at +601 s, and where rows and screens disagree then.
fn a_failing_independent_boundary(
    refuse: bool,
    failures: usize,
) -> (
    serde_json::Value,
    Option<String>,
    serde_json::Value,
    Vec<String>,
) {
    let second = "[[playlists.entries]]\nid = \"all-media-2\"\nkind = \"still\"\n\
                  still = \"/stills/all-media-2.png\"\npalette = { kind = \"keep\", mode = \"keep\" }\n";
    let document = independent_shared_item_document().replace(
        second,
        &format!("{second}{}", second.replace("all-media-2", "all-media-3")),
    );
    assert!(
        document.contains("all-media-3"),
        "the premise: a third entry"
    );
    let (mut runtime, state) = default_rule_runtime(document, &[], on_the_third(15, 0));
    assert!(state.lock().unwrap().active_outputs.contains("DP-1"));
    {
        let mut recorded = state.lock().unwrap();
        if refuse {
            recorded.refuse_when_asked.insert("pages-1".into());
        }
        let failing = if refuse { "all-media-2" } else { "pages-1" };
        recorded.fail_stage_entry_id = Some(failing.into());
        recorded.fail_stage_output = Some("DP-1".into());
        recorded.fail_stage_attempts_remaining = failures;
    }
    let at = on_the_third(16, 0);
    let start = Instant::now();
    runtime.tick(at, start + Duration::from_secs(1));
    let first = route(&status(&mut runtime, at), "DP-1");
    let screen = screen_of(&state).get("DP-1").cloned();
    for millis in [3_100, 5_200, 7_300, 9_400, 61_000, 301_000, 601_000] {
        runtime.tick(at, start + Duration::from_millis(millis));
    }
    let problems = rows_off_screen("+601 s", &mut runtime, &state, at);
    (first, screen, status(&mut runtime, at), problems)
}

#[test]
fn an_independent_boundary_off_a_refused_entry_onto_one_that_keeps_failing_moves_on() {
    let (_, _, snapshot, problems) = a_failing_independent_boundary(true, 100);
    assert!(problems.is_empty(), "{problems:#?}");
    let dp1 = route(&snapshot, "DP-1");
    assert_eq!(
        dp1["entry_id"], "all-media-3",
        "all-media-3 never tried: {dp1}"
    );
    assert_eq!(dp1["renderer_failed"], false, "{dp1}");
}

#[test]
fn an_independent_boundary_off_a_refused_entry_isnt_said_to_restore_it() {
    let (first, _, snapshot, problems) = a_failing_independent_boundary(true, 1);
    let error = first["last_error"].as_str().unwrap();
    assert!(
        error.starts_with("automatic schedule attempt 1/3 failed: "),
        "the premise: the boundary's first attempt failed: {first}"
    );
    assert!(
        !error.contains("previous wallpaper restored"),
        "all-media-1, refused, said restored: {first}"
    );
    assert!(
        error.ends_with("; paired still remains the fallback"),
        "{first}"
    );
    assert!(problems.is_empty(), "{problems:#?}");
    assert_eq!(route(&snapshot, "DP-1")["entry_id"], "all-media-2");
}

#[test]
fn control_an_ordinary_independent_boundary_that_fails_restores_its_baseline() {
    let (first, screen, snapshot, problems) = a_failing_independent_boundary(false, 1);
    let error = first["last_error"].as_str().unwrap();
    assert!(
        error.starts_with("automatic schedule attempt 1/3 failed: ")
            && error.ends_with("; previous wallpaper restored"),
        "{first}"
    );
    assert_eq!(first["entry_id"], "all-media-1", "{first}");
    assert_eq!(screen.as_deref(), Some("all-media-1"));
    assert_eq!(first["renderer_failed"], false, "{first}");
    assert!(problems.is_empty(), "{problems:#?}");
    assert_eq!(route(&snapshot, "DP-1")["entry_id"], "pages-1");
}

/// The mirrored twin: both displays on the default all-media, whose all-media-1 is a scene,
/// with a third entry. With `refuse`, eve names the default and all-media-1 is refused when
/// asked, so the boundary moves the displays off it onto all-media-2; without, eve names pages,
/// and the boundary moves them onto pages-1. That entry fails `failures` times. The status after
/// the boundary's first attempt and DP-1's screen then, the status at +601 s, and where rows and
/// screens disagree then.
fn a_failing_mirrored_boundary(
    refuse: bool,
    failures: usize,
) -> (
    serde_json::Value,
    Option<String>,
    serde_json::Value,
    Vec<String>,
) {
    let (rule, failing) = if refuse {
        ("all-media", "all-media-2")
    } else {
        ("pages", "pages-1")
    };
    let second = "[[playlists.entries]]\nid = \"all-media-2\"\nkind = \"still\"\n\
                  still = \"/stills/all-media-2.png\"\npalette = { kind = \"keep\", mode = \"keep\" }\n";
    let document = default_rule_document(rule)
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(
            "id = \"all-media-1\"\nkind = \"still\"\n",
            "id = \"all-media-1\"\nkind = \"scene\"\nscene_id = \"3000000021\"\n",
        )
        .replace(
            second,
            &format!("{second}{}", second.replace("all-media-2", "all-media-3")),
        )
        .replace(
            "connector = \"DP-1\"\nplaylist = \"mixed\"",
            "connector = \"DP-1\"\nplaylist = \"all-media\"",
        );
    assert!(
        document.contains("all-media-3"),
        "the premise: a third entry"
    );
    let (mut runtime, state) = default_rule_runtime(document, &[], on_the_third(15, 0));
    assert!(state.lock().unwrap().active_outputs.contains("DP-1"));
    {
        let mut recorded = state.lock().unwrap();
        if refuse {
            recorded.refuse_when_asked.insert("all-media-1".into());
        }
        // Mirrored applies don't stage per output: fail the entry itself.
        recorded.fail_entry_id = Some(failing.into());
        recorded.fail_entry_attempts_remaining = failures;
    }
    let at = on_the_third(16, 0);
    let start = Instant::now();
    runtime.tick(at, start + Duration::from_secs(1));
    let first = status(&mut runtime, at);
    let screen = screen_of(&state).get("DP-1").cloned();
    for millis in [3_100, 5_200, 7_300, 9_400, 61_000, 301_000, 601_000] {
        runtime.tick(at, start + Duration::from_millis(millis));
    }
    let problems = rows_off_screen("+601 s", &mut runtime, &state, at);
    (first, screen, status(&mut runtime, at), problems)
}

#[test]
fn a_mirrored_boundary_off_a_refused_entry_onto_one_that_keeps_failing_moves_on() {
    let (_, _, snapshot, problems) = a_failing_mirrored_boundary(true, 100);
    assert!(problems.is_empty(), "{problems:#?}");
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = route(&snapshot, connector);
        assert_eq!(
            row["entry_id"], "all-media-3",
            "all-media-3 never tried: {row}"
        );
    }
    assert_eq!(snapshot["renderer_failed"], false, "{snapshot}");
}

#[test]
fn a_mirrored_boundary_off_a_refused_entry_isnt_said_to_restore_it() {
    let (first, _, snapshot, problems) = a_failing_mirrored_boundary(true, 1);
    let error = first["last_error"].as_str().unwrap();
    assert!(
        error.starts_with("automatic schedule attempt 1/3 failed: "),
        "the premise: the boundary's first attempt failed: {error}"
    );
    assert!(
        !error.contains("previous wallpaper restored"),
        "all-media-1, refused, said restored: {error}"
    );
    assert!(
        error.ends_with("; paired still remains the fallback"),
        "{error}"
    );
    assert!(problems.is_empty(), "{problems:#?}");
    assert_eq!(route(&snapshot, "DP-1")["entry_id"], "all-media-2");
}

#[test]
fn control_an_ordinary_mirrored_boundary_that_fails_restores_its_baseline() {
    let (first, screen, snapshot, problems) = a_failing_mirrored_boundary(false, 1);
    let error = first["last_error"].as_str().unwrap();
    assert!(
        error.starts_with("automatic schedule attempt 1/3 failed: ")
            && error.ends_with("; previous wallpaper restored"),
        "{error}"
    );
    assert_eq!(route(&first, "DP-1")["entry_id"], "all-media-1", "{first}");
    assert_eq!(screen.as_deref(), Some("all-media-1"));
    assert_eq!(first["renderer_failed"], false, "{first}");
    assert!(problems.is_empty(), "{problems:#?}");
    assert_eq!(route(&snapshot, "DP-1")["entry_id"], "pages-1");
}

// -- review r13m R-L-i: a boundary off a refused entry whose rule has nothing left ----------
//
// A boundary off a refused entry moves on rather than restore it (R-L-h). When the rule's
// playlist holds only the refused item and an entry that keeps failing, that entry is taboo
// after the third failure and the playlist has nothing left that can play: it gives way to its
// stand-in, the default. The move found nowhere to go and applied nothing, so status named the
// stand-in's entry, renderer_failed, while the screens kept what the failed apply left.

/// Mirrored, motion on; eve names pages [pages-1, the item 3000000051, pages-2], and pages-3
/// with `third`. With `assigned`, DP-1 is on the default all-media [two stills] and HDMI-A-1 on
/// mixed [mixed-1, the item, mixed-2]; without, no display is assigned and all-media-1 is the
/// item. pages-1 is refused when asked and pages-2 fails on every apply. Started at 15:00 and
/// ticked from the boundary to +601 s: the status then, and what each output was last given.
fn a_mirrored_boundary_onto_a_rule_with_nothing_left(
    assigned: bool,
    third: bool,
) -> (
    serde_json::Value,
    std::collections::BTreeMap<String, String>,
) {
    let pages = "[[playlists.entries]]\nid = \"pages-1\"\nkind = \"still\"\n\
                 still = \"/stills/pages-1.png\"\npalette = { kind = \"keep\", mode = \"keep\" }\n";
    let more: String = (2..=if third { 3 } else { 2 })
        .map(|index| pages.replace("pages-1", &format!("pages-{index}")))
        .collect();
    let scene = |entry: &str| {
        (
            format!("id = \"{entry}\"\nkind = \"still\"\n"),
            format!("id = \"{entry}\"\nkind = \"scene\"\nscene_id = \"3000000051\"\n"),
        )
    };
    let mut document = default_rule_document("pages")
        .replace("dynamics_enabled = false", "dynamics_enabled = true")
        .replace(pages, &format!("{pages}{more}"));
    let item = if assigned {
        document = document
            .replace(
                "connector = \"DP-1\"\nplaylist = \"mixed\"",
                "connector = \"DP-1\"\nplaylist = \"all-media\"",
            )
            .replace(
                "connector = \"HDMI-A-1\"\nplaylist = \"all-media\"",
                "connector = \"HDMI-A-1\"\nplaylist = \"mixed\"",
            );
        "mixed-1"
    } else {
        document.truncate(document.find("[[displays]]").unwrap());
        "all-media-1"
    };
    for entry in ["pages-1", item] {
        let (still, scene) = scene(entry);
        document = document.replace(&still, &scene);
    }
    assert_eq!(
        document.matches("scene_id = \"3000000051\"").count(),
        2,
        "the premise: pages-1 and {item} are the item"
    );
    assert_eq!(document.contains("pages-3"), third, "the premise");
    assert_eq!(document.contains("[[displays]]"), assigned, "the premise");
    let (mut runtime, state) = default_rule_runtime(document, &[], on_the_third(15, 0));
    {
        let mut recorded = state.lock().unwrap();
        recorded.refuse_when_asked.insert("pages-1".into());
        recorded.fail_entry_id = Some("pages-2".into());
        recorded.fail_entry_attempts_remaining = 100;
    }
    let at = on_the_third(16, 0);
    let start = Instant::now();
    for millis in [1_000, 3_100, 5_200, 7_300, 9_400, 61_000, 301_000, 601_000] {
        runtime.tick(at, start + Duration::from_millis(millis));
    }
    (status(&mut runtime, at), screen_of(&state))
}

#[test]
fn a_mirrored_boundary_off_a_refused_entry_whose_rule_has_nothing_left_shows_the_default() {
    let (snapshot, screen) = a_mirrored_boundary_onto_a_rule_with_nothing_left(true, false);
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = route(&snapshot, connector);
        assert_eq!(row["entry_id"], "all-media-1", "{connector}: {row}");
        assert_eq!(row["stand_in_for"], "pages", "{connector}: {row}");
        assert_eq!(
            screen.get(connector).map(String::as_str),
            Some("all-media-1"),
            "{connector}'s screen: {row}"
        );
    }
    assert_eq!(snapshot["renderer_failed"], false, "{snapshot}");
}

#[test]
fn without_assignments_a_mirrored_boundary_whose_rule_has_nothing_left_shows_the_default() {
    let (snapshot, screen) = a_mirrored_boundary_onto_a_rule_with_nothing_left(false, false);
    // The default's all-media-1 is the refused item too: its all-media-2.
    assert_eq!(snapshot["entry_id"], "all-media-2", "{snapshot}");
    assert_eq!(snapshot["stand_in_for"], "pages", "{snapshot}");
    assert_eq!(screen.get("").map(String::as_str), Some("all-media-2"));
    assert_eq!(snapshot["renderer_failed"], false, "{snapshot}");
}

#[test]
fn control_a_mirrored_boundary_off_a_refused_entry_with_a_third_entry_ends_on_it() {
    let (snapshot, screen) = a_mirrored_boundary_onto_a_rule_with_nothing_left(true, true);
    for connector in ["DP-1", "HDMI-A-1"] {
        let row = route(&snapshot, connector);
        assert_eq!(row["entry_id"], "pages-3", "{connector}: {row}");
        assert_eq!(screen.get(connector).map(String::as_str), Some("pages-3"));
    }
    assert_eq!(snapshot["renderer_failed"], false, "{snapshot}");
}

#[test]
fn control_without_assignments_a_mirrored_boundary_with_a_third_entry_ends_on_it() {
    let (snapshot, screen) = a_mirrored_boundary_onto_a_rule_with_nothing_left(false, true);
    assert_eq!(snapshot["entry_id"], "pages-3", "{snapshot}");
    assert_eq!(screen.get("").map(String::as_str), Some("pages-3"));
    assert_eq!(snapshot["renderer_failed"], false, "{snapshot}");
}
