use crate::config::{Config, Entry, Playlist};
use crate::protocol::{Request, Response};
use crate::renderer::WallpaperDriver;
use crate::schedule;
use chrono::NaiveDateTime;
use serde::Serialize;
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
}

pub struct Runtime<D: WallpaperDriver> {
    config_path: PathBuf,
    config: Config,
    driver: D,
    manual_playlist: Option<String>,
    active_playlist: String,
    order: Vec<usize>,
    cursor: usize,
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
        let active = schedule::resolve(&config.schedules, &config.default_playlist, at)
            .map_err(|error| error.to_string())?
            .to_string();
        let shuffle = config.settings.shuffle;
        let mut runtime = Self {
            config_path,
            config,
            driver,
            manual_playlist: None,
            active_playlist: active,
            order: vec![],
            cursor: 0,
            paused: false,
            shuffle,
            rng: XorShift64::seeded(),
            last_cycle: Instant::now(),
            last_error: String::new(),
            quit: false,
        };
        runtime.rebuild_order(None)?;
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
            "previous" | "prev" => self.move_by(-1),
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
            if let Ok(wanted) =
                schedule::resolve(&self.config.schedules, &self.config.default_playlist, at)
            {
                if wanted != self.active_playlist {
                    self.active_playlist = wanted.to_string();
                    if self.rebuild_order(None).is_ok() {
                        let _ = self.apply_current();
                    }
                }
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
        let entry = self
            .current_entry()
            .cloned()
            .ok_or("active playlist is empty")?;
        let result = self.driver.apply(&entry, "", &self.config.settings);
        match result {
            Ok(()) => {
                self.last_error.clear();
                Ok(format!("playing {}", entry.id))
            }
            Err(error) => {
                self.last_error = error.clone();
                Err(error)
            }
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
        self.manual_playlist = Some(playlist.id.clone());
        self.active_playlist = playlist.id.clone();
        self.rebuild_order(None)?;
        self.apply_current()
    }

    fn follow_schedule(&mut self, at: NaiveDateTime) -> Result<String, String> {
        self.manual_playlist = None;
        self.active_playlist =
            schedule::resolve(&self.config.schedules, &self.config.default_playlist, at)
                .map_err(|error| error.to_string())?
                .to_string();
        self.rebuild_order(None)?;
        self.apply_current()
    }

    fn set_shuffle(&mut self, value: Option<&str>) -> Result<String, String> {
        self.shuffle = match value
            .unwrap_or("toggle")
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "on" | "true" | "1" => true,
            "off" | "false" | "0" => false,
            "toggle" => !self.shuffle,
            _ => return Err("usage: shuffle on|off".into()),
        };
        let current = self.current_entry().map(|entry| entry.id.clone());
        self.rebuild_order(current.as_deref())?;
        Ok(format!(
            "shuffle {}",
            if self.shuffle { "on" } else { "off" }
        ))
    }

    fn move_by(&mut self, delta: isize) -> Result<String, String> {
        if self.order.is_empty() {
            return Err("active playlist is empty".into());
        }
        let len = self.order.len() as isize;
        self.cursor = ((self.cursor as isize + delta).rem_euclid(len)) as usize;
        self.apply_current()
    }

    fn random_entry(&mut self) -> Result<String, String> {
        if self.order.is_empty() {
            return Err("active playlist is empty".into());
        }
        if self.order.len() > 1 {
            let old = self.cursor;
            while self.cursor == old {
                self.cursor = self.rng.index(self.order.len());
            }
        }
        self.apply_current()
    }

    fn reload(&mut self, at: NaiveDateTime) -> Result<String, String> {
        let next = Config::load(&self.config_path).map_err(|error| error.to_string())?;
        let old_entry = self.current_entry().map(|entry| entry.id.clone());
        self.driver.reconfigure(next.renderer.clone());
        self.config = next;
        self.shuffle = self.config.settings.shuffle;
        if let Some(manual) = &self.manual_playlist {
            if self.config.playlist(manual).is_none() {
                self.manual_playlist = None;
            }
        }
        self.active_playlist = if let Some(manual) = &self.manual_playlist {
            manual.clone()
        } else {
            schedule::resolve(&self.config.schedules, &self.config.default_playlist, at)
                .map_err(|error| error.to_string())?
                .to_string()
        };
        self.rebuild_order(old_entry.as_deref())?;
        self.apply_current()?;
        Ok("reloaded".into())
    }

    fn playlist(&self) -> Result<&Playlist, String> {
        self.config
            .playlist(&self.active_playlist)
            .ok_or_else(|| format!("active playlist {:?} is missing", self.active_playlist))
    }

    fn current_entry(&self) -> Option<&Entry> {
        let playlist = self.playlist().ok()?;
        self.order
            .get(self.cursor)
            .and_then(|index| playlist.entries.get(*index))
    }

    fn rebuild_order(&mut self, keep_entry: Option<&str>) -> Result<(), String> {
        let entries = self.playlist()?.entries.len();
        self.order = (0..entries).collect();
        if self.shuffle {
            self.rng.shuffle(&mut self.order);
        }
        self.cursor = keep_entry
            .and_then(|id| {
                let playlist = self.playlist().ok()?;
                self.order
                    .iter()
                    .position(|index| playlist.entries[*index].id == id)
            })
            .unwrap_or(0);
        Ok(())
    }

    fn status_json(&self) -> Result<String, String> {
        let playlist = self.playlist()?;
        let entry = self.current_entry();
        let kind = entry.map(|entry| match entry.kind {
            crate::config::EntryKind::Still => "still",
            crate::config::EntryKind::Video => "video",
            crate::config::EntryKind::Scene => "scene",
        });
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
        })
        .map_err(|error| error.to_string())
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
