use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};
use wall_in_one_service::config::{Config, ConfigError, EntryKind, Palette};

fn temp_file(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!(
        "wall-in-one-service-{name}-{}-{nonce}.toml",
        std::process::id()
    ))
}

fn document(schema: u32) -> String {
    format!(
        r#"schema_version = {schema}
default_playlist = "day"
[settings]
cycle_interval_seconds = 300
cycle_enabled = true
shuffle = false
dynamics_enabled = true
[renderer]
noctalia_program = "/bin/true"
niri_program = "/bin/true"
mpvpaper_program = "/bin/true"
linux_wallpaperengine_program = "/bin/true"
own_scene_renderer = false
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
id = "one"
kind = "still"
still = "/tmp/one.png"
palette = {{ kind = "adaptive", scheme = "m3-tonal-spot", mode = "dark" }}
[[playlists.entries]]
id = "two"
kind = "video"
still = "/tmp/two.png"
motion = "/tmp/two.mp4"
palette = {{ kind = "named", source = "community", name = "catppuccin", mode = "keep" }}
[[schedules]]
id = "night"
playlist = "day"
weekdays = [0, 1, 2, 3, 4]
start = "22:00"
end = "06:00"
[[displays]]
connector = "eDP-1"
playlist = "day"
"#
    )
}

#[test]
fn handwritten_config_loads_without_python_or_app_state() {
    let path = temp_file("standalone");
    fs::write(&path, document(2)).unwrap();
    let loaded = Config::load(&path).unwrap();
    fs::remove_file(path).unwrap();
    assert_eq!(loaded.playlists[0].entries[1].kind, EntryKind::Video);
    assert!(matches!(
        loaded.playlists[0].entries[0].palette,
        Palette::Adaptive { .. }
    ));
}

#[test]
fn wrong_schema_is_refused() {
    let path = temp_file("schema");
    fs::write(&path, document(99)).unwrap();
    let error = Config::load(&path).unwrap_err();
    fs::remove_file(path).unwrap();
    assert!(matches!(error, ConfigError::Invalid(_)));
    assert!(error.to_string().contains("expected 2"));
}

#[test]
fn relative_resolved_path_is_refused() {
    let path = temp_file("relative");
    fs::write(&path, document(2).replace("/tmp/two.mp4", "two.mp4")).unwrap();
    let error = Config::load(&path).unwrap_err();
    fs::remove_file(path).unwrap();
    assert!(error.to_string().contains("absolute path"));
}
