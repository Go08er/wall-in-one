use serde::Deserialize;
use std::collections::HashSet;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

pub const SCHEMA_VERSION: u32 = 1;
pub const MAX_CONFIG_BYTES: u64 = 8 * 1024 * 1024;

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
    pub schema_version: u32,
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
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RendererSettings {
    pub noctalia_program: PathBuf,
    pub mpvpaper_program: PathBuf,
    pub linux_wallpaperengine_program: PathBuf,
    pub own_scene_renderer: bool,
    pub layer: String,
    pub video_when_hidden: VideoWhenHidden,
    pub video_hardware_decode: bool,
    pub video_muted: bool,
    pub video_volume: u8,
    pub scene_fps: u16,
    pub scene_muted: bool,
    pub scene_volume: u8,
    pub scene_pause_when_covered: bool,
    pub scene_scaling: String,
    pub scene_clamp: String,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum VideoWhenHidden {
    Pause,
    Stop,
    Play,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Playlist {
    pub id: String,
    pub name: String,
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
    pub palette: Palette,
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
}

fn default_true() -> bool {
    true
}

impl Config {
    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let metadata = fs::metadata(path).map_err(ConfigError::Io)?;
        if !metadata.is_file() {
            return invalid("config path is not a regular file");
        }
        if metadata.len() > MAX_CONFIG_BYTES {
            return Err(ConfigError::TooLarge(metadata.len()));
        }
        let text = fs::read_to_string(path).map_err(ConfigError::Io)?;
        let config: Self = toml::from_str(&text).map_err(ConfigError::Decode)?;
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.schema_version != SCHEMA_VERSION {
            return invalid(format!(
                "schema_version {} is unsupported; expected {SCHEMA_VERSION}",
                self.schema_version
            ));
        }
        if self.settings.cycle_interval_seconds < 5 {
            return invalid("cycle_interval_seconds must be at least 5");
        }
        for (label, path) in [
            ("noctalia_program", &self.renderer.noctalia_program),
            ("mpvpaper_program", &self.renderer.mpvpaper_program),
            (
                "linux_wallpaperengine_program",
                &self.renderer.linux_wallpaperengine_program,
            ),
        ] {
            absolute(label, path)?;
        }

        let mut ids = HashSet::new();
        let mut names = HashSet::new();
        for playlist in &self.playlists {
            nonempty("playlist id", &playlist.id)?;
            nonempty("playlist name", &playlist.name)?;
            if !ids.insert(playlist.id.as_str()) {
                return invalid(format!("duplicate playlist id {:?}", playlist.id));
            }
            if !names.insert(playlist.name.as_str()) {
                return invalid(format!("duplicate playlist name {:?}", playlist.name));
            }
            let mut entry_ids = HashSet::new();
            for entry in &playlist.entries {
                nonempty("entry id", &entry.id)?;
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
                        Some(id) if !id.is_empty() && id.bytes().all(|b| b.is_ascii_digit()) => {}
                        _ => return invalid("scene entry needs a numeric scene_id"),
                    },
                    EntryKind::Still => {
                        return invalid("still entry must not carry motion or scene_id")
                    }
                    EntryKind::Video => return invalid("video entry must not carry scene_id"),
                    EntryKind::Scene => return invalid("scene entry must not carry motion"),
                }
                match &entry.palette {
                    Palette::Keep { .. } => {}
                    Palette::Adaptive { scheme, .. } => nonempty("adaptive scheme", scheme)?,
                    Palette::Named { name, .. } => nonempty("palette name", name)?,
                }
            }
        }
        if self.playlists.is_empty() {
            return invalid("at least one playlist is required");
        }
        reference(&self.default_playlist, &ids, &names)?;
        for rule in &self.schedules {
            nonempty("schedule id", &rule.id)?;
            reference(&rule.playlist, &ids, &names)?;
            if rule.months.iter().any(|m| !(1..=12).contains(m)) {
                return invalid(format!("schedule {:?} has an invalid month", rule.id));
            }
            if rule.weekdays.iter().any(|d| *d > 6) {
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
                    ))
                }
            }
        }
        let mut connectors = HashSet::new();
        for display in &self.displays {
            nonempty("display connector", &display.connector)?;
            if !connectors.insert(display.connector.as_str()) {
                return invalid(format!(
                    "duplicate display connector {:?}",
                    display.connector
                ));
            }
            reference(&display.playlist, &ids, &names)?;
        }
        Ok(())
    }

    pub fn playlist(&self, value: &str) -> Option<&Playlist> {
        self.playlists
            .iter()
            .find(|p| p.id == value || p.name == value)
    }
}

fn absolute(label: &str, path: &Path) -> Result<(), ConfigError> {
    if path.is_absolute() {
        Ok(())
    } else {
        invalid(format!(
            "{label} must be an absolute path: {}",
            path.display()
        ))
    }
}
fn nonempty(label: &str, value: &str) -> Result<(), ConfigError> {
    if !value.trim().is_empty() && !value.chars().any(char::is_control) {
        Ok(())
    } else {
        invalid(format!("{label} is empty or contains control characters"))
    }
}
fn reference(value: &str, ids: &HashSet<&str>, names: &HashSet<&str>) -> Result<(), ConfigError> {
    if ids.contains(value) || names.contains(value) {
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
