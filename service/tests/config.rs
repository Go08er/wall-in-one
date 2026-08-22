use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};
use wall_in_one_service::config::{
    Config, ConfigError, DisplayAssignment, EntryKind, Palette, Playlist, ScheduleRule,
};

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

fn parsed() -> Config {
    toml::from_str(&document(3)).unwrap()
}

fn playlist(template: &Playlist, index: usize) -> Playlist {
    let mut playlist = template.clone();
    playlist.id = format!("p{index}");
    playlist.name = format!("Playlist {index}");
    playlist
}

#[test]
fn handwritten_config_loads_without_python_or_app_state() {
    let path = temp_file("standalone");
    fs::write(&path, document(3)).unwrap();
    let loaded = Config::load(&path).unwrap();
    fs::remove_file(path).unwrap();
    assert_eq!(loaded.playlists[0].entries[1].kind, EntryKind::Video);
    assert!(matches!(
        loaded.playlists[0].entries[0].palette,
        Palette::Adaptive { .. }
    ));
}

#[test]
fn renderer_frame_rate_is_bounded() {
    let scene: Config =
        toml::from_str(&document(3).replace("scene_fps = 30", "scene_fps = 241")).unwrap();
    assert!(scene
        .validate()
        .unwrap_err()
        .to_string()
        .contains("scene_fps"));
}

#[test]
fn unknown_video_interpolation_is_refused() {
    let decoded = toml::from_str::<Config>(&document(3).replace(
        "video_interpolation = \"off\"",
        "video_interpolation = \"warp\"",
    ));
    assert!(decoded.is_err());
}

#[test]
fn missing_video_interpolation_is_refused_by_schema_three() {
    let decoded =
        toml::from_str::<Config>(&document(3).replace("video_interpolation = \"off\"\n", ""));
    assert!(decoded.is_err());
}

#[test]
fn wrong_schema_is_refused() {
    let path = temp_file("schema");
    fs::write(&path, document(99)).unwrap();
    let error = Config::load(&path).unwrap_err();
    fs::remove_file(path).unwrap();
    assert!(matches!(error, ConfigError::Invalid(_)));
    assert!(error.to_string().contains("expected 3"));
    assert!(error.to_string().contains("wall-in-one --write-config"));
}

#[test]
fn relative_resolved_path_is_refused() {
    let path = temp_file("relative");
    fs::write(&path, document(3).replace("/tmp/two.mp4", "two.mp4")).unwrap();
    let error = Config::load(&path).unwrap_err();
    fs::remove_file(path).unwrap();
    assert!(error.to_string().contains("absolute path"));
}

#[test]
fn duplicate_schedule_ids_are_refused() {
    let mut config = parsed();
    config.schedules.push(config.schedules[0].clone());
    let error = config.validate().unwrap_err().to_string();
    assert!(error.contains("duplicate schedule id"), "{error}");
}

#[test]
fn compiler_cardinality_bounds_are_enforced() {
    let base = parsed();

    let mut playlists = base.clone();
    playlists.playlists = (0..514)
        .map(|index| playlist(&base.playlists[0], index))
        .collect();
    let error = playlists.validate().unwrap_err().to_string();
    assert!(error.contains("no more than 513"), "{error}");

    let mut entries = base.clone();
    let template = entries.playlists[0].entries[0].clone();
    entries.playlists[0].entries = (0..10_001)
        .map(|index| {
            let mut entry = template.clone();
            entry.id = format!("e{index}");
            entry
        })
        .collect();
    let error = entries.validate().unwrap_err().to_string();
    assert!(error.contains("no more than 10000"), "{error}");

    let mut schedules = base.clone();
    let template = schedules.schedules[0].clone();
    schedules.schedules = (0..513)
        .map(|index| ScheduleRule {
            id: format!("r{index}"),
            ..template.clone()
        })
        .collect();
    let error = schedules.validate().unwrap_err().to_string();
    assert!(error.contains("no more than 512"), "{error}");

    let mut displays = base;
    displays.displays = (0..65)
        .map(|index| DisplayAssignment {
            connector: format!("DP-{index}"),
            playlist: "day".into(),
        })
        .collect();
    let error = displays.validate().unwrap_err().to_string();
    assert!(error.contains("no more than 64"), "{error}");
}

#[test]
fn generated_fallback_plus_authoring_maximum_is_accepted() {
    let base = parsed();
    let mut config = base.clone();
    config.playlists = (0..513)
        .map(|index| playlist(&base.playlists[0], index))
        .collect();
    config.default_playlist = "p0".into();
    config.schedules.clear();
    config.displays.clear();
    config.validate().unwrap();
}

#[test]
fn playlist_names_match_the_python_authoring_limit() {
    let mut accepted = parsed();
    accepted.playlists[0].name = "🐈".repeat(120);
    accepted.validate().unwrap();

    let mut refused = parsed();
    refused.playlists[0].name = "x".repeat(121);
    let error = refused.validate().unwrap_err().to_string();
    assert!(error.contains("120 characters"), "{error}");
}

#[test]
fn wire_visible_identifiers_and_paths_are_bounded() {
    let mut identifier = parsed();
    identifier.playlists[0].id = "x".repeat(257);
    identifier.default_playlist = identifier.playlists[0].id.clone();
    identifier.schedules[0].playlist = identifier.playlists[0].id.clone();
    identifier.displays[0].playlist = identifier.playlists[0].id.clone();
    let error = identifier.validate().unwrap_err().to_string();
    assert!(error.contains("256 bytes"), "{error}");

    let mut path = parsed();
    path.playlists[0].entries[0].still = PathBuf::from(format!("/{}", "x".repeat(4096)));
    let error = path.validate().unwrap_err().to_string();
    assert!(error.contains("4096 bytes"), "{error}");
}

#[test]
fn ambiguous_playlist_identity_is_refused() {
    let base = parsed();

    let mut folded = base.clone();
    folded.playlists.push(Playlist {
        id: "other".into(),
        name: "day".into(),
        entries: base.playlists[0].entries.clone(),
    });
    let error = folded.validate().unwrap_err().to_string();
    assert!(error.contains("without case"), "{error}");

    let mut crossed = base;
    crossed.playlists.push(Playlist {
        id: "Day".into(),
        name: "Other".into(),
        entries: crossed.playlists[0].entries.clone(),
    });
    let error = crossed.validate().unwrap_err().to_string();
    assert!(error.contains("another playlist's name"), "{error}");
}

#[test]
fn status_snapshot_is_bounded_before_runtime_start() {
    let base = parsed();
    let mut config = base.clone();
    config.playlists = (0..513)
        .map(|index| {
            let mut found = playlist(&base.playlists[0], index);
            found.name = format!("{index:03}{}", "🐈".repeat(117));
            found
        })
        .collect();
    config.default_playlist = "p0".into();
    let template = base.schedules[0].clone();
    config.schedules = (0..512)
        .map(|index| ScheduleRule {
            id: format!("{}-{index:04}", "r".repeat(251)),
            playlist: format!("p{index}"),
            ..template.clone()
        })
        .collect();
    config.displays.clear();
    let error = config.validate().unwrap_err().to_string();
    assert!(error.contains("protocol response limit"), "{error}");
}
