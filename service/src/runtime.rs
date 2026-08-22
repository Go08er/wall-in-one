use crate::config::{Config, Entry, Playlist, ScheduleRule};
use crate::protocol::{Request, Response};
use crate::renderer::{WallpaperDriver, MAX_OUTPUT_NAME_BYTES};
use crate::schedule;
use chrono::NaiveDateTime;
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const OUTPUT_PROBE_INTERVAL: Duration = Duration::from_secs(5);
const MAX_LAST_ERROR_BYTES: usize = 12 * 1024;
const MAX_OUTPUT_DISCOVERY_ERROR_BYTES: usize = 4 * 1024;
const TRUNCATION_MARKER: &str = " ... [truncated] ... ";

fn clean_error(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if character.is_control() {
                ' '
            } else {
                character
            }
        })
        .collect()
}

fn truncate_middle(value: &str, maximum_bytes: usize) -> String {
    let clean = clean_error(value);
    if clean.len() <= maximum_bytes {
        return clean;
    }
    if maximum_bytes <= TRUNCATION_MARKER.len() {
        return "[truncated]".chars().take(maximum_bytes).collect();
    }
    let content_bytes = maximum_bytes - TRUNCATION_MARKER.len();
    let prefix_bytes = content_bytes * 3 / 5;
    let suffix_bytes = content_bytes - prefix_bytes;
    let mut prefix_end = prefix_bytes;
    while !clean.is_char_boundary(prefix_end) {
        prefix_end -= 1;
    }
    let mut suffix_start = clean.len() - suffix_bytes;
    while !clean.is_char_boundary(suffix_start) {
        suffix_start += 1;
    }
    format!(
        "{}{}{}",
        &clean[..prefix_end],
        TRUNCATION_MARKER,
        &clean[suffix_start..]
    )
}

fn bounded_failure_summary(failures: &[String]) -> String {
    if failures.is_empty() {
        return String::new();
    }
    let separators = failures.len().saturating_sub(1).saturating_mul(2);
    let each = MAX_LAST_ERROR_BYTES
        .saturating_sub(separators)
        .checked_div(failures.len())
        .unwrap_or(0);
    failures
        .iter()
        .map(|failure| truncate_middle(failure, each))
        .collect::<Vec<_>>()
        .join("; ")
}

#[derive(Debug, Serialize)]
pub struct Status<'a> {
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    pub source: &'a str,
    pub entry_id: Option<&'a str>,
    pub kind: Option<&'a str>,
    pub still: Option<String>,
    pub motion_active: Option<bool>,
    pub playback_state: &'a str,
    pub paused: bool,
    pub stopped: bool,
    pub shuffle: bool,
    pub shuffle_default: bool,
    pub shuffle_source: &'a str,
    pub cycle_enabled: bool,
    pub cycle_default: bool,
    pub cycle_source: &'a str,
    pub last_error: &'a str,
    pub output_discovery_error: &'a str,
    pub playlists: Vec<PlaylistStatus<'a>>,
    pub schedule: ScheduleStatus<'a>,
    pub schedules: Vec<ScheduleRuleStatus<'a>>,
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
pub struct ScheduleStatus<'a> {
    pub following: bool,
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    pub rule_id: Option<&'a str>,
}

#[derive(Debug, Serialize)]
pub struct ScheduleRuleStatus<'a> {
    pub id: &'a str,
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    pub months: &'a [u8],
    pub weekdays: &'a [u8],
    pub start: Option<&'a str>,
    pub end: Option<&'a str>,
    pub enabled: bool,
    pub selected: bool,
    pub in_force: bool,
}

#[derive(Debug, Serialize)]
pub struct DisplayStatus<'a> {
    pub connector: &'a str,
    pub assignment_source: &'a str,
    pub assigned_playlist_id: &'a str,
    pub assigned_playlist: &'a str,
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    pub entry_id: &'a str,
    pub kind: &'a str,
    pub still: String,
    pub motion_active: bool,
}

#[derive(Debug)]
struct PlaylistCursor {
    order: Vec<usize>,
    position: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PlaybackState {
    Playing,
    Paused,
    Stopped,
}

pub struct Runtime<D: WallpaperDriver> {
    config_path: PathBuf,
    config: Config,
    driver: D,
    manual_playlist: Option<String>,
    active_playlist: String,
    schedule_overrode_default: bool,
    cursors: HashMap<String, PlaylistCursor>,
    playback_state: PlaybackState,
    shuffle_override: Option<bool>,
    cycle_override: Option<bool>,
    renderer_failed: bool,
    /// Connectors used by the most recent apply. A blank connector is the
    /// renderer's efficient all-output target when there are no explicit
    /// assignments.
    target_outputs: Vec<String>,
    /// Last successful live compositor snapshot. This remains available as a
    /// bounded fallback across a transient niri failure.
    live_outputs: Option<Vec<String>>,
    output_discovery_error: String,
    last_output_probe: Instant,
    rng: XorShift64,
    last_cycle: Instant,
    last_error: String,
    authoritative_generation: u64,
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
        let target_outputs = if config.displays.is_empty() {
            vec![String::new()]
        } else {
            config
                .displays
                .iter()
                .map(|display| display.connector.clone())
                .collect()
        };
        let mut runtime = Self {
            config_path,
            config,
            driver,
            manual_playlist: None,
            active_playlist: active,
            schedule_overrode_default,
            cursors: HashMap::new(),
            playback_state: PlaybackState::Playing,
            shuffle_override: None,
            cycle_override: None,
            renderer_failed: false,
            target_outputs,
            live_outputs: None,
            output_discovery_error: String::new(),
            last_output_probe: Instant::now(),
            rng: XorShift64::seeded(),
            last_cycle: Instant::now(),
            last_error: String::new(),
            authoritative_generation: 0,
            quit: false,
        };
        runtime.rebuild_cursors(&HashMap::new())?;
        Ok(runtime)
    }

    pub fn should_quit(&self) -> bool {
        self.quit
    }

    /// Changes when a successful apply or terminal command supersedes a
    /// pending automatic startup apply. Pause and stop deliberately do not:
    /// they shape that eventual apply into paused motion or a still-only one.
    pub fn authoritative_generation(&self) -> u64 {
        self.authoritative_generation
    }

    fn supersede_startup_apply(&mut self) {
        self.authoritative_generation = self.authoritative_generation.wrapping_add(1);
    }

    pub fn shutdown(&mut self) {
        self.driver.stop();
        self.quit = true;
    }

    pub fn handle(&mut self, request: Request, at: NaiveDateTime) -> Response {
        let no_argument = |usage: &str| {
            if request.argument.is_some() {
                Err(format!("usage: {usage}"))
            } else {
                Ok(())
            }
        };
        let result = match request.verb.as_str() {
            "playlist-use" => self.use_playlist(request.argument.as_deref()),
            "schedule-follow" => {
                no_argument("schedule-follow").and_then(|()| self.follow_schedule(at))
            }
            "play" => no_argument("play").and_then(|()| self.play()),
            "pause" => no_argument("pause").and_then(|()| self.pause()),
            "toggle" => no_argument("toggle").and_then(|()| self.toggle()),
            "stop" => no_argument("stop").and_then(|()| self.stop_motion()),
            "shuffle" => self.set_shuffle(request.argument.as_deref()),
            "cycle" => self.set_cycle(request.argument.as_deref()),
            "next" => no_argument("next").and_then(|()| self.move_by(1)),
            "previous" => no_argument("previous").and_then(|()| self.move_by(-1)),
            "random" => no_argument("random").and_then(|()| self.random_entry()),
            "status" => {
                if let Err(error) = no_argument("status") {
                    return Response::failure(error);
                }
                return match self.status_json(at) {
                    Ok(status) => Response::success(status),
                    Err(error) => Response::failure(error),
                };
            }
            "reload" => no_argument("reload").and_then(|()| self.reload(at)),
            "quit" => no_argument("quit").map(|()| {
                self.supersede_startup_apply();
                self.quit = true;
                self.driver.stop();
                "quitting".into()
            }),
            _ => Err(format!("unknown runtime verb {:?}", request.verb)),
        };
        match result {
            Ok(message) => Response::success(message),
            Err(error) => Response::failure(error),
        }
    }

    pub fn tick(&mut self, at: NaiveDateTime, now: Instant) {
        let failures = self.driver.poll_failures();
        if !failures.is_empty() {
            self.last_error = bounded_failure_summary(&failures);
            // Videos remain retryable: unlike scenes, SystemDriver does not
            // suppress a video after it exits.  Remember that Play has real
            // work to do instead of treating the still fallback as healthy
            // playback.  Scene retries still reach SystemDriver's session
            // suppression and fail with the attributable scene diagnostic.
            self.renderer_failed = true;
        }
        if self.manual_playlist.is_none() {
            if let Ok(scheduled) = schedule::resolve_override(&self.config.schedules, at) {
                let overrode = scheduled.is_some();
                let wanted = scheduled
                    .unwrap_or(&self.config.default_playlist)
                    .to_string();
                let playlist_changed = wanted != self.active_playlist;
                let routing_changed = overrode != self.schedule_overrode_default;
                self.schedule_overrode_default = overrode;
                if playlist_changed {
                    self.active_playlist = wanted;
                    self.reset_cursor(&self.active_playlist.clone());
                }
                if playlist_changed || routing_changed {
                    let _ = self.apply_current();
                }
            }
        }
        if self.playback_state != PlaybackState::Paused
            && self.cycle_enabled()
            && now.duration_since(self.last_cycle)
                >= Duration::from_secs(self.config.settings.cycle_interval_seconds)
        {
            let _ = self.move_by(1);
            self.last_cycle = now;
        }
        if now.saturating_duration_since(self.last_output_probe) >= OUTPUT_PROBE_INTERVAL {
            self.last_output_probe = now;
            self.driver.begin_apply();
            match self.probe_outputs() {
                Ok(outputs) if self.live_outputs.as_ref() != Some(&outputs) => {
                    // The discovery call populated the driver's batch snapshot;
                    // reuse it for this hand-over instead of asking niri twice.
                    let _ = self.apply_current_in_batch(Ok(outputs));
                }
                Ok(outputs) => {
                    self.live_outputs = Some(outputs);
                    self.output_discovery_error.clear();
                }
                Err(error) => {
                    self.output_discovery_error =
                        truncate_middle(&error, MAX_OUTPUT_DISCOVERY_ERROR_BYTES)
                }
            }
            self.driver.end_apply();
        }
    }

    pub fn apply_current(&mut self) -> Result<String, String> {
        self.last_output_probe = Instant::now();
        self.driver.begin_apply();
        let discovered = self.probe_outputs();
        let result = self.apply_current_in_batch(discovered);
        self.driver.end_apply();
        result
    }

    fn probe_outputs(&mut self) -> Result<Vec<String>, String> {
        let mut outputs = self.driver.connected_outputs()?;
        outputs.retain(|output| {
            !output.is_empty()
                && output.len() <= MAX_OUTPUT_NAME_BYTES
                && !output.chars().any(char::is_control)
        });
        outputs.sort();
        outputs.dedup();
        if outputs.is_empty() {
            Err("live output discovery returned no usable connectors".into())
        } else {
            Ok(outputs)
        }
    }

    fn apply_current_in_batch(
        &mut self,
        discovered: Result<Vec<String>, String>,
    ) -> Result<String, String> {
        self.target_outputs = match discovered {
            Ok(outputs) => {
                self.live_outputs = Some(outputs.clone());
                self.output_discovery_error.clear();
                if self.config.displays.is_empty() {
                    vec![String::new()]
                } else {
                    outputs
                }
            }
            Err(error) => {
                self.output_discovery_error =
                    truncate_middle(&error, MAX_OUTPUT_DISCOVERY_ERROR_BYTES);
                if self.config.displays.is_empty() {
                    vec![String::new()]
                } else if let Some(outputs) = &self.live_outputs {
                    outputs.clone()
                } else {
                    // niri may not be ready during login. Preserve the former
                    // explicit-assignment behaviour until a live snapshot is
                    // available; unassigned outputs join on the first probe.
                    self.config
                        .displays
                        .iter()
                        .map(|display| display.connector.clone())
                        .collect()
                }
            }
        };
        let targets = self.current_targets(&self.target_outputs);
        let outputs: Vec<String> = targets.iter().map(|(_, output)| output.clone()).collect();
        self.driver.retain_outputs(&outputs);
        if targets.is_empty() {
            return self.fail("active display playlists are empty");
        }
        let played = targets[0].0.id.clone();
        let mut errors = Vec::new();
        let mut settings = self.config.settings.clone();
        if self.playback_state == PlaybackState::Stopped {
            settings.dynamics_enabled = false;
        }
        for (entry, output) in targets {
            if let Err(error) = self.driver.apply(&entry, &output, &settings) {
                errors.push(if output.is_empty() {
                    error
                } else {
                    format!("{output}: {error}")
                });
            }
        }
        if self.playback_state == PlaybackState::Paused {
            if let Err(error) = self.driver.set_paused(true) {
                // The newly started renderer is not paused. Do not leave status
                // claiming otherwise. Undo any partial multi-output pause on a
                // best-effort basis before reporting the renderer as playing.
                self.playback_state = PlaybackState::Playing;
                let rollback = self
                    .driver
                    .set_paused(false)
                    .err()
                    .map(|detail| format!("; resume rollback also failed: {detail}"))
                    .unwrap_or_default();
                errors.push(format!("could not pause renderer: {error}{rollback}"));
            }
        }
        if errors.is_empty() {
            self.supersede_startup_apply();
            self.last_error.clear();
            self.renderer_failed = false;
            Ok(match self.playback_state {
                PlaybackState::Playing => format!("playing {played}"),
                PlaybackState::Paused => format!("paused {played}"),
                PlaybackState::Stopped => format!("showing {played}; motion stopped"),
            })
        } else {
            let error = errors.join("; ");
            self.last_error = bounded_failure_summary(&errors);
            self.renderer_failed = true;
            Err(error)
        }
    }

    fn current_targets(&self, outputs: &[String]) -> Vec<(Entry, String)> {
        let mut targets = Vec::new();
        if self.config.displays.is_empty() {
            if let Some(entry) = self.current_entry_for(&self.active_playlist).cloned() {
                targets.push((entry, String::new()));
            }
        } else {
            for output in outputs {
                let reference = if self.manual_playlist.is_some() || self.schedule_overrode_default
                {
                    &self.active_playlist
                } else {
                    self.config
                        .displays
                        .iter()
                        .find(|display| display.connector == *output)
                        .map_or(self.config.default_playlist.as_str(), |display| {
                            display.playlist.as_str()
                        })
                };
                if let Some(entry) = self.current_entry_for(reference).cloned() {
                    targets.push((entry, output.clone()));
                }
            }
        }
        targets
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
        let before = self.shuffle_enabled();
        self.shuffle_override = match value
            .ok_or("usage: shuffle on|off|default")?
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "on" | "true" | "1" => Some(true),
            "off" | "false" | "0" => Some(false),
            "default" | "config" | "follow" => None,
            _ => return Err("usage: shuffle on|off|default".into()),
        };
        let enabled = self.shuffle_enabled();
        if enabled != before {
            let current = self.current_entry_ids();
            self.rebuild_cursors(&current)?;
        }
        Ok(format!(
            "shuffle {} ({})",
            if enabled { "on" } else { "off" },
            if self.shuffle_override.is_some() {
                "manual"
            } else {
                "config"
            }
        ))
    }

    fn shuffle_enabled(&self) -> bool {
        self.shuffle_override
            .unwrap_or(self.config.settings.shuffle)
    }

    fn cycle_enabled(&self) -> bool {
        self.cycle_override
            .unwrap_or(self.config.settings.cycle_enabled)
    }

    fn set_cycle(&mut self, value: Option<&str>) -> Result<String, String> {
        let before = self.cycle_enabled();
        self.cycle_override = match value
            .ok_or("usage: cycle on|off|default")?
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "on" | "true" | "1" => Some(true),
            "off" | "false" | "0" => Some(false),
            "default" | "config" | "follow" => None,
            _ => return Err("usage: cycle on|off|default".into()),
        };
        let enabled = self.cycle_enabled();
        if enabled && !before {
            // Time spent explicitly off is not a debt: turning cycling back on
            // starts a fresh interval instead of immediately skipping ahead.
            self.last_cycle = Instant::now();
        }
        Ok(format!(
            "cycle {} ({})",
            if enabled { "on" } else { "off" },
            if self.cycle_override.is_some() {
                "manual"
            } else {
                "config"
            }
        ))
    }

    fn play(&mut self) -> Result<String, String> {
        match self.playback_state {
            PlaybackState::Playing => {
                if self.renderer_failed {
                    self.apply_current()?;
                }
            }
            PlaybackState::Paused => {
                if let Err(error) = self.driver.set_paused(false) {
                    // A multi-output driver can fail after resuming an earlier
                    // child. Restore the prior state as far as possible so the
                    // unchanged Paused status remains truthful.
                    let rollback = self
                        .driver
                        .set_paused(true)
                        .err()
                        .map(|detail| format!("; pause rollback also failed: {detail}"))
                        .unwrap_or_default();
                    return Err(format!("could not resume renderer: {error}{rollback}"));
                }
                self.playback_state = PlaybackState::Playing;
            }
            PlaybackState::Stopped => {
                self.playback_state = PlaybackState::Playing;
                if let Err(error) = self.apply_current() {
                    // A failed resume did not restore motion. Keep status honest
                    // and retain the released/still-only state for another try.
                    self.playback_state = PlaybackState::Stopped;
                    return Err(error);
                }
            }
        }
        Ok("playing".into())
    }

    fn pause(&mut self) -> Result<String, String> {
        if self.playback_state == PlaybackState::Stopped {
            return Ok("stopped; use play to resume motion".into());
        }
        if let Err(error) = self.driver.set_paused(true) {
            // Restore any children already paused before one target failed.
            let rollback = self
                .driver
                .set_paused(false)
                .err()
                .map(|detail| format!("; resume rollback also failed: {detail}"))
                .unwrap_or_default();
            return Err(format!("could not pause renderer: {error}{rollback}"));
        }
        self.playback_state = PlaybackState::Paused;
        Ok("paused".into())
    }

    fn toggle(&mut self) -> Result<String, String> {
        if self.playback_state == PlaybackState::Playing {
            self.pause()
        } else {
            self.play()
        }
    }

    fn stop_motion(&mut self) -> Result<String, String> {
        if self.playback_state == PlaybackState::Paused {
            // linux-wallpaperengine is process-frozen for pause. Let it receive
            // SIGTERM rather than waiting for the supervisor's SIGKILL timeout.
            let _ = self.driver.set_paused(false);
        }
        self.driver.stop();
        self.playback_state = PlaybackState::Stopped;
        Ok("stopped; paired still remains active".into())
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
        let old_targets = self.current_targets(&self.target_outputs);
        let old_entries = self.current_entry_ids();
        let video_audio_changed = (next.renderer.video_muted, next.renderer.video_volume)
            != (
                self.config.renderer.video_muted,
                self.config.renderer.video_volume,
            );
        // Mute and volume are live mpv properties. Treating them like launch
        // settings made every slider step stop and recreate the wallpaper.
        let mut comparable_renderer = next.renderer.clone();
        comparable_renderer.video_muted = self.config.renderer.video_muted;
        comparable_renderer.video_volume = self.config.renderer.video_volume;
        let renderer_changed = comparable_renderer != self.config.renderer;
        let dynamics_changed =
            next.settings.dynamics_enabled != self.config.settings.dynamics_enabled;
        let displays_changed = next.displays != self.config.displays;
        let video_audio_result = if renderer_changed {
            self.driver.reconfigure(next.renderer.clone());
            None
        } else if video_audio_changed {
            Some(
                self.driver
                    .set_video_audio(next.renderer.video_muted, next.renderer.video_volume),
            )
        } else {
            None
        };
        self.config = next;
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
        if renderer_changed
            || dynamics_changed
            || displays_changed
            || old_targets != self.current_targets(&self.target_outputs)
        {
            self.apply_current()?;
        }
        if let Some(result) = video_audio_result {
            result?;
        }
        Ok("reloaded".into())
    }

    fn playlist(&self) -> Result<&Playlist, String> {
        self.config
            .playlist(&self.active_playlist)
            .ok_or_else(|| format!("active playlist {:?} is missing", self.active_playlist))
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
            if self.shuffle_enabled() {
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
        self.target_outputs
            .iter()
            .filter_map(|output| {
                let reference = self
                    .config
                    .displays
                    .iter()
                    .find(|display| display.connector == *output)
                    .map_or(self.config.default_playlist.as_str(), |display| {
                        display.playlist.as_str()
                    });
                let playlist = self.config.playlist(reference)?;
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
        self.last_error = truncate_middle(&error, MAX_LAST_ERROR_BYTES);
        Err(error)
    }

    fn status_json(&self, at: NaiveDateTime) -> Result<String, String> {
        let active_playlist = self.playlist()?;
        let effective_ids = self.effective_playlist_ids();
        let summary_playlist = if self.manual_playlist.is_some()
            || self.schedule_overrode_default
            || self.config.displays.is_empty()
        {
            Some(active_playlist)
        } else if effective_ids.len() == 1 {
            self.config.playlist(&effective_ids[0])
        } else {
            None
        };
        let entry = summary_playlist.and_then(|playlist| self.current_entry_for(&playlist.id));
        let kind = entry.map(|entry| entry_kind(entry.kind));
        let active_ids: HashSet<_> = effective_ids.into_iter().collect();
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
        let selected_rule = schedule::resolve_rule(&self.config.schedules, at)
            .map_err(|error| error.to_string())?;
        let scheduled_playlist = self
            .config
            .playlist(
                selected_rule
                    .map(|rule| rule.playlist.as_str())
                    .unwrap_or(&self.config.default_playlist),
            )
            .ok_or("the selected schedule names a missing playlist")?;
        let schedules = self
            .config
            .schedules
            .iter()
            .map(|rule| self.schedule_rule_status(rule, selected_rule))
            .collect::<Result<Vec<_>, _>>()?;
        let mut displays = Vec::new();
        if self.config.displays.is_empty() {
            if let Some(entry) = entry {
                let assigned = self
                    .config
                    .playlist(&self.config.default_playlist)
                    .ok_or("default playlist is missing")?;
                displays.push(DisplayStatus {
                    connector: "ALL",
                    assignment_source: "default",
                    assigned_playlist_id: &assigned.id,
                    assigned_playlist: &assigned.name,
                    playlist_id: &active_playlist.id,
                    playlist: &active_playlist.name,
                    entry_id: &entry.id,
                    kind: entry_kind(entry.kind),
                    still: entry.still.display().to_string(),
                    motion_active: self.driver.motion_active(""),
                });
            }
        } else {
            for output in &self.target_outputs {
                let explicit = self
                    .config
                    .displays
                    .iter()
                    .find(|display| display.connector == *output);
                let assigned_reference = explicit
                    .map_or(self.config.default_playlist.as_str(), |display| {
                        display.playlist.as_str()
                    });
                let assigned = self
                    .config
                    .playlist(assigned_reference)
                    .ok_or_else(|| format!("display {output} names a missing playlist"))?;
                let reference = if self.manual_playlist.is_some() || self.schedule_overrode_default
                {
                    &self.active_playlist
                } else {
                    assigned_reference
                };
                let effective = self
                    .config
                    .playlist(reference)
                    .ok_or_else(|| format!("display {output} names a missing playlist"))?;
                if let Some(entry) = self.current_entry_for(&effective.id) {
                    displays.push(DisplayStatus {
                        connector: output,
                        assignment_source: if explicit.is_some() {
                            "explicit"
                        } else {
                            "default"
                        },
                        assigned_playlist_id: &assigned.id,
                        assigned_playlist: &assigned.name,
                        playlist_id: &effective.id,
                        playlist: &effective.name,
                        entry_id: &entry.id,
                        kind: entry_kind(entry.kind),
                        still: entry.still.display().to_string(),
                        motion_active: self.driver.motion_active(output),
                    });
                }
            }
        }
        serde_json::to_string(&Status {
            playlist_id: summary_playlist.map_or("", |playlist| playlist.id.as_str()),
            playlist: summary_playlist
                .map_or("Multiple displays", |playlist| playlist.name.as_str()),
            source: if self.manual_playlist.is_some() {
                "manual"
            } else {
                "schedule"
            },
            entry_id: entry.map(|entry| entry.id.as_str()),
            kind,
            still: entry.map(|entry| entry.still.display().to_string()),
            motion_active: summary_playlist.map(|_| {
                if self.config.displays.is_empty() {
                    self.driver.motion_active("")
                } else {
                    self.target_outputs
                        .iter()
                        .any(|output| self.driver.motion_active(output))
                }
            }),
            playback_state: match self.playback_state {
                PlaybackState::Playing => "playing",
                PlaybackState::Paused => "paused",
                PlaybackState::Stopped => "stopped",
            },
            paused: self.playback_state == PlaybackState::Paused,
            stopped: self.playback_state == PlaybackState::Stopped,
            shuffle: self.shuffle_enabled(),
            shuffle_default: self.config.settings.shuffle,
            shuffle_source: if self.shuffle_override.is_some() {
                "manual"
            } else {
                "config"
            },
            cycle_enabled: self.cycle_enabled(),
            cycle_default: self.config.settings.cycle_enabled,
            cycle_source: if self.cycle_override.is_some() {
                "manual"
            } else {
                "config"
            },
            last_error: &self.last_error,
            output_discovery_error: &self.output_discovery_error,
            playlists,
            schedule: ScheduleStatus {
                following: self.manual_playlist.is_none(),
                playlist_id: &scheduled_playlist.id,
                playlist: &scheduled_playlist.name,
                rule_id: selected_rule.map(|rule| rule.id.as_str()),
            },
            schedules,
            displays,
        })
        .map_err(|error| error.to_string())
    }

    fn schedule_rule_status<'a>(
        &'a self,
        rule: &'a ScheduleRule,
        selected: Option<&ScheduleRule>,
    ) -> Result<ScheduleRuleStatus<'a>, String> {
        let playlist = self
            .config
            .playlist(&rule.playlist)
            .ok_or_else(|| format!("schedule {} names a missing playlist", rule.id))?;
        let is_selected = selected.is_some_and(|chosen| chosen.id == rule.id);
        Ok(ScheduleRuleStatus {
            id: &rule.id,
            playlist_id: &playlist.id,
            playlist: &playlist.name,
            months: &rule.months,
            weekdays: &rule.weekdays,
            start: rule.start.as_deref(),
            end: rule.end.as_deref(),
            enabled: rule.enabled,
            selected: is_selected,
            in_force: is_selected && self.manual_playlist.is_none(),
        })
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
