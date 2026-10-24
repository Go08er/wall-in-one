use std::ffi::CString;
use std::fs::{self, OpenOptions};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::symlink;
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use wall_in_one_service::config::{
    Config, ConfigError, DisplayAssignment, DisplayMode, EntryKind, MAX_CONFIG_BYTES,
    MAX_ENTRY_PROPERTIES, MAX_PROPERTY_NAME_BYTES, MAX_PROPERTY_VALUE_BYTES, OVERRIDES_FILENAME,
    Overrides, Palette, Playlist, SceneClamp, SceneScaling, ScheduleRule, overrides_path,
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

#[test]
fn override_fields_are_never_runtime_toml_keys() {
    // runtime.toml keeps its released shape: an older service, which reads
    // it with deny_unknown_fields, can always load what this build writes.
    for schema in [4, 5] {
        for (needle, key) in [
            (
                "id = \"day\"\nname = \"Day\"\n",
                "cycle_interval_seconds = 60\n",
            ),
            ("id = \"day\"\nname = \"Day\"\n", "shuffle = true\n"),
            (
                "connector = \"eDP-1\"\nplaylist = \"day\"\n",
                "beats_global_rules = true\n",
            ),
        ] {
            let source = document(schema).replace(needle, &format!("{needle}{key}"));
            let error = toml::from_str::<Config>(&source).unwrap_err().to_string();
            assert!(error.contains("unknown field"), "{schema} {key}: {error}");
        }
    }
    // Schema 8 (a playlist that plays nothing, `allow_web_wallpapers`) is the
    // newest this build reads; the next one is refused by name.
    let error = Config::from_bytes(document(9).into_bytes())
        .unwrap_err()
        .to_string();
    assert!(error.contains("expected 4, 5, 6, 7 or 8"), "{error}");
}

fn config_directory(label: &str) -> PathBuf {
    let path = temp_file(label).with_extension("d");
    fs::create_dir_all(&path).unwrap();
    path
}

const OVERRIDES: &str = "schema_version = 1\n\
[[playlists]]\nid = \"day\"\ncycle_interval_seconds = 60\nshuffle = true\n\
[[displays]]\nconnector = \"eDP-1\"\nbeats_global_rules = true\n";

#[test]
fn overrides_beside_the_document_are_applied_on_load() {
    let root = config_directory("overrides");
    let path = root.join("runtime.toml");
    fs::write(&path, document(4)).unwrap();
    let plain = Config::load(&path).unwrap();
    assert_eq!(
        plain,
        Config::from_bytes(document(4).into_bytes()).unwrap(),
        "no overrides file is the ordinary case"
    );
    assert_eq!(plain.overrides_sha256, None);
    assert_eq!(
        (
            plain.playlists[0].cycle_interval_seconds,
            plain.playlists[0].shuffle
        ),
        (None, None)
    );
    assert!(!plain.displays[0].beats_global_rules);

    fs::write(overrides_path(&path), OVERRIDES).unwrap();
    assert_eq!(overrides_path(&path), root.join(OVERRIDES_FILENAME));
    let loaded = Config::load(&path).unwrap();
    assert_eq!(loaded.playlists[0].cycle_interval_seconds, Some(60));
    assert_eq!(loaded.playlists[0].shuffle, Some(true));
    assert!(loaded.displays[0].beats_global_rules);
    assert_eq!(
        loaded.source_sha256, plain.source_sha256,
        "runtime.toml identity"
    );
    assert_eq!(loaded.overrides_sha256.as_ref().map(String::len), Some(64));
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn overrides_for_playlists_or_displays_the_document_lacks_are_skipped() {
    // The app writes the overrides first and runtime.toml second, so a load
    // between the two can see entries for things the document lacks yet.
    let mut config = parsed();
    let overrides = Overrides::from_bytes(
        b"schema_version = 1\n\
          [[playlists]]\nid = \"gone\"\nshuffle = true\n\
          [[playlists]]\nid = \"day\"\ncycle_interval_seconds = 900\n\
          [[displays]]\nconnector = \"HDMI-A-9\"\nbeats_global_rules = true\n",
    )
    .unwrap();
    let skipped = config.apply_overrides(&overrides);
    assert_eq!(skipped.len(), 2, "{skipped:?}");
    assert!(skipped[0].contains("\"gone\"") && skipped[1].contains("\"HDMI-A-9\""));
    assert_eq!(config.playlists[0].cycle_interval_seconds, Some(900));
    assert!(!config.displays[0].beats_global_rules);
}

#[test]
fn a_malformed_overrides_file_fails_the_whole_load() {
    let root = config_directory("bad-overrides");
    let path = root.join("runtime.toml");
    fs::write(&path, document(5)).unwrap();
    for (label, overrides) in [
        ("no schema", OVERRIDES.replace("schema_version = 1\n", "")),
        (
            "text schema",
            OVERRIDES.replace("schema_version = 1", "schema_version = \"1\""),
        ),
        ("negative schema", "schema_version = -1\n".into()),
        ("unknown key", format!("{OVERRIDES}colour = \"red\"\n")),
        (
            "unknown playlist key",
            "schema_version = 1\n[[playlists]]\nid = \"day\"\nweight = 2\n".into(),
        ),
        (
            "interval too short",
            OVERRIDES.replace("cycle_interval_seconds = 60", "cycle_interval_seconds = 4"),
        ),
        (
            "interval too long",
            OVERRIDES.replace(
                "cycle_interval_seconds = 60",
                "cycle_interval_seconds = 86401",
            ),
        ),
        (
            "nothing overridden",
            "schema_version = 1\n[[playlists]]\nid = \"day\"\n".into(),
        ),
        (
            "repeated playlist",
            format!("{OVERRIDES}[[playlists]]\nid = \"day\"\nshuffle = false\n"),
        ),
        (
            "repeated display",
            format!("{OVERRIDES}[[displays]]\nconnector = \"eDP-1\"\nbeats_global_rules = false\n"),
        ),
        ("not toml", "schema_version = \n".into()),
        (
            "wrong type",
            OVERRIDES.replace("shuffle = true", "shuffle = \"yes\""),
        ),
    ] {
        fs::write(overrides_path(&path), &overrides).unwrap();
        let error = Config::load(&path).unwrap_err();
        assert!(matches!(error, ConfigError::Invalid(_)), "{label}: {error}");
        assert!(
            error.to_string().contains(OVERRIDES_FILENAME),
            "{label}: {error}"
        );
    }
    fs::remove_file(overrides_path(&path)).unwrap();
    symlink(&path, overrides_path(&path)).unwrap();
    assert!(
        Config::load(&path).is_err(),
        "a symlinked overrides file is refused"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_newer_overrides_schema_is_ignored_rather_than_fatal() {
    // A newer release adds keys only behind a schema bump. After a rollback
    // this service must keep running on runtime.toml alone.
    let root = config_directory("newer-overrides");
    let path = root.join("runtime.toml");
    fs::write(&path, document(5)).unwrap();
    let plain = Config::load(&path).unwrap();
    let newer = OVERRIDES
        .replace("schema_version = 1", "schema_version = 2")
        .replace("shuffle = true", "shuffle = \"bag\"\ncolour = \"red\"")
        + "[[wallpapers]]\nid = \"x\"\n";
    fs::write(overrides_path(&path), &newer).unwrap();
    let loaded = Config::load(&path).unwrap();
    assert_eq!(
        loaded.overrides_ignored.as_deref(),
        Some("unsupported schema_version 2")
    );
    assert_eq!(loaded.overrides_sha256, None);
    assert_eq!(
        Config {
            overrides_ignored: None,
            ..loaded
        },
        plain,
        "nothing from the newer file was applied"
    );
    assert_eq!(Overrides::declared_schema(newer.as_bytes()).unwrap(), 2);
    assert!(
        Overrides::from_bytes(newer.as_bytes()).is_err(),
        "strict parse is unchanged"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn overrides_bounds_match_the_global_interval() {
    for (seconds, valid) in [(5, true), (86_400, true)] {
        let source = OVERRIDES.replace(
            "cycle_interval_seconds = 60",
            &format!("cycle_interval_seconds = {seconds}"),
        );
        assert_eq!(Overrides::from_bytes(source.as_bytes()).is_ok(), valid);
    }
    assert!(
        Overrides::from_bytes(
            OVERRIDES
                .replace("cycle_interval_seconds = 60", "cycle_interval_seconds = -1")
                .as_bytes()
        )
        .is_err()
    );
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

/// A document as 0.3.x wrote it: `[renderer]` still names niri, which the
/// service once asked for displays.
fn released_document(schema: u32) -> String {
    let current = document(schema);
    assert!(!current.contains("niri_program"));
    current.replace(
        "[renderer]\n",
        "[renderer]\nniri_program = \"/run/current-system/sw/bin/niri\"\n",
    )
}

#[test]
fn a_document_written_by_0_3_with_niri_program_loads_and_means_the_same() {
    for schema in [4, 5] {
        let current = temp_file("current");
        let released = temp_file("released");
        fs::write(&current, document(schema)).unwrap();
        fs::write(&released, released_document(schema)).unwrap();

        let current_config = Config::load(&current).unwrap();
        let released_config = Config::load(&released).unwrap();

        fs::remove_file(current).unwrap();
        fs::remove_file(released).unwrap();
        assert_eq!(
            current_config.renderer, released_config.renderer,
            "niri_program is accepted and ignored"
        );
        assert_eq!(current_config.playlists, released_config.playlists);
    }
}

#[test]
fn niri_program_is_ignored_whatever_it_holds() {
    for value in ["\"relative/niri\"", "\"\"", "42", "false"] {
        let text = document(4).replace(
            "[renderer]\n",
            &format!("[renderer]\nniri_program = {value}\n"),
        );
        let config: Config = toml::from_str(&text).unwrap();
        config.validate().unwrap();
    }
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
    let original = document(4);
    let changed = format!("{original}\n# same model, different accepted bytes\n");
    let first = Config::from_bytes(original.as_bytes().to_vec()).unwrap();
    let mut second = Config::from_bytes(changed.as_bytes().to_vec()).unwrap();
    assert_eq!(
        first.source_sha256.as_deref(),
        // Independently computed with sha256sum, including the final newline.
        // Keep the on-disk fingerprint stable across digest-library upgrades.
        Some("cccc077dfa28c4859c7812426e908d71a520825870cc30e03da20293a734162b")
    );
    assert_ne!(first.source_sha256, second.source_sha256);
    second.source_sha256.clone_from(&first.source_sha256);
    assert_eq!(first, second);
    // In-memory parsing cannot claim that bytes were accepted from disk, and
    // an input document cannot supply this internal provenance field itself.
    assert!(
        toml::from_str::<Config>(&original)
            .unwrap()
            .source_sha256
            .is_none()
    );
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
        expected.source_sha256 = Some(
            Sha256::digest(source.as_bytes())
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect(),
        );
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
    assert!(
        parsed
            .validate()
            .unwrap_err()
            .to_string()
            .contains("taboo reason")
    );
}

/// `document` with a Wallpaper Engine preset in a playlist of its own:
/// `extra` goes after its `scene_id`. Schema 6, the one presets need.
fn with_preset(extra: &str) -> String {
    with_preset_in(6, extra)
}

fn with_preset_in(schema: u32, extra: &str) -> String {
    format!(
        "{}\n[[playlists]]\nid = \"presets\"\nname = \"Presets\"\n\
         [[playlists.entries]]\nid = \"y2k\"\nkind = \"scene\"\nstill = \"/tmp/y2k.png\"\n\
         scene_id = \"2946165131\"\n{extra}\npalette = {{ kind = \"keep\", mode = \"keep\" }}\n",
        document(schema)
    )
}

#[test]
fn a_preset_needs_schema_six_and_schema_six_carries_everything_five_does() {
    for schema in [4, 5] {
        for extra in [
            "scene_base = \"2777242023\"",
            "properties = { bloom = \"true\" }",
        ] {
            let message = refusal(&with_preset_in(schema, extra));
            assert!(
                message.contains("require schema_version 6"),
                "{schema} {extra}: {message}"
            );
        }
        // Without either key, a scene entry is what schemas 4 and 5 always held.
        let plain: Config = toml::from_str(&with_preset_in(schema, "")).unwrap();
        plain.validate().unwrap();
    }
    let battery: Config = toml::from_str(&with_preset("scene_base = \"2777242023\"").replace(
        "theme_source_connector = \"\"\n",
        "theme_source_connector = \"\"\nstop_animations_on_battery = true\n",
    ))
    .unwrap();
    battery.validate().unwrap();
    assert!(battery.settings.stop_animations_on_battery);
    assert_eq!(
        wall_in_one_service::config::SUPPORTED_SCHEMA_VERSIONS,
        &[4, 5, 6, 7, 8]
    );
}

fn refusal(document: &str) -> String {
    let parsed: Config = toml::from_str(document).unwrap();
    parsed.validate().unwrap_err().to_string()
}

#[test]
fn a_preset_carries_an_optional_base_and_bounded_properties() {
    // Without either field: what 0.4.0 wrote and reads.
    let plain: Config = toml::from_str(&with_preset("")).unwrap();
    plain.validate().unwrap();
    let entry = &plain.playlists[1].entries[0];
    assert_eq!(entry.scene_base, None);
    assert!(entry.properties.is_empty());
    assert_eq!(entry.rendered_scene().unwrap(), "2946165131");

    let preset: Config = toml::from_str(&with_preset(
        "scene_base = \"2777242023\"\n\
         properties = { zeta = \"1\", arcs = \"true\", colour_1 = \"0.000000 0.501961 0.000000\" }",
    ))
    .unwrap();
    preset.validate().unwrap();
    let entry = &preset.playlists[1].entries[0];
    assert_eq!(entry.scene_id.as_deref(), Some("2946165131"));
    assert_eq!(entry.rendered_scene().unwrap(), "2777242023");
    assert_eq!(
        entry.properties.keys().collect::<Vec<_>>(),
        ["arcs", "colour_1", "zeta"],
        "name order, whatever order the document gives"
    );

    // The bounds: count, name, value, base, and only on a scene.
    let many = (0..=MAX_ENTRY_PROPERTIES)
        .map(|index| format!("p{index} = \"1\""))
        .collect::<Vec<_>>()
        .join(", ");
    assert!(refusal(&with_preset(&format!("properties = {{ {many} }}"))).contains("properties"));
    let most = (0..MAX_ENTRY_PROPERTIES)
        .map(|index| format!("p{index} = \"1\""))
        .collect::<Vec<_>>()
        .join(", ");
    let parsed: Config =
        toml::from_str(&with_preset(&format!("properties = {{ {most} }}"))).unwrap();
    parsed.validate().unwrap();
    for name in [
        "\"-flag\"",
        "\"a=b\"",
        "\"a b\"",
        "\"a.b\"",
        "\"\"",
        &format!("\"{}\"", "n".repeat(MAX_PROPERTY_NAME_BYTES + 1)),
    ] {
        let message = refusal(&with_preset(&format!("properties = {{ {name} = \"1\" }}")));
        assert!(message.contains("property name"), "{name}: {message}");
    }
    for value in [
        format!("\"{}\"", "v".repeat(MAX_PROPERTY_VALUE_BYTES + 1)),
        "\"1\\n--screen-root\"".to_string(),
    ] {
        let message = refusal(&with_preset(&format!("properties = {{ ok = {value} }}")));
        assert!(message.contains("\"ok\""), "{value}: {message}");
    }
    let longest = "v".repeat(MAX_PROPERTY_VALUE_BYTES);
    let parsed: Config = toml::from_str(&with_preset(&format!(
        "properties = {{ ok = \"{longest}\" }}"
    )))
    .unwrap();
    parsed.validate().unwrap();
    assert!(refusal(&with_preset("scene_base = \"../12\"")).contains("scene_base"));
    let on_a_video = document(4).replace(
        "motion = \"/tmp/two.mp4\"",
        "motion = \"/tmp/two.mp4\"\nproperties = { a = \"1\" }",
    );
    assert!(refusal(&on_a_video).contains("only a scene entry"));
    let base_on_a_still = document(4).replace(
        "still = \"/tmp/one.png\"",
        "still = \"/tmp/one.png\"\nscene_base = \"12\"",
    );
    assert!(refusal(&base_on_a_still).contains("only a scene entry"));
}

#[test]
fn the_renderer_dials_need_schema_seven_and_stay_in_bounds() {
    let video_dials = "motion = \"/tmp/two.mp4\"\nvideo = { speed = 1.5 }";
    let global_dial = "scene_clamp = \"\"\nvideo_rotate = 90\n";
    for schema in [4, 5, 6] {
        let own = document(schema).replace("motion = \"/tmp/two.mp4\"", video_dials);
        assert!(
            refusal(&own).contains("require schema_version 7"),
            "{schema}"
        );
        let global = document(schema).replace("scene_clamp = \"\"\n", global_dial);
        assert!(
            refusal(&global).contains("requires schema_version 7"),
            "{schema}"
        );
        // A dial written at its default is no dial: an older document's shape.
        let default = document(schema).replace(
            "scene_clamp = \"\"\n",
            "scene_clamp = \"\"\nvideo_speed = 1.0\nscene_particles = true\n",
        );
        let parsed: Config = toml::from_str(&default).unwrap();
        parsed.validate().unwrap();
    }
    let both = document(7)
        .replace("motion = \"/tmp/two.mp4\"", video_dials)
        .replace("scene_clamp = \"\"\n", global_dial);
    let parsed: Config = toml::from_str(&both).unwrap();
    parsed.validate().unwrap();
    assert_eq!(parsed.renderer.video_rotate, 90);
    assert_eq!(
        parsed.playlists[0].entries[1].video.as_ref().unwrap().speed,
        Some(1.5)
    );

    for (needle, replacement, expected) in [
        (
            "motion = \"/tmp/two.mp4\"",
            "motion = \"/tmp/two.mp4\"\nvideo = { speed = 9.0 }",
            "speed",
        ),
        (
            "motion = \"/tmp/two.mp4\"",
            "motion = \"/tmp/two.mp4\"\nvideo = { rotate = 45 }",
            "rotate",
        ),
        (
            "motion = \"/tmp/two.mp4\"",
            "motion = \"/tmp/two.mp4\"\nvideo = { gamma = -101 }",
            "gamma",
        ),
        (
            "scene_clamp = \"\"\n",
            "scene_clamp = \"\"\nvideo_speed = 0.1\n",
            "video_speed",
        ),
        (
            "scene_clamp = \"\"\n",
            "scene_clamp = \"\"\nvideo_brightness = 200\n",
            "video_brightness",
        ),
    ] {
        let message = refusal(&document(7).replace(needle, replacement));
        assert!(message.contains(expected), "{replacement}: {message}");
    }
    // Typed: a word where a number goes, or an option nobody allowed, never loads.
    for replacement in [
        "motion = \"/tmp/two.mp4\"\nvideo = { speed = \"1.5 script=/x.lua\" }",
        "motion = \"/tmp/two.mp4\"\nvideo = { script = \"/x.lua\" }",
        "motion = \"/tmp/two.mp4\"\nvideo = { fit = \"fill include=/etc/passwd\" }",
        // Not a dial: mpv's loop goes back to the file's start, not `start`.
        "motion = \"/tmp/two.mp4\"\nvideo = { start = 42 }",
    ] {
        let decoded = toml::from_str::<Config>(
            &document(7).replace("motion = \"/tmp/two.mp4\"", replacement),
        );
        assert!(decoded.is_err(), "{replacement}");
    }
    let video_on_a_still = document(7).replace(
        "still = \"/tmp/one.png\"",
        "still = \"/tmp/one.png\"\nvideo = { speed = 2.0 }",
    );
    assert!(refusal(&video_on_a_still).contains("only a video entry"));
    let scene_dials = with_preset_in(7, "scene = { fps = 0 }");
    assert!(refusal(&scene_dials).contains("fps"));
    let scene_on_video = document(7).replace(
        "motion = \"/tmp/two.mp4\"",
        "motion = \"/tmp/two.mp4\"\nscene = { fps = 60 }",
    );
    assert!(refusal(&scene_on_video).contains("only a video entry"));
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
    assert!(
        scene
            .validate()
            .unwrap_err()
            .to_string()
            .contains("scene_fps")
    );
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

    assert!(
        toml::from_str::<Config>(
            &document(4).replace("scene_scaling = \"\"", "scene_scaling = \"shell words\"")
        )
        .is_err()
    );
    assert!(
        toml::from_str::<Config>(
            &document(4).replace("scene_clamp = \"\"", "scene_clamp = \"mirror\"")
        )
        .is_err()
    );
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

    assert!(
        toml::from_str::<Config>(
            &document(4).replace("display_mode = \"mirrored\"", "display_mode = \"span\"")
        )
        .is_err()
    );
}

#[test]
fn connector_fields_are_single_protocol_tokens() {
    let theme: Config = toml::from_str(&document(4).replace(
        "theme_source_connector = \"\"",
        "theme_source_connector = \"DP 1\"",
    ))
    .unwrap();
    assert!(
        theme
            .validate()
            .unwrap_err()
            .to_string()
            .contains("whitespace")
    );

    let schedule: Config = toml::from_str(&document(4).replace(
        "id = \"night\"\nplaylist = \"day\"\nweekdays",
        "id = \"night\"\nplaylist = \"day\"\nconnector = \"DP 1\"\nweekdays",
    ))
    .unwrap();
    assert!(
        schedule
            .validate()
            .unwrap_err()
            .to_string()
            .contains("whitespace")
    );

    let display: Config = toml::from_str(&document(4).replace("eDP-1", "DP 1")).unwrap();
    assert!(
        display
            .validate()
            .unwrap_err()
            .to_string()
            .contains("whitespace")
    );
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
            beats_global_rules: false,
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
        cycle_interval_seconds: None,
        shuffle: None,
        nothing_to_play: None,
        entries: base.playlists[0].entries.clone(),
    });
    let error = folded.validate().unwrap_err().to_string();
    assert!(error.contains("without case"), "{error}");

    let mut crossed = base;
    crossed.playlists.push(Playlist {
        id: "Day".into(),
        name: "Other".into(),
        cycle_interval_seconds: None,
        shuffle: None,
        nothing_to_play: None,
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

// -- schema 8: a playlist that plays nothing, and allow_web_wallpapers -----------------------

/// `document(schema)` with one more playlist, `Later`, as written after `[[playlists]]`.
fn with_later(schema: u32, later: &str) -> String {
    format!(
        "{}\n[[playlists]]\nid = \"later\"\nname = \"Later\"\n{later}\n",
        document(schema)
    )
}

#[test]
fn schema_eight_reads_a_playlist_that_plays_nothing_with_its_reason() {
    let parsed = Config::from_bytes(
        with_later(
            8,
            "nothing_to_play = \"web wallpapers are off\"\nentries = []",
        )
        .into_bytes(),
    )
    .unwrap();
    let later = parsed.playlist("later").unwrap();
    assert!(later.entries.is_empty());
    assert_eq!(later.nothing_to_play(), Some("web wallpapers are off"));
    // Without the key, entries read as none (the shape 0.4.0 wrote by mistake).
    let parsed =
        Config::from_bytes(with_later(8, "nothing_to_play = \"empty\"").into_bytes()).unwrap();
    assert!(parsed.playlist("later").unwrap().entries.is_empty());
    // A document that plays nothing at all: its only playlist, the default.
    let only = document(8)
        .split("[[playlists]]")
        .next()
        .unwrap()
        .replace("default_playlist = \"day\"", "default_playlist = \"later\"")
        + "[[playlists]]\nid = \"later\"\nname = \"Later\"\nnothing_to_play = \"r\"\nentries = []\n";
    Config::from_bytes(only.into_bytes()).unwrap();
}

#[test]
fn below_schema_eight_a_playlist_with_no_entries_is_refused_by_name() {
    for schema in [4, 5, 6, 7] {
        for later in ["entries = []", "", "nothing_to_play = \"r\"\nentries = []"] {
            let error = Config::from_bytes(with_later(schema, later).into_bytes())
                .unwrap_err()
                .to_string();
            assert!(
                error.contains("playlist \"Later\" has no entries")
                    && error.contains("requires schema_version 8"),
                "{schema} {later:?}: {error}"
            );
        }
    }
}

#[test]
fn a_reason_is_required_exactly_when_a_playlist_has_no_entries() {
    let error = Config::from_bytes(with_later(8, "entries = []").into_bytes())
        .unwrap_err()
        .to_string();
    assert!(error.contains("no nothing_to_play reason"), "{error}");
    let with_entry = with_later(
        8,
        "nothing_to_play = \"r\"\n[[playlists.entries]]\nid = \"a\"\nkind = \"still\"\n\
         still = \"/tmp/a.png\"\npalette = { kind = \"keep\" }",
    );
    let error = Config::from_bytes(with_entry.into_bytes())
        .unwrap_err()
        .to_string();
    assert!(error.contains("can't carry nothing_to_play"), "{error}");
    for reason in [
        "\"\"",
        "\" \"",
        "\"line\\nbreak\"",
        &format!("{:?}", "x".repeat(129)),
    ] {
        let error = Config::from_bytes(
            with_later(8, &format!("nothing_to_play = {reason}\nentries = []")).into_bytes(),
        )
        .unwrap_err()
        .to_string();
        assert!(
            error.contains("nothing_to_play reason"),
            "{reason}: {error}"
        );
    }
    let longest = format!("nothing_to_play = {:?}\nentries = []", "x".repeat(128));
    Config::from_bytes(with_later(8, &longest).into_bytes()).unwrap();
}

#[test]
fn allow_web_wallpapers_needs_schema_eight_and_is_off_when_absent() {
    let allowed = |schema: u32| {
        document(schema).replace(
            "theme_source_connector = \"\"\n",
            "theme_source_connector = \"\"\nallow_web_wallpapers = true\n",
        )
    };
    for schema in [4, 5, 6, 7] {
        let error = Config::from_bytes(allowed(schema).into_bytes())
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("allow_web_wallpapers requires schema_version 8"),
            "{schema}: {error}"
        );
        assert!(
            !Config::from_bytes(document(schema).into_bytes())
                .unwrap()
                .settings
                .allow_web_wallpapers
        );
    }
    assert!(
        Config::from_bytes(allowed(8).into_bytes())
            .unwrap()
            .settings
            .allow_web_wallpapers
    );
}
