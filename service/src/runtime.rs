use crate::config::{Config, Entry, Playlist, ScheduleRule, MAX_PATH_BYTES};
use crate::protocol::{Request, Response};
use crate::renderer::{WallpaperDriver, MAX_OUTPUT_NAME_BYTES};
use crate::schedule;
use chrono::NaiveDateTime;
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const OUTPUT_PROBE_INTERVAL: Duration = Duration::from_secs(5);
const AUTOMATIC_APPLY_ATTEMPTS: u8 = 3;
const AUTOMATIC_RETRY_DELAY: Duration = Duration::from_secs(2);
const PLAYBACK_HISTORY_LIMIT: usize = 128;
const MAX_TABOO_STATUS_ENTRIES: usize = 64;
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
    pub config_generation: &'a str,
    pub config_path: &'a str,
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
    pub automatic_retry: Option<AutomaticRetryStatus<'a>>,
    pub taboo_entries: Vec<TabooStatus<'a>>,
    pub taboo_entries_omitted: usize,
    pub playlists: Vec<PlaylistStatus<'a>>,
    pub schedule: ScheduleStatus<'a>,
    pub schedules: Vec<ScheduleRuleStatus<'a>>,
    pub displays: Vec<DisplayStatus<'a>>,
}

#[derive(Debug, Serialize)]
pub struct AutomaticRetryStatus<'a> {
    pub attempt: u8,
    pub maximum_attempts: u8,
    pub reason: &'a str,
}

#[derive(Debug, Serialize)]
pub struct TabooStatus<'a> {
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    pub entry_id: &'a str,
    pub kind: &'a str,
    pub scene_id: Option<&'a str>,
    pub reason: &'a str,
    pub source: &'a str,
    pub durable: bool,
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

#[derive(Clone, Debug)]
struct PlaylistCursor {
    order: Vec<usize>,
    position: usize,
    /// Previously played entry indexes, oldest first. This is deliberately
    /// bounded: Previous is a convenience, not an unbounded session log.
    history: Vec<usize>,
    /// Entries unwound by Previous, newest continuation at the end.
    forward: Vec<usize>,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq)]
struct EntryKey {
    playlist_id: String,
    entry_id: String,
}

#[derive(Clone, Debug)]
struct TabooEntry {
    reason: String,
    source: String,
    /// True only after the app has compiled this finding back into conf.
    /// A later config omission is therefore an explicit app-owned clear;
    /// session-only findings survive unrelated reloads until acknowledged.
    durable: bool,
}

fn configured_taboo(config: &Config) -> (HashMap<EntryKey, TabooEntry>, Vec<EntryKey>) {
    let mut records = HashMap::new();
    let mut order = Vec::new();
    for playlist in &config.playlists {
        for entry in &playlist.entries {
            let Some(taboo) = &entry.taboo else {
                continue;
            };
            let key = EntryKey {
                playlist_id: playlist.id.clone(),
                entry_id: entry.id.clone(),
            };
            records.insert(
                key.clone(),
                TabooEntry {
                    reason: taboo.reason.clone(),
                    source: taboo.source.clone(),
                    durable: true,
                },
            );
            order.push(key);
        }
    }
    if order.len() > MAX_TABOO_STATUS_ENTRIES {
        order.drain(..order.len() - MAX_TABOO_STATUS_ENTRIES);
    }
    (records, order)
}

#[derive(Clone)]
struct SelectionState {
    active_playlist: String,
    schedule_overrode_default: bool,
    cursors: HashMap<String, PlaylistCursor>,
    rng: XorShift64,
}

struct PendingAutomatic {
    baseline: SelectionState,
    candidate: SelectionState,
    attempts: u8,
    next_attempt: Instant,
    reason: &'static str,
    failures: Vec<ApplyFailure>,
}

#[derive(Clone, Debug)]
struct ApplyFailure {
    key: EntryKey,
    reason: String,
}

#[derive(Clone, Debug, PartialEq)]
struct Target {
    playlist_id: String,
    entry: Entry,
    output: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PlaybackState {
    Playing,
    Paused,
    Stopped,
}

struct ReloadSnapshot {
    config: Config,
    manual_playlist: Option<String>,
    active_playlist: String,
    schedule_overrode_default: bool,
    cursors: HashMap<String, PlaylistCursor>,
    playback_state: PlaybackState,
    target_outputs: Vec<String>,
    live_outputs: Option<Vec<String>>,
    output_discovery_error: String,
    last_output_probe: Instant,
    rng: XorShift64,
    last_cycle: Instant,
    last_error: String,
    renderer_failed: bool,
    authoritative_generation: u64,
    taboo: HashMap<EntryKey, TabooEntry>,
    taboo_order: Vec<EntryKey>,
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
    last_apply_failures: Vec<ApplyFailure>,
    pending_automatic: Option<PendingAutomatic>,
    taboo: HashMap<EntryKey, TabooEntry>,
    taboo_order: Vec<EntryKey>,
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
        let rendered_config_path = config_path
            .to_str()
            .ok_or("runtime config path must be valid UTF-8")?;
        if !config_path.is_absolute()
            || config_path
                .components()
                .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
        {
            return Err("runtime config path must be a normalized absolute path".into());
        }
        if rendered_config_path.len() > MAX_PATH_BYTES
            || rendered_config_path.chars().any(char::is_control)
        {
            return Err(format!(
                "runtime config path must be at most {MAX_PATH_BYTES} UTF-8 bytes without control characters"
            ));
        }
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
        let (taboo, taboo_order) = configured_taboo(&config);
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
            last_apply_failures: Vec::new(),
            pending_automatic: None,
            taboo,
            taboo_order,
            authoritative_generation: 0,
            quit: false,
        };
        runtime.rebuild_cursors(&HashMap::new())?;
        for playlist in runtime.effective_playlist_ids() {
            // Do not launch motion which the app already knows is borked. If
            // every entry is taboo, retain the first and apply its still only.
            let _ = runtime.reset_cursor_automatic(&playlist);
        }
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

    fn restore_reload_snapshot(&mut self, snapshot: ReloadSnapshot) {
        self.config = snapshot.config;
        self.manual_playlist = snapshot.manual_playlist;
        self.active_playlist = snapshot.active_playlist;
        self.schedule_overrode_default = snapshot.schedule_overrode_default;
        self.cursors = snapshot.cursors;
        self.playback_state = snapshot.playback_state;
        self.target_outputs = snapshot.target_outputs;
        self.live_outputs = snapshot.live_outputs;
        self.output_discovery_error = snapshot.output_discovery_error;
        self.last_output_probe = snapshot.last_output_probe;
        self.rng = snapshot.rng;
        self.last_cycle = snapshot.last_cycle;
        self.last_error = snapshot.last_error;
        self.renderer_failed = snapshot.renderer_failed;
        self.authoritative_generation = snapshot.authoritative_generation;
        self.taboo = snapshot.taboo;
        self.taboo_order = snapshot.taboo_order;
    }

    fn selection_state(&self) -> SelectionState {
        SelectionState {
            active_playlist: self.active_playlist.clone(),
            schedule_overrode_default: self.schedule_overrode_default,
            cursors: self.cursors.clone(),
            rng: self.rng,
        }
    }

    fn restore_selection(&mut self, state: &SelectionState) {
        self.active_playlist.clone_from(&state.active_playlist);
        self.schedule_overrode_default = state.schedule_overrode_default;
        self.cursors.clone_from(&state.cursors);
        self.rng = state.rng;
    }

    fn cancel_automatic_retry(&mut self) {
        self.pending_automatic = None;
    }

    pub fn shutdown(&mut self) {
        self.cancel_automatic_retry();
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
        if !matches!(request.verb.as_str(), "status") {
            // An explicit client command supersedes an automatic candidate
            // waiting between attempts. It must never reappear two seconds
            // later and overwrite the person's choice.
            self.cancel_automatic_retry();
        }
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
            let messages: Vec<String> = failures
                .iter()
                .map(|failure| failure.message.clone())
                .collect();
            self.last_error = bounded_failure_summary(&messages);
            for failure in &failures {
                if failure.permanent_for_session {
                    if let Some(key) = self.failure_key(&failure.entry_id, &failure.output) {
                        self.mark_taboo(key, &failure.message, "renderer-crash");
                    }
                }
            }
            // Videos remain retryable: unlike scenes, SystemDriver does not
            // suppress a video after it exits.  Remember that Play has real
            // work to do instead of treating the still fallback as healthy
            // playback.  Scene retries still reach SystemDriver's session
            // suppression and fail with the attributable scene diagnostic.
            self.renderer_failed = true;
        }
        let had_pending = self.pending_automatic.is_some();
        if let Some(pending) = self.pending_automatic.take() {
            if now >= pending.next_attempt {
                self.attempt_automatic(pending, now);
            } else {
                self.pending_automatic = Some(pending);
            }
        }
        let mut schedule_transition_attempted = false;
        if !had_pending && self.pending_automatic.is_none() && self.manual_playlist.is_none() {
            if let Ok(scheduled) = schedule::resolve_override(&self.config.schedules, at) {
                let overrode = scheduled.is_some();
                let wanted = scheduled
                    .unwrap_or(&self.config.default_playlist)
                    .to_string();
                let playlist_changed = wanted != self.active_playlist;
                let routing_changed = overrode != self.schedule_overrode_default;
                if playlist_changed || routing_changed {
                    schedule_transition_attempted = true;
                    let baseline = self.selection_state();
                    self.schedule_overrode_default = overrode;
                    let mut candidate_available = true;
                    if playlist_changed {
                        self.active_playlist = wanted.clone();
                        candidate_available =
                            self.reset_cursor_automatic(&self.active_playlist.clone());
                    }
                    if candidate_available {
                        let candidate = self.selection_state();
                        self.restore_selection(&baseline);
                        self.start_automatic(baseline, candidate, "schedule", now);
                    } else {
                        self.restore_selection(&baseline);
                        self.last_error = format!(
                            "scheduled playlist {wanted:?} has no usable entries; every entry is taboo this session"
                        );
                    }
                }
            }
        }
        if !schedule_transition_attempted
            && !had_pending
            && self.pending_automatic.is_none()
            && self.playback_state != PlaybackState::Paused
            && self.cycle_enabled()
            && now.duration_since(self.last_cycle)
                >= Duration::from_secs(self.config.settings.cycle_interval_seconds)
        {
            self.start_automatic_move(now);
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

    fn start_automatic_move(&mut self, now: Instant) {
        let baseline = self.selection_state();
        let mut moved = false;
        for playlist in self.effective_playlist_ids() {
            moved |= self.move_cursor_forward(&playlist, true);
        }
        if !moved {
            self.restore_selection(&baseline);
            self.last_cycle = now;
            self.last_error = "every entry in the active playlist is taboo this session".into();
            return;
        }
        let candidate = self.selection_state();
        self.restore_selection(&baseline);
        self.start_automatic(baseline, candidate, "cycle", now);
    }

    fn start_automatic(
        &mut self,
        baseline: SelectionState,
        candidate: SelectionState,
        reason: &'static str,
        now: Instant,
    ) {
        self.attempt_automatic(
            PendingAutomatic {
                baseline,
                candidate,
                attempts: 0,
                next_attempt: now,
                reason,
                failures: Vec::new(),
            },
            now,
        );
    }

    fn attempt_automatic(&mut self, mut pending: PendingAutomatic, now: Instant) {
        self.restore_selection(&pending.candidate);
        let attempted: Vec<EntryKey> = self
            .current_targets(&self.target_outputs)
            .into_iter()
            .map(|target| EntryKey {
                playlist_id: target.playlist_id,
                entry_id: target.entry.id,
            })
            .collect();
        pending.attempts += 1;
        match self.apply_current() {
            Ok(_) => {
                // A successful automatic hand-over owns a complete residency
                // interval. The candidate selection is already installed.
                self.last_cycle = now;
                self.pending_automatic = None;
            }
            Err(error) => {
                let mut failures = self.last_apply_failures.clone();
                if failures.is_empty() {
                    failures = attempted
                        .into_iter()
                        .map(|key| ApplyFailure {
                            key,
                            reason: error.clone(),
                        })
                        .collect();
                }
                pending.failures = failures;
                self.restore_selection(&pending.baseline);

                // Driver apply is necessarily break-before-make. Restore the
                // last known-good selection immediately after a rejected
                // candidate so status and the desktop agree between retries.
                let rollback = self.apply_current().err();
                let diagnostic = match rollback {
                    Some(rollback) => format!(
                        "automatic {} attempt {}/{} failed: {error}; could not restore the previous wallpaper: {rollback}",
                        pending.reason, pending.attempts, AUTOMATIC_APPLY_ATTEMPTS
                    ),
                    None => format!(
                        "automatic {} attempt {}/{} failed: {error}; previous wallpaper restored",
                        pending.reason, pending.attempts, AUTOMATIC_APPLY_ATTEMPTS
                    ),
                };
                self.last_error = truncate_middle(&diagnostic, MAX_LAST_ERROR_BYTES);
                self.renderer_failed = false;

                if pending.attempts >= AUTOMATIC_APPLY_ATTEMPTS {
                    for failure in &pending.failures {
                        self.mark_taboo(failure.key.clone(), &failure.reason, "automatic-apply");
                    }
                    self.last_error = truncate_middle(
                        &format!(
                            "{diagnostic}; entry marked taboo for this session after {} failed attempts",
                            AUTOMATIC_APPLY_ATTEMPTS
                        ),
                        MAX_LAST_ERROR_BYTES,
                    );
                    self.pending_automatic = None;
                } else {
                    pending.next_attempt = now + AUTOMATIC_RETRY_DELAY;
                    self.pending_automatic = Some(pending);
                }
            }
        }
    }

    fn failure_key(&self, entry_id: &str, output: &str) -> Option<EntryKey> {
        self.current_targets(&self.target_outputs)
            .into_iter()
            .find(|target| target.output == output && target.entry.id == entry_id)
            .map(|target| EntryKey {
                playlist_id: target.playlist_id,
                entry_id: target.entry.id,
            })
            .or_else(|| {
                let mut matches =
                    self.config.playlists.iter().filter(|playlist| {
                        playlist.entries.iter().any(|entry| entry.id == entry_id)
                    });
                let playlist = matches.next()?;
                if matches.next().is_some() {
                    None
                } else {
                    Some(EntryKey {
                        playlist_id: playlist.id.clone(),
                        entry_id: entry_id.to_string(),
                    })
                }
            })
    }

    fn mark_taboo(&mut self, key: EntryKey, reason: &str, source: &'static str) {
        if self.taboo.contains_key(&key) {
            return;
        }
        let record = TabooEntry {
            reason: truncate_middle(reason, 512),
            source: source.to_string(),
            durable: false,
        };
        if self.taboo_order.len() == MAX_TABOO_STATUS_ENTRIES {
            self.taboo_order.remove(0);
        }
        self.taboo_order.push(key.clone());
        self.taboo.insert(key, record);
    }

    fn reconcile_configured_taboo(&mut self) {
        let (configured, configured_order) = configured_taboo(&self.config);
        self.taboo
            .retain(|key, record| !record.durable || configured.contains_key(key));
        self.taboo_order.retain(|key| self.taboo.contains_key(key));

        for (key, configured_record) in &configured {
            if let Some(record) = self.taboo.get_mut(key) {
                record.reason.clone_from(&configured_record.reason);
                record.source.clone_from(&configured_record.source);
                record.durable = true;
                continue;
            }
            self.taboo.insert(key.clone(), configured_record.clone());
        }
        for key in configured_order {
            if self.taboo_order.contains(&key) {
                continue;
            }
            if self.taboo_order.len() == MAX_TABOO_STATUS_ENTRIES {
                self.taboo_order.remove(0);
            }
            self.taboo_order.push(key.clone());
        }
    }

    fn is_taboo(&self, playlist_id: &str, entry_id: &str) -> bool {
        self.taboo.contains_key(&EntryKey {
            playlist_id: playlist_id.to_string(),
            entry_id: entry_id.to_string(),
        })
    }

    fn is_durable_taboo(&self, playlist_id: &str, entry_id: &str) -> bool {
        self.taboo
            .get(&EntryKey {
                playlist_id: playlist_id.to_string(),
                entry_id: entry_id.to_string(),
            })
            .is_some_and(|record| record.durable)
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
        let outputs: Vec<String> = targets.iter().map(|target| target.output.clone()).collect();
        self.driver.retain_outputs(&outputs);
        self.last_apply_failures.clear();
        if targets.is_empty() {
            return self.fail("active display playlists are empty");
        }
        let played = targets[0].entry.id.clone();
        let mut errors = Vec::new();
        let mut base_settings = self.config.settings.clone();
        if self.playback_state == PlaybackState::Stopped {
            base_settings.dynamics_enabled = false;
        }
        for target in targets {
            let mut settings = base_settings.clone();
            if self.is_durable_taboo(&target.playlist_id, &target.entry.id) {
                settings.dynamics_enabled = false;
            }
            if let Err(error) = self.driver.apply(&target.entry, &target.output, &settings) {
                self.last_apply_failures.push(ApplyFailure {
                    key: EntryKey {
                        playlist_id: target.playlist_id,
                        entry_id: target.entry.id,
                    },
                    reason: error.clone(),
                });
                errors.push(if target.output.is_empty() {
                    error
                } else {
                    format!("{}: {error}", target.output)
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

    fn current_targets(&self, outputs: &[String]) -> Vec<Target> {
        let mut targets = Vec::new();
        if self.config.displays.is_empty() {
            if let Some(playlist) = self.config.playlist(&self.active_playlist) {
                if let Some(entry) = self.current_entry_for(&playlist.id).cloned() {
                    targets.push(Target {
                        playlist_id: playlist.id.clone(),
                        entry,
                        output: String::new(),
                    });
                }
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
                if let Some(playlist) = self.config.playlist(reference) {
                    if let Some(entry) = self.current_entry_for(&playlist.id).cloned() {
                        targets.push(Target {
                            playlist_id: playlist.id.clone(),
                            entry,
                            output: output.clone(),
                        });
                    }
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
        let result = self.apply_current();
        if result.is_ok() {
            self.last_cycle = Instant::now();
        }
        result
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
        let result = self.apply_current();
        if result.is_ok() {
            self.last_cycle = Instant::now();
        }
        result
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
                    self.last_cycle = Instant::now();
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
                self.last_cycle = Instant::now();
            }
            PlaybackState::Stopped => {
                self.playback_state = PlaybackState::Playing;
                if let Err(error) = self.apply_current() {
                    // A failed resume did not restore motion. Keep status honest
                    // and retain the released/still-only state for another try.
                    self.playback_state = PlaybackState::Stopped;
                    return Err(error);
                }
                self.last_cycle = Instant::now();
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
        self.move_by_at(delta, Instant::now())
    }

    fn move_by_at(&mut self, delta: isize, now: Instant) -> Result<String, String> {
        let mut moved = false;
        for playlist in self.effective_playlist_ids() {
            moved |= if delta < 0 {
                self.move_cursor_backward(&playlist)
            } else {
                self.move_cursor_forward(&playlist, false)
            };
        }
        if !moved {
            return self.fail(if delta < 0 {
                "no previous playback history"
            } else {
                "active display playlists are empty or taboo"
            });
        }
        let result = self.apply_current();
        if result.is_ok() {
            self.last_cycle = now;
        }
        result
    }

    fn random_entry(&mut self) -> Result<String, String> {
        let mut moved = false;
        for playlist in self.effective_playlist_ids() {
            moved |= self.move_cursor_random(&playlist);
        }
        if !moved {
            return self.fail("active display playlists are empty");
        }
        let result = self.apply_current();
        if result.is_ok() {
            self.last_cycle = Instant::now();
        }
        result
    }

    fn reload(&mut self, at: NaiveDateTime) -> Result<String, String> {
        let next = Config::load(&self.config_path).map_err(|error| error.to_string())?;
        let old_targets = self.current_targets(&self.target_outputs);
        let old_entries = self.current_entry_ids();
        let next_manual = self
            .manual_playlist
            .as_ref()
            .filter(|manual| next.playlist(manual).is_some())
            .cloned();
        let (next_active, next_schedule_overrode_default) = if let Some(manual) = &next_manual {
            (manual.clone(), false)
        } else {
            let scheduled = schedule::resolve_override(&next.schedules, at)
                .map_err(|error| error.to_string())?;
            (
                scheduled.unwrap_or(&next.default_playlist).to_string(),
                scheduled.is_some(),
            )
        };
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

        // Adopt the candidate only in memory until every required driver change
        // succeeds. A valid TOML document can still be unplayable (for example,
        // an empty active playlist or media deleted after compilation), so a
        // decoded config is not yet the new last-known-good generation.
        let old_config = std::mem::replace(&mut self.config, next);
        let old_manual_playlist = std::mem::replace(&mut self.manual_playlist, next_manual);
        let old_active_playlist = std::mem::replace(&mut self.active_playlist, next_active);
        let old_schedule_overrode_default = std::mem::replace(
            &mut self.schedule_overrode_default,
            next_schedule_overrode_default,
        );
        let old_cursors = std::mem::take(&mut self.cursors);
        let snapshot = ReloadSnapshot {
            config: old_config,
            manual_playlist: old_manual_playlist,
            active_playlist: old_active_playlist,
            schedule_overrode_default: old_schedule_overrode_default,
            cursors: old_cursors,
            playback_state: self.playback_state,
            target_outputs: self.target_outputs.clone(),
            live_outputs: self.live_outputs.clone(),
            output_discovery_error: self.output_discovery_error.clone(),
            last_output_probe: self.last_output_probe,
            rng: self.rng,
            last_cycle: self.last_cycle,
            last_error: self.last_error.clone(),
            renderer_failed: self.renderer_failed,
            authoritative_generation: self.authoritative_generation,
            taboo: self.taboo.clone(),
            taboo_order: self.taboo_order.clone(),
        };
        self.reconcile_configured_taboo();
        if let Err(error) = self.rebuild_cursors(&old_entries) {
            self.restore_reload_snapshot(snapshot);
            return Err(error);
        }

        let new_targets = self.current_targets(&self.target_outputs);
        let residency_changed = snapshot.active_playlist != self.active_playlist
            || snapshot.manual_playlist != self.manual_playlist
            || snapshot.schedule_overrode_default != self.schedule_overrode_default
            || old_targets != new_targets;
        let apply_needed =
            renderer_changed || dynamics_changed || displays_changed || old_targets != new_targets;
        let mut apply_attempted = false;
        let mut failure = None;

        if renderer_changed {
            self.driver.reconfigure(self.config.renderer.clone());
        } else if video_audio_changed {
            if let Err(error) = self.driver.set_video_audio(
                self.config.renderer.video_muted,
                self.config.renderer.video_volume,
            ) {
                failure = Some(error);
            }
        }
        if failure.is_none() && apply_needed {
            apply_attempted = true;
            if let Err(error) = self.apply_current() {
                failure = Some(error);
            }
        }

        if let Some(error) = failure {
            let previous_last_error = snapshot.last_error.clone();
            let previous_renderer_failed = snapshot.renderer_failed;
            self.restore_reload_snapshot(snapshot);

            let mut rollback_errors = Vec::new();
            if renderer_changed {
                self.driver.reconfigure(self.config.renderer.clone());
            } else if video_audio_changed {
                if let Err(rollback) = self.driver.set_video_audio(
                    self.config.renderer.video_muted,
                    self.config.renderer.video_volume,
                ) {
                    rollback_errors.push(format!("could not restore video audio: {rollback}"));
                }
            }
            if apply_attempted {
                if let Err(rollback) = self.apply_current() {
                    rollback_errors.push(format!("could not restore wallpaper: {rollback}"));
                }
            }

            if rollback_errors.is_empty() {
                self.last_error = previous_last_error;
                self.renderer_failed = previous_renderer_failed;
                return Err(format!(
                    "reload rejected: {error}; previous configuration restored"
                ));
            }
            let rollback = rollback_errors.join("; ");
            self.last_error = truncate_middle(
                &format!("reload rejected: {error}; {rollback}"),
                MAX_LAST_ERROR_BYTES,
            );
            self.renderer_failed = true;
            return Err(format!("reload rejected: {error}; {rollback}"));
        }

        if residency_changed {
            self.last_cycle = Instant::now();
        }
        self.taboo.retain(|key, _| {
            self.config
                .playlist(&key.playlist_id)
                .is_some_and(|playlist| {
                    playlist
                        .entries
                        .iter()
                        .any(|entry| entry.id == key.entry_id)
                })
        });
        self.taboo_order.retain(|key| self.taboo.contains_key(key));
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
            let current = keep_entries
                .get(&id)
                .and_then(|wanted| entry_ids.iter().position(|entry| entry == wanted))
                .unwrap_or(0);
            let (order, position) = if self.shuffle_enabled() && entries > 0 {
                let mut rest: Vec<usize> = (0..entries).filter(|index| *index != current).collect();
                self.rng.shuffle(&mut rest);
                let mut order = Vec::with_capacity(entries);
                order.push(current);
                order.extend(rest);
                (order, 0)
            } else {
                ((0..entries).collect(), current)
            };
            cursors.insert(
                id,
                PlaylistCursor {
                    order,
                    position,
                    history: Vec::new(),
                    forward: Vec::new(),
                },
            );
        }
        self.cursors = cursors;
        self.playlist()?;
        Ok(())
    }

    fn reset_cursor(&mut self, reference: &str) {
        if let Some(playlist) = self.config.playlist(reference) {
            if let Some(cursor) = self.cursors.get_mut(&playlist.id) {
                cursor.position = 0;
                cursor.history.clear();
                cursor.forward.clear();
            }
        }
    }

    fn reset_cursor_automatic(&mut self, reference: &str) -> bool {
        let Some(playlist) = self.config.playlist(reference) else {
            return false;
        };
        let playlist_id = playlist.id.clone();
        let eligible: HashSet<usize> = playlist
            .entries
            .iter()
            .enumerate()
            .filter_map(|(index, entry)| (!self.is_taboo(&playlist_id, &entry.id)).then_some(index))
            .collect();
        let Some(cursor) = self.cursors.get_mut(&playlist_id) else {
            return false;
        };
        let Some(position) = cursor
            .order
            .iter()
            .position(|index| eligible.contains(index))
        else {
            return false;
        };
        cursor.position = position;
        cursor.history.clear();
        cursor.forward.clear();
        true
    }

    fn move_cursor_forward(&mut self, reference: &str, automatic: bool) -> bool {
        let Some(playlist) = self.config.playlist(reference) else {
            return false;
        };
        let playlist_id = playlist.id.clone();
        let entry_ids: Vec<String> = playlist
            .entries
            .iter()
            .map(|entry| entry.id.clone())
            .collect();
        let taboo = &self.taboo;
        let shuffle_enabled = self
            .shuffle_override
            .unwrap_or(self.config.settings.shuffle);
        let eligible = |index: usize| {
            !automatic
                || !taboo.contains_key(&EntryKey {
                    playlist_id: playlist_id.clone(),
                    entry_id: entry_ids[index].clone(),
                })
        };
        let Some(cursor) = self.cursors.get_mut(&playlist_id) else {
            return false;
        };
        let Some(&current) = cursor.order.get(cursor.position) else {
            return false;
        };

        while let Some(index) = cursor.forward.pop() {
            if eligible(index) {
                push_bounded(&mut cursor.history, current);
                if let Some(position) = cursor
                    .order
                    .iter()
                    .position(|candidate| *candidate == index)
                {
                    cursor.position = position;
                }
                return index != current;
            }
        }

        if shuffle_enabled {
            if let Some(position) = ((cursor.position + 1)..cursor.order.len())
                .find(|position| eligible(cursor.order[*position]))
            {
                push_bounded(&mut cursor.history, current);
                cursor.position = position;
                cursor.forward.clear();
                return cursor.order[position] != current;
            }

            let mut next: Vec<usize> = (0..entry_ids.len())
                .filter(|index| eligible(*index))
                .collect();
            if next.is_empty() {
                return false;
            }
            self.rng.shuffle(&mut next);
            if next.len() > 1 && next[0] == current {
                next.swap(0, 1);
            }
            let selected = next[0];
            if selected == current {
                return false;
            }
            push_bounded(&mut cursor.history, current);
            cursor.order = next;
            cursor.position = 0;
            cursor.forward.clear();
            true
        } else {
            for offset in 1..=cursor.order.len() {
                let position = (cursor.position + offset) % cursor.order.len();
                let index = cursor.order[position];
                if eligible(index) && index != current {
                    push_bounded(&mut cursor.history, current);
                    cursor.position = position;
                    cursor.forward.clear();
                    return true;
                }
            }
            false
        }
    }

    fn move_cursor_backward(&mut self, reference: &str) -> bool {
        let Some(playlist) = self.config.playlist(reference) else {
            return false;
        };
        let Some(cursor) = self.cursors.get_mut(&playlist.id) else {
            return false;
        };
        let Some(previous) = cursor.history.pop() else {
            return false;
        };
        let Some(&current) = cursor.order.get(cursor.position) else {
            return false;
        };
        push_bounded(&mut cursor.forward, current);
        cursor.position = if let Some(position) = cursor
            .order
            .iter()
            .position(|candidate| *candidate == previous)
        {
            position
        } else {
            cursor.order.push(previous);
            cursor.order.len() - 1
        };
        true
    }

    fn move_cursor_random(&mut self, reference: &str) -> bool {
        let Some(playlist) = self.config.playlist(reference) else {
            return false;
        };
        let playlist_id = playlist.id.clone();
        let eligible: Vec<usize> = playlist
            .entries
            .iter()
            .enumerate()
            .filter_map(|(index, entry)| (!self.is_taboo(&playlist_id, &entry.id)).then_some(index))
            .collect();
        let Some(cursor) = self.cursors.get_mut(&playlist_id) else {
            return false;
        };
        let Some(&current) = cursor.order.get(cursor.position) else {
            return false;
        };
        if self
            .shuffle_override
            .unwrap_or(self.config.settings.shuffle)
        {
            let remaining_positions: Vec<usize> = ((cursor.position + 1)..cursor.order.len())
                .filter(|position| eligible.contains(&cursor.order[*position]))
                .collect();
            if remaining_positions.is_empty() {
                let mut next: Vec<usize> = eligible;
                if next.len() <= 1 {
                    return false;
                }
                self.rng.shuffle(&mut next);
                if next[0] == current {
                    next.swap(0, 1);
                }
                push_bounded(&mut cursor.history, current);
                cursor.forward.clear();
                cursor.order = next;
                cursor.position = 0;
                return true;
            }
            let chosen = remaining_positions[self.rng.index(remaining_positions.len())];
            let next = cursor.position + 1;
            cursor.order.swap(next, chosen);
            push_bounded(&mut cursor.history, current);
            cursor.forward.clear();
            cursor.position = next;
        } else {
            let candidates: Vec<usize> = eligible
                .into_iter()
                .filter(|index| *index != current)
                .collect();
            if candidates.is_empty() {
                return false;
            }
            let selected = candidates[self.rng.index(candidates.len())];
            push_bounded(&mut cursor.history, current);
            cursor.forward.clear();
            cursor.position = cursor
                .order
                .iter()
                .position(|index| *index == selected)
                .unwrap_or(cursor.position);
        }
        true
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
        let taboo_total = self.taboo.len();
        let taboo_entries: Vec<TabooStatus<'_>> = self
            .taboo_order
            .iter()
            .rev()
            .filter_map(|key| {
                let record = self.taboo.get(key)?;
                let playlist = self.config.playlist(&key.playlist_id)?;
                let entry = playlist
                    .entries
                    .iter()
                    .find(|entry| entry.id == key.entry_id)?;
                Some(TabooStatus {
                    playlist_id: &playlist.id,
                    playlist: &playlist.name,
                    entry_id: &entry.id,
                    kind: entry_kind(entry.kind),
                    scene_id: entry.scene_id.as_deref(),
                    reason: &record.reason,
                    source: record.source.as_str(),
                    durable: record.durable,
                })
            })
            .take(MAX_TABOO_STATUS_ENTRIES)
            .collect();
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
            config_generation: &self.config.config_generation,
            config_path: self
                .config_path
                .to_str()
                .expect("Runtime::new validated the config path as UTF-8"),
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
            automatic_retry: self
                .pending_automatic
                .as_ref()
                .map(|pending| AutomaticRetryStatus {
                    attempt: pending.attempts,
                    maximum_attempts: AUTOMATIC_APPLY_ATTEMPTS,
                    reason: pending.reason,
                }),
            taboo_entries_omitted: taboo_total.saturating_sub(taboo_entries.len()),
            taboo_entries,
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

fn push_bounded(values: &mut Vec<usize>, value: usize) {
    if values.len() == PLAYBACK_HISTORY_LIMIT {
        values.remove(0);
    }
    values.push(value);
}

#[derive(Clone, Copy)]
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

#[cfg(test)]
mod tests {
    use super::{push_bounded, PLAYBACK_HISTORY_LIMIT};

    #[test]
    fn playback_history_has_a_hard_memory_bound() {
        let mut history = Vec::new();
        for value in 0..(PLAYBACK_HISTORY_LIMIT * 3) {
            push_bounded(&mut history, value);
        }
        assert_eq!(history.len(), PLAYBACK_HISTORY_LIMIT);
        assert_eq!(history[0], PLAYBACK_HISTORY_LIMIT * 2);
        assert_eq!(history.last(), Some(&(PLAYBACK_HISTORY_LIMIT * 3 - 1)));
    }
}
