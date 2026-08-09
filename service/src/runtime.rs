use crate::config::{Config, Entry, Playlist};
use crate::protocol::{Request, Response};
use crate::renderer::WallpaperDriver;
use crate::schedule;
use chrono::NaiveDateTime;
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[derive(Debug, Serialize)]
pub struct Status<'a> {
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    pub source: &'a str,
    pub entry_id: Option<&'a str>,
    pub kind: Option<&'a str>,
    pub still: Option<String>,
    pub paused: bool,
    pub shuffle: bool,
    pub cycle_enabled: bool,
    pub last_error: &'a str,
    pub playlists: Vec<PlaylistStatus<'a>>,
    pub displays: Vec<DisplayStatus<'a>>,
}

#[derive(Debug, Serialize)]
pub struct PlaylistStatus<'a> {
    pub id: &'a str,
    pub name: &'a str,
    pub entries: usize,
    pub active: bool,
}

#[derive(Debug, Serialize)]
pub struct DisplayStatus<'a> {
    pub connector: &'a str,
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    pub entry_id: &'a str,
    pub kind: &'a str,
    pub still: String,
}

#[derive(Debug)]
struct PlaylistCursor {
    order: Vec<usize>,
    position: usize,
}

pub struct Runtime<D: WallpaperDriver> {
    config_path: PathBuf,
    config: Config,
    driver: D,
    manual_playlist: Option<String>,
    active_playlist: String,
    schedule_overrode_default: bool,
    cursors: HashMap<String, PlaylistCursor>,
    paused: bool,
    shuffle: bool,
    rng: XorShift64,
    last_cycle: Instant,
    last_error: String,
    quit: bool,
}

impl<D: WallpaperDriver> Runtime<D> {
    pub fn new(
        config_path: PathBuf,
        config: Config,
        driver: D,
        at: NaiveDateTime,
    ) -> Result<Self, String> {
        let scheduled =
            schedule::resolve_override(&config.schedules, at).map_err(|error| error.to_string())?;
        let schedule_overrode_default = scheduled.is_some();
        let active = scheduled.unwrap_or(&config.default_playlist).to_string();
        let shuffle = config.settings.shuffle;
        let mut runtime = Self {
            config_path,
            config,
            driver,
            manual_playlist: None,
            active_playlist: active,
            schedule_overrode_default,
            cursors: HashMap::new(),
            paused: false,
            shuffle,
            rng: XorShift64::seeded(),
            last_cycle: Instant::now(),
            last_error: String::new(),
            quit: false,
        };
        runtime.rebuild_cursors(&HashMap::new())?;
        Ok(runtime)
    }

    pub fn should_quit(&self) -> bool {
        self.quit
    }

    pub fn shutdown(&mut self) {
        self.driver.stop();
        self.quit = true;
    }

    pub fn handle(&mut self, request: Request, at: NaiveDateTime) -> Response {
        let result = match request.verb.as_str() {
            "playlist-use" => self.use_playlist(request.argument.as_deref()),
            "schedule-follow" => self.follow_schedule(at),
            "play" => {
                self.paused = false;
                self.driver.set_paused(false);
                Ok("playing".into())
            }
            "pause" => {
                self.paused = true;
                self.driver.set_paused(true);
                Ok("paused".into())
            }
            "toggle" => {
                self.paused = !self.paused;
                self.driver.set_paused(self.paused);
                Ok(if self.paused {
                    "paused".into()
                } else {
                    "playing".into()
                })
            }
            "shuffle" => self.set_shuffle(request.argument.as_deref()),
            "next" => self.move_by(1),
            "previous" => self.move_by(-1),
            "random" => self.random_entry(),
            "status" => {
                return match self.status_json() {
                    Ok(status) => Response::success(status),
                    Err(error) => Response::failure(error),
                }
            }
            "reload" => self.reload(at),
            "quit" => {
                self.quit = true;
                self.driver.stop();
                Ok("quitting".into())
            }
            _ => Err(format!("unknown runtime verb {:?}", request.verb)),
        };
        match result {
            Ok(message) => Response::success(message),
            Err(error) => Response::failure(error),
        }
    }

    pub fn tick(&mut self, at: NaiveDateTime, now: Instant) {
        if self.manual_playlist.is_none() {
            if let Ok(scheduled) = schedule::resolve_override(&self.config.schedules, at) {
                let overrode = scheduled.is_some();
                let wanted = scheduled
                    .unwrap_or(&self.config.default_playlist)
                    .to_string();
                if wanted != self.active_playlist {
                    self.active_playlist = wanted;
                    self.reset_cursor(&self.active_playlist.clone());
                    let _ = self.apply_current();
                }
                self.schedule_overrode_default = overrode;
            }
        }
        if !self.paused
            && self.config.settings.cycle_enabled
            && now.duration_since(self.last_cycle)
                >= Duration::from_secs(self.config.settings.cycle_interval_seconds)
        {
            let _ = self.move_by(1);
            self.last_cycle = now;
        }
    }

    pub fn apply_current(&mut self) -> Result<String, String> {
        let mut targets = Vec::new();
        if self.config.displays.is_empty() {
            if let Some(entry) = self.current_entry_for(&self.active_playlist).cloned() {
                targets.push((entry, String::new()));
            }
        } else {
            for display in &self.config.displays {
                let reference = if self.manual_playlist.is_some() || self.schedule_overrode_default
                {
                    &self.active_playlist
                } else {
                    &display.playlist
                };
                if let Some(entry) = self.current_entry_for(reference).cloned() {
                    targets.push((entry, display.connector.clone()));
                }
            }
        }
        if targets.is_empty() {
            return self.fail("active display playlists are empty");
        }
        let played = targets[0].0.id.clone();
        let mut errors = Vec::new();
        for (entry, output) in targets {
            if let Err(error) = self.driver.apply(&entry, &output, &self.config.settings) {
                errors.push(if output.is_empty() {
                    error
                } else {
                    format!("{output}: {error}")
                });
            }
        }
        if errors.is_empty() {
            self.last_error.clear();
            Ok(format!("playing {played}"))
        } else {
            let error = errors.join("; ");
            self.last_error = error.clone();
            Err(error)
        }
    }

    fn use_playlist(&mut self, value: Option<&str>) -> Result<String, String> {
        let reference = value
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or("usage: playlist-use <name>")?;
        let playlist = self
            .config
            .playlist(reference)
            .ok_or_else(|| format!("no such playlist {reference:?}"))?;
        let playlist_id = playlist.id.clone();
        self.manual_playlist = Some(playlist_id.clone());
        self.active_playlist = playlist_id.clone();
        self.schedule_overrode_default = false;
        self.reset_cursor(&playlist_id);
        self.apply_current()
    }

    fn follow_schedule(&mut self, at: NaiveDateTime) -> Result<String, String> {
        self.manual_playlist = None;
        let scheduled = schedule::resolve_override(&self.config.schedules, at)
            .map_err(|error| error.to_string())?;
        self.schedule_overrode_default = scheduled.is_some();
        self.active_playlist = scheduled
            .unwrap_or(&self.config.default_playlist)
            .to_string();
        self.reset_cursor(&self.active_playlist.clone());
        self.apply_current()
    }

    fn set_shuffle(&mut self, value: Option<&str>) -> Result<String, String> {
        self.shuffle = match value
            .ok_or("usage: shuffle on|off")?
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "on" | "true" | "1" => true,
            "off" | "false" | "0" => false,
            _ => return Err("usage: shuffle on|off".into()),
        };
        let current = self.current_entry_ids();
        self.rebuild_cursors(&current)?;
        Ok(format!(
            "shuffle {}",
            if self.shuffle { "on" } else { "off" }
        ))
    }

    fn move_by(&mut self, delta: isize) -> Result<String, String> {
        let mut moved = false;
        for playlist in self.effective_playlist_ids() {
            if let Some(cursor) = self.cursors.get_mut(&playlist) {
                if !cursor.order.is_empty() {
                    let len = cursor.order.len() as isize;
                    cursor.position = ((cursor.position as isize + delta).rem_euclid(len)) as usize;
                    moved = true;
                }
            }
        }
        if !moved {
            return self.fail("active display playlists are empty");
        }
        self.apply_current()
    }

    fn random_entry(&mut self) -> Result<String, String> {
        let mut moved = false;
        for playlist in self.effective_playlist_ids() {
            if let Some(cursor) = self.cursors.get_mut(&playlist) {
                if cursor.order.is_empty() {
                    continue;
                }
                if cursor.order.len() > 1 {
                    let old = cursor.position;
                    while cursor.position == old {
                        cursor.position = self.rng.index(cursor.order.len());
                    }
                }
                moved = true;
            }
        }
        if !moved {
            return self.fail("active display playlists are empty");
        }
        self.apply_current()
    }

    fn reload(&mut self, at: NaiveDateTime) -> Result<String, String> {
        let next = Config::load(&self.config_path).map_err(|error| error.to_string())?;
        let old_entries = self.current_entry_ids();
        self.driver.reconfigure(next.renderer.clone());
        self.config = next;
        self.shuffle = self.config.settings.shuffle;
        if let Some(manual) = &self.manual_playlist {
            if self.config.playlist(manual).is_none() {
                self.manual_playlist = None;
            }
        }
        self.active_playlist = if let Some(manual) = &self.manual_playlist {
            self.schedule_overrode_default = false;
            manual.clone()
        } else {
            let scheduled = schedule::resolve_override(&self.config.schedules, at)
                .map_err(|error| error.to_string())?;
            self.schedule_overrode_default = scheduled.is_some();
            scheduled
                .unwrap_or(&self.config.default_playlist)
                .to_string()
        };
        self.rebuild_cursors(&old_entries)?;
        self.apply_current()?;
        Ok("reloaded".into())
    }

    fn playlist(&self) -> Result<&Playlist, String> {
        self.config
            .playlist(&self.active_playlist)
            .ok_or_else(|| format!("active playlist {:?} is missing", self.active_playlist))
    }

    fn current_entry(&self) -> Option<&Entry> {
        self.current_entry_for(&self.active_playlist)
    }

    fn current_entry_for(&self, reference: &str) -> Option<&Entry> {
        let playlist = self.config.playlist(reference)?;
        let cursor = self.cursors.get(&playlist.id)?;
        cursor
            .order
            .get(cursor.position)
            .and_then(|index| playlist.entries.get(*index))
    }

    fn current_entry_ids(&self) -> HashMap<String, String> {
        self.config
            .playlists
            .iter()
            .filter_map(|playlist| {
                self.current_entry_for(&playlist.id)
                    .map(|entry| (playlist.id.clone(), entry.id.clone()))
            })
            .collect()
    }

    fn rebuild_cursors(&mut self, keep_entries: &HashMap<String, String>) -> Result<(), String> {
        let specifications: Vec<_> = self
            .config
            .playlists
            .iter()
            .map(|playlist| {
                (
                    playlist.id.clone(),
                    playlist.entries.len(),
                    playlist
                        .entries
                        .iter()
                        .map(|entry| entry.id.clone())
                        .collect::<Vec<_>>(),
                )
            })
            .collect();
        let mut cursors = HashMap::new();
        for (id, entries, entry_ids) in specifications {
            let mut order: Vec<usize> = (0..entries).collect();
            if self.shuffle {
                self.rng.shuffle(&mut order);
            }
            let position = keep_entries
                .get(&id)
                .and_then(|wanted| order.iter().position(|index| entry_ids[*index] == *wanted))
                .unwrap_or(0);
            cursors.insert(id, PlaylistCursor { order, position });
        }
        self.cursors = cursors;
        self.playlist()?;
        Ok(())
    }

    fn reset_cursor(&mut self, reference: &str) {
        if let Some(playlist) = self.config.playlist(reference) {
            if let Some(cursor) = self.cursors.get_mut(&playlist.id) {
                cursor.position = 0;
            }
        }
    }

    fn effective_playlist_ids(&self) -> Vec<String> {
        if self.config.displays.is_empty()
            || self.manual_playlist.is_some()
            || self.schedule_overrode_default
        {
            return vec![self.active_playlist.clone()];
        }
        let mut seen = HashSet::new();
        self.config
            .displays
            .iter()
            .filter_map(|display| {
                let playlist = self.config.playlist(&display.playlist)?;
                if seen.insert(playlist.id.clone()) {
                    Some(playlist.id.clone())
                } else {
                    None
                }
            })
            .collect()
    }

    fn fail<T>(&mut self, error: impl Into<String>) -> Result<T, String> {
        let error = error.into();
        self.last_error = error.clone();
        Err(error)
    }

    fn status_json(&self) -> Result<String, String> {
        let playlist = self.playlist()?;
        let entry = self.current_entry();
        let kind = entry.map(|entry| entry_kind(entry.kind));
        let active_ids: HashSet<_> = self.effective_playlist_ids().into_iter().collect();
        let playlists = self
            .config
            .playlists
            .iter()
            .map(|playlist| PlaylistStatus {
                id: &playlist.id,
                name: &playlist.name,
                entries: playlist.entries.len(),
                active: active_ids.contains(&playlist.id),
            })
            .collect();
        let mut displays = Vec::new();
        if self.config.displays.is_empty() {
            if let Some(entry) = entry {
                displays.push(DisplayStatus {
                    connector: "ALL",
                    playlist_id: &playlist.id,
                    playlist: &playlist.name,
                    entry_id: &entry.id,
                    kind: entry_kind(entry.kind),
                    still: entry.still.display().to_string(),
                });
            }
        } else {
            for display in &self.config.displays {
                let reference = if self.manual_playlist.is_some() || self.schedule_overrode_default
                {
                    &self.active_playlist
                } else {
                    &display.playlist
                };
                let effective = self.config.playlist(reference).ok_or_else(|| {
                    format!("display {} names a missing playlist", display.connector)
                })?;
                if let Some(entry) = self.current_entry_for(&effective.id) {
                    displays.push(DisplayStatus {
                        connector: &display.connector,
                        playlist_id: &effective.id,
                        playlist: &effective.name,
                        entry_id: &entry.id,
                        kind: entry_kind(entry.kind),
                        still: entry.still.display().to_string(),
                    });
                }
            }
        }
        serde_json::to_string(&Status {
            playlist_id: &playlist.id,
            playlist: &playlist.name,
            source: if self.manual_playlist.is_some() {
                "manual"
            } else {
                "schedule"
            },
            entry_id: entry.map(|entry| entry.id.as_str()),
            kind,
            still: entry.map(|entry| entry.still.display().to_string()),
            paused: self.paused,
            shuffle: self.shuffle,
            cycle_enabled: self.config.settings.cycle_enabled,
            last_error: &self.last_error,
            playlists,
            displays,
        })
        .map_err(|error| error.to_string())
    }
}

fn entry_kind(kind: crate::config::EntryKind) -> &'static str {
    match kind {
        crate::config::EntryKind::Still => "still",
        crate::config::EntryKind::Video => "video",
        crate::config::EntryKind::Scene => "scene",
    }
}

struct XorShift64(u64);
impl XorShift64 {
    fn seeded() -> Self {
        let seed = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_nanos() as u64)
            .unwrap_or(0x5eed);
        Self(if seed == 0 { 0x5eed } else { seed })
    }
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn index(&mut self, len: usize) -> usize {
        (self.next() as usize) % len
    }
    fn shuffle(&mut self, values: &mut [usize]) {
        for index in (1..values.len()).rev() {
            let chosen = self.index(index + 1);
            values.swap(index, chosen);
        }
    }
}

#[allow(dead_code)]
fn _is_absolute(path: &Path) -> bool {
    path.is_absolute()
}
