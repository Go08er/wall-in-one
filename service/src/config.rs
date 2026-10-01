use crate::protocol::MAX_RESPONSE_BYTES;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fmt;
use std::fs::OpenOptions;
use std::io::{Error, ErrorKind, Read};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

pub const SCHEMA_VERSION: u32 = 5;
pub const SUPPORTED_SCHEMA_VERSIONS: &[u32] = &[4, SCHEMA_VERSION];
pub const MAX_CONFIG_BYTES: u64 = 8 * 1024 * 1024;

/// The optional sibling of `runtime.toml` that carries per-playlist rotation
/// and display precedence. It is found by this name next to the main
/// document and never referenced from it, so a service that predates it
/// never opens it and the main document keeps exactly its released shape.
pub const OVERRIDES_FILENAME: &str = "runtime-overrides.toml";
pub const OVERRIDES_SCHEMA_VERSION: u32 = 1;
pub const SUPPORTED_OVERRIDE_SCHEMAS: &[u32] = &[OVERRIDES_SCHEMA_VERSION];
const MAX_OVERRIDES_BYTES: u64 = 1024 * 1024;
/// Same bounds as the global `cycle_interval_seconds` the app compiles.
pub const MIN_CYCLE_INTERVAL_SECONDS: u64 = 5;
pub const MAX_CYCLE_INTERVAL_SECONDS: u64 = 24 * 60 * 60;

// These mirror the authoring-store ceilings.  The generated all-media
// fallback, one global Quick choice, and up to 64 connector Quick choices are
// additional to the 512 playlists a person can create.
const MAX_PLAYLISTS: usize = 578;
const MAX_ENTRIES_PER_PLAYLIST: usize = 10_000;
const MAX_SCHEDULES: usize = 512;
const MAX_DISPLAYS: usize = 64;
const MAX_PLAYLIST_NAME_CHARS: usize = 120;
const MAX_IDENTIFIER_BYTES: usize = 256;
const MAX_REFERENCE_BYTES: usize = MAX_PLAYLIST_NAME_CHARS * 4;
const MAX_CONNECTOR_BYTES: usize = 256;
const MAX_OPTION_BYTES: usize = 256;
pub const MAX_PATH_BYTES: usize = 4096;
const CONFIG_GENERATION_HEX_BYTES: usize = 64;
const MAX_TABOO_REASON_BYTES: usize = 512;
const MAX_TABOO_SOURCE_BYTES: usize = 64;

// Status is a single atomic snapshot containing every playlist and schedule.
// Reserve room for JSON structure and ordinary bounded runtime diagnostics,
// then count every configured string at four times its UTF-8 size.  Status is
// JSON stored inside the line protocol's JSON `message`, so quotes and
// backslashes can be escaped twice.  This prevents a successful status
// snapshot from crossing protocol::MAX_RESPONSE_BYTES only when queried.
const STATUS_STRUCTURAL_RESERVE: usize = 256 * 1024;
const STATUS_DIAGNOSTIC_RESERVE: usize = 64 * 1024;
const STATUS_JSON_ESCAPE_FACTOR: usize = 4;
const MAX_LIVE_OUTPUTS: usize = 64;

#[derive(Debug)]
pub enum ConfigError {
    Io(std::io::Error),
    TooLarge(u64),
    Decode(toml::de::Error),
    Invalid(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "cannot read config: {error}"),
            Self::TooLarge(bytes) => write!(f, "config is {bytes} bytes, over the limit"),
            Self::Decode(error) => write!(f, "config is not valid TOML: {error}"),
            Self::Invalid(detail) => write!(f, "invalid config: {detail}"),
        }
    }
}

impl std::error::Error for ConfigError {}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// Identity of the exact successfully parsed bytes, never an authored key.
    #[serde(skip)]
    pub source_sha256: Option<String>,
    /// Identity of the applied `runtime-overrides.toml` bytes, if one exists.
    #[serde(skip)]
    pub overrides_sha256: Option<String>,
    /// Why a present `runtime-overrides.toml` was not applied: a schema this
    /// service does not know, written by a newer release.
    #[serde(skip)]
    pub overrides_ignored: Option<String>,
    pub schema_version: u32,
    pub config_generation: String,
    pub default_playlist: String,
    pub settings: Settings,
    pub renderer: RendererSettings,
    pub playlists: Vec<Playlist>,
    #[serde(default)]
    pub schedules: Vec<ScheduleRule>,
    #[serde(default)]
    pub displays: Vec<DisplayAssignment>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub cycle_interval_seconds: u64,
    pub cycle_enabled: bool,
    pub shuffle: bool,
    pub dynamics_enabled: bool,
    #[serde(default)]
    pub stop_animations_on_battery: bool,
    pub display_mode: DisplayMode,
    pub theme_source_connector: String,
}

#[derive(Clone, Copy, Debug, Deserialize, serde::Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum DisplayMode {
    Mirrored,
    Independent,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RendererSettings {
    pub noctalia_program: PathBuf,
    pub niri_program: PathBuf,
    pub mpvpaper_program: PathBuf,
    pub linux_wallpaperengine_program: PathBuf,
    pub own_scene_renderer: bool,
    pub layer: String,
    pub video_when_hidden: VideoWhenHidden,
    pub video_hardware_decode: bool,
    pub video_interpolation: VideoInterpolation,
    pub video_muted: bool,
    pub video_volume: u8,
    pub scene_fps: u16,
    pub scene_muted: bool,
    pub scene_volume: u8,
    pub scene_pause_when_covered: bool,
    pub scene_scaling: SceneScaling,
    pub scene_clamp: SceneClamp,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum SceneScaling {
    #[default]
    #[serde(rename = "")]
    Default,
    Stretch,
    Fit,
    Fill,
}

impl SceneScaling {
    pub fn option(self) -> Option<&'static str> {
        match self {
            Self::Default => None,
            Self::Stretch => Some("stretch"),
            Self::Fit => Some("fit"),
            Self::Fill => Some("fill"),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum SceneClamp {
    #[default]
    #[serde(rename = "")]
    Default,
    Clamp,
    Border,
    Repeat,
}

impl SceneClamp {
    pub fn option(self) -> Option<&'static str> {
        match self {
            Self::Default => None,
            Self::Clamp => Some("clamp"),
            Self::Border => Some("border"),
            Self::Repeat => Some("repeat"),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum VideoWhenHidden {
    Pause,
    Stop,
    Play,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum VideoInterpolation {
    #[default]
    Off,
    Oversample,
    Linear,
}

impl VideoInterpolation {
    pub fn tscale(self) -> Option<&'static str> {
        match self {
            Self::Off => None,
            Self::Oversample => Some("oversample"),
            Self::Linear => Some("linear"),
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Playlist {
    pub id: String,
    pub name: String,
    /// This playlist's own rotation interval, from `runtime-overrides.toml`
    /// only (never a `runtime.toml` key). `None` means the global
    /// `settings.cycle_interval_seconds`.
    #[serde(skip)]
    pub cycle_interval_seconds: Option<u64>,
    /// This playlist's own shuffle default, from `runtime-overrides.toml`
    /// only. `None` means the global `settings.shuffle`. A live
    /// `shuffle on|off` override still wins.
    #[serde(skip)]
    pub shuffle: Option<bool>,
    pub entries: Vec<Entry>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub id: String,
    pub kind: EntryKind,
    pub still: PathBuf,
    #[serde(default)]
    pub motion: Option<PathBuf>,
    #[serde(default)]
    pub scene_id: Option<String>,
    #[serde(default)]
    pub taboo: Option<EntryTaboo>,
    pub palette: Palette,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EntryTaboo {
    pub reason: String,
    pub source: String,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum EntryKind {
    Still,
    Video,
    Scene,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
pub enum Palette {
    Keep {
        #[serde(default)]
        mode: ThemeMode,
    },
    Adaptive {
        scheme: String,
        #[serde(default)]
        mode: ThemeMode,
    },
    Named {
        source: PaletteSource,
        name: String,
        #[serde(default)]
        mode: ThemeMode,
    },
}

impl Palette {
    pub fn mode(&self) -> ThemeMode {
        match self {
            Self::Keep { mode } | Self::Adaptive { mode, .. } | Self::Named { mode, .. } => *mode,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ThemeMode {
    #[default]
    Keep,
    Dark,
    Light,
    Auto,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum PaletteSource {
    Builtin,
    Community,
    Custom,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ScheduleRule {
    pub id: String,
    pub playlist: String,
    #[serde(default)]
    pub connector: String,
    #[serde(default)]
    pub months: Vec<u8>,
    #[serde(default)]
    pub weekdays: Vec<u8>,
    #[serde(default)]
    pub start: Option<String>,
    #[serde(default)]
    pub end: Option<String>,
    #[serde(default = "default_true")]
    pub enabled: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct DisplayAssignment {
    pub connector: String,
    pub playlist: String,
    /// Opt-in from `runtime-overrides.toml` only: this display's own playlist
    /// beats global (untargeted) schedule rules. Rules aimed at this
    /// connector still win. Off keeps the established order: manual, any
    /// matching rule, assignment, default.
    #[serde(skip)]
    pub beats_global_rules: bool,
}

/// The one key of `runtime-overrides.toml` every schema shares, read without
/// rejecting anything else: a newer schema may add keys this service lacks.
#[derive(Deserialize)]
struct OverridesSchema {
    schema_version: u32,
}

/// `runtime-overrides.toml`: what the app sets beyond the released document.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Overrides {
    pub schema_version: u32,
    #[serde(default)]
    pub playlists: Vec<PlaylistOverride>,
    #[serde(default)]
    pub displays: Vec<DisplayOverride>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PlaylistOverride {
    /// The runtime playlist id, exactly as in `runtime.toml`.
    pub id: String,
    #[serde(default)]
    pub cycle_interval_seconds: Option<u64>,
    #[serde(default)]
    pub shuffle: Option<bool>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct DisplayOverride {
    /// A connector that has an explicit assignment in `runtime.toml`.
    pub connector: String,
    pub beats_global_rules: bool,
}

impl Overrides {
    /// The schema an overrides file declares, ignoring everything else.
    ///
    /// Additions to the file bump its `schema_version`, and a service skips
    /// a version it does not know instead of refusing to start, so a newer
    /// release's file never stops an older service after a rollback. A file
    /// that is not TOML or names no integer schema is damage, not the future.
    pub fn declared_schema(bytes: &[u8]) -> Result<u32, ConfigError> {
        let text = std::str::from_utf8(bytes).map_err(|error| {
            ConfigError::Io(Error::new(
                ErrorKind::InvalidData,
                format!("{OVERRIDES_FILENAME} is not UTF-8: {error}"),
            ))
        })?;
        let declared: OverridesSchema = toml::from_str(text).map_err(ConfigError::Decode)?;
        Ok(declared.schema_version)
    }

    /// Parse and validate a file of a schema this service supports.
    pub fn from_bytes(bytes: &[u8]) -> Result<Self, ConfigError> {
        let text = std::str::from_utf8(bytes).map_err(|error| {
            ConfigError::Io(Error::new(
                ErrorKind::InvalidData,
                format!("{OVERRIDES_FILENAME} is not UTF-8: {error}"),
            ))
        })?;
        let overrides: Self = toml::from_str(text).map_err(ConfigError::Decode)?;
        overrides.validate()?;
        Ok(overrides)
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        if !SUPPORTED_OVERRIDE_SCHEMAS.contains(&self.schema_version) {
            return invalid(format!(
                "{OVERRIDES_FILENAME} schema_version {} is unsupported; expected {OVERRIDES_SCHEMA_VERSION}",
                self.schema_version
            ));
        }
        if self.playlists.len() > MAX_PLAYLISTS || self.displays.len() > MAX_DISPLAYS {
            return invalid(format!(
                "{OVERRIDES_FILENAME} has more than {MAX_PLAYLISTS} playlists or {MAX_DISPLAYS} displays"
            ));
        }
        let mut ids = HashSet::new();
        for playlist in &self.playlists {
            bounded_nonempty("override playlist id", &playlist.id, MAX_IDENTIFIER_BYTES)?;
            if !ids.insert(playlist.id.as_str()) {
                return invalid(format!(
                    "{OVERRIDES_FILENAME} repeats playlist {:?}",
                    playlist.id
                ));
            }
            if playlist.cycle_interval_seconds.is_none() && playlist.shuffle.is_none() {
                return invalid(format!(
                    "{OVERRIDES_FILENAME} playlist {:?} overrides nothing",
                    playlist.id
                ));
            }
            if let Some(seconds) = playlist.cycle_interval_seconds
                && !(MIN_CYCLE_INTERVAL_SECONDS..=MAX_CYCLE_INTERVAL_SECONDS).contains(&seconds)
            {
                return invalid(format!(
                    "{OVERRIDES_FILENAME} playlist {:?} cycle_interval_seconds must be between {MIN_CYCLE_INTERVAL_SECONDS} and {MAX_CYCLE_INTERVAL_SECONDS}",
                    playlist.id
                ));
            }
        }
        let mut connectors = HashSet::new();
        for display in &self.displays {
            bounded_connector("override display connector", &display.connector, true)?;
            if !connectors.insert(display.connector.as_str()) {
                return invalid(format!(
                    "{OVERRIDES_FILENAME} repeats display {:?}",
                    display.connector
                ));
            }
        }
        Ok(())
    }
}

/// Where `runtime-overrides.toml` lives for one `runtime.toml` path.
pub fn overrides_path(config_path: &Path) -> PathBuf {
    config_path.with_file_name(OVERRIDES_FILENAME)
}

fn default_true() -> bool {
    true
}

impl Config {
    /// Load `runtime.toml` and, when present, its `runtime-overrides.toml`.
    ///
    /// A missing overrides file is the ordinary case. One declaring a schema
    /// this service does not know (a newer release wrote it) is skipped with
    /// one log line and reported in status; the service runs without it. One
    /// declaring a supported schema must be valid, or the whole load fails
    /// like any other config error. Its entries that name a playlist or
    /// connector this document lacks are skipped with a log line (the app
    /// writes the overrides first, so a reader can briefly see them ahead of
    /// the document they belong to).
    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let mut config = Self::from_bytes(Self::read_bytes(path)?)?;
        let sidecar = overrides_path(path);
        match Self::read_bytes_bounded(&sidecar, MAX_OVERRIDES_BYTES) {
            Ok(bytes) => {
                let invalid_sidecar = |error: ConfigError| {
                    ConfigError::Invalid(format!("{}: {error}", sidecar.display()))
                };
                let schema = Overrides::declared_schema(&bytes).map_err(invalid_sidecar)?;
                if !SUPPORTED_OVERRIDE_SCHEMAS.contains(&schema) {
                    let reason = format!("unsupported schema_version {schema}");
                    eprintln!(
                        "wall-in-one-service: {}: not applied: {reason}; this service applies schema_version {OVERRIDES_SCHEMA_VERSION}",
                        sidecar.display()
                    );
                    config.overrides_ignored = Some(reason);
                    return Ok(config);
                }
                let digest = sha256_hex(&bytes);
                let overrides = Overrides::from_bytes(&bytes).map_err(invalid_sidecar)?;
                for skipped in config.apply_overrides(&overrides) {
                    eprintln!("wall-in-one-service: {}: {skipped}", sidecar.display());
                }
                config.overrides_sha256 = Some(digest);
            }
            Err(ConfigError::Io(error)) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => {
                return Err(ConfigError::Invalid(format!(
                    "{}: {error}",
                    sidecar.display()
                )));
            }
        }
        Ok(config)
    }

    /// Apply validated overrides onto this document; return what was skipped.
    ///
    /// Only an explicit assignment can beat global rules, so a connector this
    /// document does not assign is skipped. (The runtime still leaves the
    /// opt-in dormant in mirrored mode, where there is one route.)
    pub fn apply_overrides(&mut self, overrides: &Overrides) -> Vec<String> {
        let mut skipped = Vec::new();
        for wanted in &overrides.playlists {
            match self.playlists.iter_mut().find(|p| p.id == wanted.id) {
                Some(playlist) => {
                    playlist.cycle_interval_seconds = wanted.cycle_interval_seconds;
                    playlist.shuffle = wanted.shuffle;
                }
                None => skipped.push(format!(
                    "ignoring rotation for playlist {:?}, which this runtime.toml does not have",
                    wanted.id
                )),
            }
        }
        for wanted in &overrides.displays {
            match self
                .displays
                .iter_mut()
                .find(|d| d.connector == wanted.connector)
            {
                Some(display) => display.beats_global_rules = wanted.beats_global_rules,
                None => skipped.push(format!(
                    "ignoring beats_global_rules for display {:?}, which this runtime.toml does not assign",
                    wanted.connector
                )),
            }
        }
        skipped
    }

    /// Read one bounded generation; parsing and status hash the same bytes.
    pub fn read_bytes(path: &Path) -> Result<Vec<u8>, ConfigError> {
        Self::read_bytes_bounded(path, MAX_CONFIG_BYTES)
    }

    fn read_bytes_bounded(path: &Path, maximum: u64) -> Result<Vec<u8>, ConfigError> {
        // Open once, without following a final symlink or waiting on a FIFO,
        // then inspect and read that exact descriptor. A metadata(path) followed
        // by read_to_string(path) lets an attacker replace the path between the
        // two operations; trusting st_size alone also lets a growing file bypass
        // the wire bound.
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)
            .map_err(ConfigError::Io)?;
        let metadata = file.metadata().map_err(ConfigError::Io)?;
        if !metadata.is_file() {
            return invalid("config path is not a regular file");
        }
        if metadata.len() > maximum {
            return Err(ConfigError::TooLarge(metadata.len()));
        }
        let mut bytes = Vec::with_capacity(metadata.len() as usize);
        file.take(maximum + 1)
            .read_to_end(&mut bytes)
            .map_err(ConfigError::Io)?;
        if bytes.len() as u64 > maximum {
            return Err(ConfigError::TooLarge(bytes.len() as u64));
        }
        Ok(bytes)
    }

    pub fn from_bytes(bytes: Vec<u8>) -> Result<Self, ConfigError> {
        if bytes.len() as u64 > MAX_CONFIG_BYTES {
            return Err(ConfigError::TooLarge(bytes.len() as u64));
        }
        let digest = sha256_hex(&bytes);
        let text = String::from_utf8(bytes).map_err(|error| {
            ConfigError::Io(Error::new(
                ErrorKind::InvalidData,
                format!("config is not UTF-8: {error}"),
            ))
        })?;
        let mut config: Self = toml::from_str(&text).map_err(ConfigError::Decode)?;
        config.source_sha256 = Some(digest);
        config.validate()?;
        // Deserialization interleaves retained strings/vectors with temporary
        // parser allocations. Shrinking vectors alone leaves small live
        // objects pinning most of those otherwise-free heap pages. Compact the
        // validated model once while those objects are still allocated, then
        // release the fragmented original and the input before publishing it.
        // Clone also gives every immutable Vec/String exactly its used length.
        // This costs one bounded copy at load/reload, never on status or tick.
        let compact = config.clone();
        drop(config);
        drop(text);
        #[cfg(all(target_os = "linux", target_env = "gnu"))]
        // SAFETY: malloc_trim only releases completely free allocator pages.
        // Do it before initial renderer helpers can block, and on reload too;
        // retaining parser slack for the session has no runtime benefit.
        unsafe {
            libc::malloc_trim(0);
        }
        Ok(compact)
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        if !SUPPORTED_SCHEMA_VERSIONS.contains(&self.schema_version) {
            return invalid(format!(
                "schema_version {} is unsupported; expected 4 or {SCHEMA_VERSION}; regenerate it with `wall-in-one --write-config` or restart the packaged wall-in-one.service",
                self.schema_version
            ));
        }
        if self.schema_version == 4 && self.settings.stop_animations_on_battery {
            return invalid("stop_animations_on_battery requires schema_version 5");
        }
        if self.config_generation.len() != CONFIG_GENERATION_HEX_BYTES
            || !self
                .config_generation
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return invalid(
                "config_generation must be exactly 64 lowercase hexadecimal characters",
            );
        }
        if self.settings.cycle_interval_seconds < 5 {
            return invalid("cycle_interval_seconds must be at least 5");
        }
        bounded_connector(
            "theme source connector",
            &self.settings.theme_source_connector,
            false,
        )?;
        if self.settings.display_mode == DisplayMode::Independent
            && self.settings.theme_source_connector.is_empty()
        {
            return invalid("independent display mode needs a non-empty theme_source_connector");
        }
        if self.renderer.video_volume > 100 || self.renderer.scene_volume > 100 {
            return invalid("renderer volume must be between 0 and 100");
        }
        if self.renderer.scene_fps == 0 || self.renderer.scene_fps > 240 {
            return invalid("scene_fps must be between 1 and 240");
        }
        bounded_nonempty("renderer layer", &self.renderer.layer, MAX_OPTION_BYTES)?;
        for (label, path) in [
            ("noctalia_program", &self.renderer.noctalia_program),
            ("niri_program", &self.renderer.niri_program),
            ("mpvpaper_program", &self.renderer.mpvpaper_program),
            (
                "linux_wallpaperengine_program",
                &self.renderer.linux_wallpaperengine_program,
            ),
        ] {
            absolute(label, path)?;
        }

        if self.playlists.len() > MAX_PLAYLISTS {
            return invalid(format!(
                "there are {} playlists; no more than {MAX_PLAYLISTS} are supported",
                self.playlists.len()
            ));
        }
        if self.schedules.len() > MAX_SCHEDULES {
            return invalid(format!(
                "there are {} schedule rules; no more than {MAX_SCHEDULES} are supported",
                self.schedules.len()
            ));
        }
        if self.displays.len() > MAX_DISPLAYS {
            return invalid(format!(
                "there are {} display assignments; no more than {MAX_DISPLAYS} are supported",
                self.displays.len()
            ));
        }

        let mut ids = HashMap::new();
        let mut names = HashMap::new();
        let mut folded_names = HashSet::new();
        for (playlist_index, playlist) in self.playlists.iter().enumerate() {
            bounded_nonempty("playlist id", &playlist.id, MAX_IDENTIFIER_BYTES)?;
            bounded_nonempty_chars("playlist name", &playlist.name, MAX_PLAYLIST_NAME_CHARS)?;
            if ids.insert(playlist.id.as_str(), playlist_index).is_some() {
                return invalid(format!("duplicate playlist id {:?}", playlist.id));
            }
            if names
                .insert(playlist.name.as_str(), playlist_index)
                .is_some()
            {
                return invalid(format!("duplicate playlist name {:?}", playlist.name));
            }
            if !folded_names.insert(fold_name(&playlist.name)) {
                return invalid(format!(
                    "duplicate playlist name {:?} when compared without case",
                    playlist.name
                ));
            }
            if playlist.entries.len() > MAX_ENTRIES_PER_PLAYLIST {
                return invalid(format!(
                    "playlist {:?} has {} entries; no more than {MAX_ENTRIES_PER_PLAYLIST} are supported",
                    playlist.name,
                    playlist.entries.len()
                ));
            }
            let mut entry_ids = HashSet::new();
            for entry in &playlist.entries {
                bounded_nonempty("entry id", &entry.id, MAX_IDENTIFIER_BYTES)?;
                if !entry_ids.insert(entry.id.as_str()) {
                    return invalid(format!(
                        "duplicate entry id {:?} in playlist {:?}",
                        entry.id, playlist.name
                    ));
                }
                absolute("entry still", &entry.still)?;
                match entry.kind {
                    EntryKind::Still if entry.motion.is_none() && entry.scene_id.is_none() => {}
                    EntryKind::Video if entry.scene_id.is_none() => match &entry.motion {
                        Some(path) => absolute("video motion", path)?,
                        None => return invalid("video entry needs motion"),
                    },
                    EntryKind::Scene if entry.motion.is_none() => match &entry.scene_id {
                        Some(id)
                            if !id.is_empty()
                                && id.len() <= MAX_IDENTIFIER_BYTES
                                && id.bytes().all(|b| b.is_ascii_digit()) => {}
                        _ => return invalid("scene entry needs a numeric scene_id"),
                    },
                    EntryKind::Still => {
                        return invalid("still entry must not carry motion or scene_id");
                    }
                    EntryKind::Video => return invalid("video entry must not carry scene_id"),
                    EntryKind::Scene => return invalid("scene entry must not carry motion"),
                }
                match &entry.palette {
                    Palette::Keep { .. } => {}
                    Palette::Adaptive { scheme, .. } => {
                        bounded_nonempty("adaptive scheme", scheme, MAX_OPTION_BYTES)?
                    }
                    Palette::Named { name, .. } => {
                        bounded_nonempty("palette name", name, MAX_OPTION_BYTES)?
                    }
                }
                if let Some(taboo) = &entry.taboo {
                    bounded_nonempty("entry taboo reason", &taboo.reason, MAX_TABOO_REASON_BYTES)?;
                    bounded_nonempty("entry taboo source", &taboo.source, MAX_TABOO_SOURCE_BYTES)?;
                }
            }
        }
        if self.playlists.is_empty() {
            return invalid("at least one playlist is required");
        }
        for (id, id_index) in &ids {
            if let Some(name_index) = names.get(id)
                && id_index != name_index
            {
                return invalid(format!(
                    "playlist id {id:?} is also another playlist's name"
                ));
            }
        }
        bounded_nonempty(
            "default playlist reference",
            &self.default_playlist,
            MAX_REFERENCE_BYTES,
        )?;
        reference(&self.default_playlist, &ids, &names)?;
        let mut schedule_ids = HashSet::new();
        for rule in &self.schedules {
            bounded_nonempty("schedule id", &rule.id, MAX_IDENTIFIER_BYTES)?;
            if !schedule_ids.insert(rule.id.as_str()) {
                return invalid(format!("duplicate schedule id {:?}", rule.id));
            }
            bounded_nonempty(
                "schedule playlist reference",
                &rule.playlist,
                MAX_REFERENCE_BYTES,
            )?;
            bounded_connector("schedule connector", &rule.connector, false)?;
            reference(&rule.playlist, &ids, &names)?;
            if rule.months.len() > 12
                || rule.months.iter().any(|m| !(1..=12).contains(m))
                || rule.months.iter().collect::<HashSet<_>>().len() != rule.months.len()
            {
                return invalid(format!("schedule {:?} has an invalid month", rule.id));
            }
            if rule.weekdays.len() > 7
                || rule.weekdays.iter().any(|d| *d > 6)
                || rule.weekdays.iter().collect::<HashSet<_>>().len() != rule.weekdays.len()
            {
                return invalid(format!("schedule {:?} has an invalid weekday", rule.id));
            }
            match (&rule.start, &rule.end) {
                (None, None) => {}
                (Some(start), Some(end)) => {
                    parse_time(start)?;
                    parse_time(end)?;
                }
                _ => {
                    return invalid(format!(
                        "schedule {:?} has only half a time window",
                        rule.id
                    ));
                }
            }
        }
        let mut connectors = HashSet::new();
        for display in &self.displays {
            bounded_connector("display connector", &display.connector, true)?;
            if !connectors.insert(display.connector.as_str()) {
                return invalid(format!(
                    "duplicate display connector {:?}",
                    display.connector
                ));
            }
            bounded_nonempty(
                "display playlist reference",
                &display.playlist,
                MAX_REFERENCE_BYTES,
            )?;
            reference(&display.playlist, &ids, &names)?;
        }
        self.validate_status_budget()?;
        Ok(())
    }

    fn validate_status_budget(&self) -> Result<(), ConfigError> {
        let mut configured_text = 0_usize;
        let mut largest_playlist_identity = 0_usize;
        let mut largest_entry = 0_usize;

        for playlist in &self.playlists {
            let identity = playlist.id.len().saturating_add(playlist.name.len());
            configured_text = configured_text.saturating_add(identity);
            largest_playlist_identity = largest_playlist_identity.max(identity);
            for entry in &playlist.entries {
                largest_entry =
                    largest_entry.max(entry.id.len().saturating_add(path_bytes(&entry.still)));
            }
        }
        for rule in &self.schedules {
            let playlist = self
                .playlist(&rule.playlist)
                .expect("playlist references were validated before the status budget");
            configured_text = configured_text
                .saturating_add(rule.id.len())
                .saturating_add(rule.connector.len())
                .saturating_add(playlist.id.len())
                .saturating_add(playlist.name.len())
                .saturating_add(rule.start.as_ref().map_or(0, String::len))
                .saturating_add(rule.end.as_ref().map_or(0, String::len));
        }

        // niri discovery is independently capped at 64 live outputs and the
        // atomic inventory also retains up to 64 configured-but-detached
        // assignments. A manual override may put the largest playlist and
        // entry on every row, so count the full 128-row union.
        let largest_connector = self
            .displays
            .iter()
            .map(|display| display.connector.len())
            .max()
            .unwrap_or(MAX_CONNECTOR_BYTES)
            .max(MAX_CONNECTOR_BYTES);
        let per_display = largest_connector
            .saturating_add(largest_playlist_identity.saturating_mul(2))
            .saturating_add(largest_entry);
        configured_text = configured_text
            .saturating_add(
                per_display.saturating_mul(MAX_LIVE_OUTPUTS.saturating_add(MAX_DISPLAYS)),
            )
            .saturating_add(largest_playlist_identity)
            .saturating_add(largest_entry);

        let encoded_bound = configured_text
            .saturating_mul(STATUS_JSON_ESCAPE_FACTOR)
            .saturating_add(STATUS_STRUCTURAL_RESERVE)
            .saturating_add(STATUS_DIAGNOSTIC_RESERVE);
        if encoded_bound > MAX_RESPONSE_BYTES {
            return invalid(format!(
                "runtime status could exceed the {MAX_RESPONSE_BYTES}-byte protocol response limit"
            ));
        }
        Ok(())
    }

    pub fn playlist(&self, value: &str) -> Option<&Playlist> {
        self.playlists
            .iter()
            .find(|p| p.id == value || p.name == value)
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn absolute(label: &str, path: &Path) -> Result<(), ConfigError> {
    if !path.is_absolute() {
        invalid(format!(
            "{label} must be an absolute path: {}",
            path.display()
        ))
    } else {
        let rendered = path.to_string_lossy();
        if rendered.len() > MAX_PATH_BYTES || rendered.chars().any(char::is_control) {
            invalid(format!(
                "{label} is longer than {MAX_PATH_BYTES} bytes or contains control characters"
            ))
        } else {
            Ok(())
        }
    }
}
fn path_bytes(path: &Path) -> usize {
    path.to_string_lossy().len()
}
fn bounded_nonempty(label: &str, value: &str, maximum_bytes: usize) -> Result<(), ConfigError> {
    if !value.trim().is_empty()
        && value.len() <= maximum_bytes
        && !value.chars().any(char::is_control)
    {
        Ok(())
    } else {
        invalid(format!(
            "{label} is empty, longer than {maximum_bytes} bytes, or contains control characters"
        ))
    }
}
fn bounded_nonempty_chars(
    label: &str,
    value: &str,
    maximum_chars: usize,
) -> Result<(), ConfigError> {
    if !value.trim().is_empty()
        && value.chars().count() <= maximum_chars
        && !value.chars().any(char::is_control)
    {
        Ok(())
    } else {
        invalid(format!(
            "{label} is empty, longer than {maximum_chars} characters, or contains control characters"
        ))
    }
}
fn bounded_connector(label: &str, value: &str, required: bool) -> Result<(), ConfigError> {
    if (!required || !value.is_empty())
        && !value.chars().any(char::is_whitespace)
        && value.len() <= MAX_CONNECTOR_BYTES
        && !value.chars().any(char::is_control)
    {
        Ok(())
    } else {
        invalid(format!(
            "{label} is empty when required, contains whitespace, is longer than {MAX_CONNECTOR_BYTES} bytes, or contains control characters"
        ))
    }
}
fn fold_name(value: &str) -> String {
    value.chars().flat_map(char::to_lowercase).collect()
}
fn reference(
    value: &str,
    ids: &HashMap<&str, usize>,
    names: &HashMap<&str, usize>,
) -> Result<(), ConfigError> {
    if ids.contains_key(value) || names.contains_key(value) {
        Ok(())
    } else {
        invalid(format!("unknown playlist {value:?}"))
    }
}
pub fn parse_time(value: &str) -> Result<u16, ConfigError> {
    let Some((hours, minutes)) = value.split_once(':') else {
        return invalid(format!("{value:?} is not HH:MM"));
    };
    let hours: u16 = hours
        .parse()
        .map_err(|_| ConfigError::Invalid(format!("{value:?} is not HH:MM")))?;
    let minutes: u16 = minutes
        .parse()
        .map_err(|_| ConfigError::Invalid(format!("{value:?} is not HH:MM")))?;
    if hours > 23 || minutes > 59 {
        return invalid(format!("{value:?} is not a time of day"));
    }
    Ok(hours * 60 + minutes)
}
fn invalid<T>(detail: impl Into<String>) -> Result<T, ConfigError> {
    Err(ConfigError::Invalid(detail.into()))
}
