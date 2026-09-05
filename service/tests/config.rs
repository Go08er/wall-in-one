use std::ffi::CString;
use std::fs::{self, OpenOptions};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::symlink;
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use wall_in_one_service::config::{
    Config, ConfigError, DisplayAssignment, DisplayMode, EntryKind, Palette, Playlist, SceneClamp,
    SceneScaling, ScheduleRule, MAX_CONFIG_BYTES,
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

#[test]
fn battery_policy_requires_schema_five_and_legacy_defaults_off() {
    let legacy: Config = toml::from_str(&document(4)).unwrap();
    legacy.validate().unwrap();
    assert!(!legacy.settings.stop_animations_on_battery);
    for schema in [4, 5] {
        let enabled = document(schema).replace(
            "dynamics_enabled = true",
            "dynamics_enabled = true\nstop_animations_on_battery = true",
        );
        let config: Config = toml::from_str(&enabled).unwrap();
        assert_eq!(config.validate().is_ok(), schema == 5);
    }
}

fn document(schema: u32) -> String {
    format!(
        r#"schema_version = {schema}
config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
default_playlist = "day"
[settings]
cycle_interval_seconds = 300
cycle_enabled = true
shuffle = false
dynamics_enabled = true
display_mode = "mirrored"
theme_source_connector = ""
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
    toml::from_str(&document(4)).unwrap()
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
    fs::write(&path, document(4)).unwrap();
    let loaded = Config::load(&path).unwrap();
    fs::remove_file(path).unwrap();
    assert_eq!(loaded.playlists[0].entries[1].kind, EntryKind::Video);
    assert!(matches!(
        loaded.playlists[0].entries[0].palette,
        Palette::Adaptive { .. }
    ));
}

#[test]
fn loaded_config_digest_binds_exact_bytes_not_only_its_generation() {
    use sha2::{Digest, Sha256};
    let original = document(4);
    let changed = format!("{original}\n# same model, different accepted bytes\n");
    let first = Config::from_bytes(original.as_bytes().to_vec()).unwrap();
    let mut second = Config::from_bytes(changed.as_bytes().to_vec()).unwrap();
    assert_eq!(
        first.source_sha256.as_deref(),
        Some(format!("{:x}", Sha256::digest(original.as_bytes())).as_str())
    );
    assert_ne!(first.source_sha256, second.source_sha256);
    second.source_sha256.clone_from(&first.source_sha256);
    assert_eq!(first, second);
    // In-memory parsing cannot claim that bytes were accepted from disk, and
    // an input document cannot supply this internal provenance field itself.
    assert!(toml::from_str::<Config>(&original)
        .unwrap()
        .source_sha256
        .is_none());
    let forged = format!("source_sha256 = \"{}\"\n{original}", "0".repeat(64));
    assert!(Config::from_bytes(forged.into_bytes()).is_err());
}

#[test]
fn loaded_configuration_keeps_exact_values_without_unused_vector_capacity() {
    for schema in [4, 5] {
        let original = document(schema);
        let (header, rest) = original.split_once("[[playlists]]").unwrap();
        let (_, tail) = rest.split_once("[[schedules]]").unwrap();
        let mut source = header.to_string();
        for (index, count) in [600, 100, 100, 100].into_iter().enumerate() {
            let id = if index == 0 {
                "day".into()
            } else {
                format!("p{index}")
            };
            source.push_str(&format!("\n[[playlists]]\nid = {id:?}\nname = {id:?}\n"));
            for entry in 0..count {
                source.push_str(&format!(
                    "[[playlists.entries]]\nid = \"e{entry}\"\nkind = \"still\"\n\
                     still = \"/wallpapers/{entry}.jpg\"\npalette = {{ kind = \"keep\", mode = \"keep\" }}\n"
                ));
            }
        }
        source.push_str("[[schedules]]");
        source.push_str(tail);
        let mut expected: Config = toml::from_str(&source).unwrap();
        use sha2::{Digest, Sha256};
        expected.source_sha256 = Some(format!("{:x}", Sha256::digest(source.as_bytes())));
        let path = temp_file("compact-config");
        fs::write(&path, source).unwrap();
        let loaded = Config::load(&path).unwrap();
        fs::remove_file(path).unwrap();
        assert_eq!(loaded, expected);
        for playlist in &loaded.playlists {
            assert_eq!(playlist.entries.capacity(), playlist.entries.len());
            assert_eq!(playlist.id.capacity(), playlist.id.len());
            assert_eq!(playlist.name.capacity(), playlist.name.len());
            for entry in &playlist.entries {
                assert_eq!(entry.id.capacity(), entry.id.len());
                assert_eq!(entry.still.capacity(), entry.still.as_os_str().len());
            }
        }
        assert_eq!(loaded.playlists.capacity(), loaded.playlists.len());
        assert_eq!(loaded.schedules.capacity(), loaded.schedules.len());
        assert_eq!(loaded.displays.capacity(), loaded.displays.len());
        for rule in &loaded.schedules {
            assert_eq!(rule.months.capacity(), rule.months.len());
            assert_eq!(rule.weekdays.capacity(), rule.weekdays.len());
        }
    }
}

#[test]
fn config_generation_is_required_canonical_sha256_text() {
    for invalid_generation in [
        "",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
    ] {
        let decoded: Config = toml::from_str(&document(4).replace(
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            invalid_generation,
        ))
        .unwrap();
        let error = decoded.validate().unwrap_err().to_string();
        assert!(error.contains("config_generation"), "{error}");
    }

    let missing = document(4).replace(
        "config_generation = \"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\"\n",
        "",
    );
    assert!(toml::from_str::<Config>(&missing).is_err());
}

#[test]
fn entry_taboo_metadata_is_optional_bounded_and_backwards_compatible() {
    let original = document(4);
    let with_taboo = original.replace(
        "motion = \"/tmp/two.mp4\"",
        "motion = \"/tmp/two.mp4\"\n\
         taboo = { reason = \"mpvpaper rejected this wallpaper\", source = \"automatic-apply\" }",
    );
    let parsed: Config = toml::from_str(&with_taboo).unwrap();
    parsed.validate().unwrap();
    let taboo = parsed.playlists[0].entries[1].taboo.as_ref().unwrap();
    assert_eq!(taboo.reason, "mpvpaper rejected this wallpaper");
    assert_eq!(taboo.source, "automatic-apply");

    let too_long = with_taboo.replace("mpvpaper rejected this wallpaper", &"x".repeat(513));
    let parsed: Config = toml::from_str(&too_long).unwrap();
    assert!(parsed
        .validate()
        .unwrap_err()
        .to_string()
        .contains("taboo reason"));
}

#[test]
fn config_loader_refuses_a_final_symlink() {
    let target = temp_file("symlink-target");
    let link = temp_file("symlink");
    fs::write(&target, document(4)).unwrap();
    symlink(&target, &link).unwrap();

    let error = Config::load(&link).unwrap_err();

    fs::remove_file(link).unwrap();
    fs::remove_file(target).unwrap();
    assert!(matches!(error, ConfigError::Io(_)));
}

#[test]
fn config_loader_refuses_a_fifo_without_blocking() {
    let path = temp_file("fifo");
    let encoded = CString::new(path.as_os_str().as_bytes()).unwrap();
    let created = unsafe { libc::mkfifo(encoded.as_ptr(), 0o600) };
    assert_eq!(created, 0, "{}", std::io::Error::last_os_error());

    let started = Instant::now();
    let error = Config::load(&path).unwrap_err();
    let elapsed = started.elapsed();

    fs::remove_file(path).unwrap();
    assert!(error.to_string().contains("not a regular file"), "{error}");
    assert!(
        elapsed < Duration::from_secs(1),
        "FIFO read blocked for {elapsed:?}"
    );
}

#[test]
fn config_loader_rejects_oversized_regular_files_before_decoding() {
    let path = temp_file("oversized");
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .unwrap();
    file.set_len(MAX_CONFIG_BYTES + 1).unwrap();

    let error = Config::load(&path).unwrap_err();

    fs::remove_file(path).unwrap();
    assert!(matches!(error, ConfigError::TooLarge(size) if size == MAX_CONFIG_BYTES + 1));
}

#[test]
fn renderer_frame_rate_is_bounded() {
    let scene: Config =
        toml::from_str(&document(4).replace("scene_fps = 30", "scene_fps = 241")).unwrap();
    assert!(scene
        .validate()
        .unwrap_err()
        .to_string()
        .contains("scene_fps"));
}

#[test]
fn unknown_video_interpolation_is_refused() {
    let decoded = toml::from_str::<Config>(&document(4).replace(
        "video_interpolation = \"off\"",
        "video_interpolation = \"warp\"",
    ));
    assert!(decoded.is_err());
}

#[test]
fn scene_presentation_modes_are_typed_and_unknown_values_are_refused() {
    let configured: Config = toml::from_str(
        &document(4)
            .replace("scene_scaling = \"\"", "scene_scaling = \"fill\"")
            .replace("scene_clamp = \"\"", "scene_clamp = \"border\""),
    )
    .unwrap();
    assert_eq!(configured.renderer.scene_scaling, SceneScaling::Fill);
    assert_eq!(configured.renderer.scene_clamp, SceneClamp::Border);

    assert!(toml::from_str::<Config>(
        &document(4).replace("scene_scaling = \"\"", "scene_scaling = \"shell words\"")
    )
    .is_err());
    assert!(toml::from_str::<Config>(
        &document(4).replace("scene_clamp = \"\"", "scene_clamp = \"mirror\"")
    )
    .is_err());
}

#[test]
fn missing_video_interpolation_is_refused_by_schema_four() {
    let decoded =
        toml::from_str::<Config>(&document(4).replace("video_interpolation = \"off\"\n", ""));
    assert!(decoded.is_err());
}

#[test]
fn schema_four_display_contract_executes_independent_mode_strictly() {
    let mirrored = parsed();
    assert_eq!(mirrored.settings.display_mode, DisplayMode::Mirrored);
    assert_eq!(mirrored.settings.theme_source_connector, "");
    assert_eq!(mirrored.schedules[0].connector, "");

    let independent = document(4)
        .replace(
            "display_mode = \"mirrored\"",
            "display_mode = \"independent\"",
        )
        .replace(
            "theme_source_connector = \"\"",
            "theme_source_connector = \"DP-1\"",
        )
        .replace(
            "id = \"night\"\nplaylist = \"day\"\nweekdays",
            "id = \"night\"\nplaylist = \"day\"\nconnector = \"DP-1\"\nweekdays",
        );
    let decoded: Config = toml::from_str(&independent).unwrap();
    assert_eq!(decoded.settings.display_mode, DisplayMode::Independent);
    assert_eq!(decoded.settings.theme_source_connector, "DP-1");
    assert_eq!(decoded.schedules[0].connector, "DP-1");
    decoded.validate().unwrap();

    let missing_source: Config = toml::from_str(&document(4).replace(
        "display_mode = \"mirrored\"",
        "display_mode = \"independent\"",
    ))
    .unwrap();
    let error = missing_source.validate().unwrap_err().to_string();
    assert!(
        error.contains("non-empty theme_source_connector"),
        "{error}"
    );

    assert!(toml::from_str::<Config>(
        &document(4).replace("display_mode = \"mirrored\"", "display_mode = \"span\"")
    )
    .is_err());
}

#[test]
fn connector_fields_are_single_protocol_tokens() {
    let theme: Config = toml::from_str(&document(4).replace(
        "theme_source_connector = \"\"",
        "theme_source_connector = \"DP 1\"",
    ))
    .unwrap();
    assert!(theme
        .validate()
        .unwrap_err()
        .to_string()
        .contains("whitespace"));

    let schedule: Config = toml::from_str(&document(4).replace(
        "id = \"night\"\nplaylist = \"day\"\nweekdays",
        "id = \"night\"\nplaylist = \"day\"\nconnector = \"DP 1\"\nweekdays",
    ))
    .unwrap();
    assert!(schedule
        .validate()
        .unwrap_err()
        .to_string()
        .contains("whitespace"));

    let display: Config = toml::from_str(&document(4).replace("eDP-1", "DP 1")).unwrap();
    assert!(display
        .validate()
        .unwrap_err()
        .to_string()
        .contains("whitespace"));
}

#[test]
fn wrong_schema_is_refused() {
    let path = temp_file("schema");
    fs::write(&path, document(99)).unwrap();
    let error = Config::load(&path).unwrap_err();
    fs::remove_file(path).unwrap();
    assert!(matches!(error, ConfigError::Invalid(_)));
    assert!(error.to_string().contains("expected 4"));
    assert!(error.to_string().contains("wall-in-one --write-config"));
}

#[test]
fn relative_resolved_path_is_refused() {
    let path = temp_file("relative");
    fs::write(&path, document(4).replace("/tmp/two.mp4", "two.mp4")).unwrap();
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
    playlists.playlists = (0..579)
        .map(|index| playlist(&base.playlists[0], index))
        .collect();
    playlists.default_playlist = "p0".into();
    playlists.schedules.clear();
    playlists.displays.clear();
    let error = playlists.validate().unwrap_err().to_string();
    assert!(error.contains("no more than 578"), "{error}");

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
    config.playlists = (0..578)
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
