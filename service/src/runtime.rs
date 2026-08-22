use crate::config::{
    Config, DisplayMode, Entry, EntryKind, Playlist, ScheduleRule, MAX_PATH_BYTES,
};
use crate::protocol::{Request, Response};
use crate::renderer::{RendererFailure, WallpaperDriver, MAX_OUTPUT_NAME_BYTES};
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
const MAX_DISPLAY_DIAGNOSTIC_TOTAL_BYTES: usize = 48 * 1024;
const MAX_OUTPUT_DISCOVERY_ERROR_BYTES: usize = 4 * 1024;
const MAX_LIVE_DISPLAY_ROUTES: usize = 64;
const MAX_RETAINED_DISPLAY_ROUTES: usize = 64;
const MAX_STATUS_DISPLAY_ROWS: usize = MAX_LIVE_DISPLAY_ROUTES + MAX_RETAINED_DISPLAY_ROUTES;
const MAX_DISPLAY_ERROR_BYTES: usize = MAX_DISPLAY_DIAGNOSTIC_TOTAL_BYTES / MAX_STATUS_DISPLAY_ROWS;
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
    pub status_version: u8,
    pub runtime_instance: &'a str,
    pub config_epoch: u64,
    pub config_generation: &'a str,
    pub config_path: &'a str,
    pub display_mode: &'a str,
    pub theme_source: ThemeSourceStatus<'a>,
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    pub source: &'a str,
    pub entry_id: Option<&'a str>,
    pub kind: Option<&'a str>,
    pub still: Option<String>,
    pub motion_active: Option<bool>,
    /// Aggregate renderer degradation for compact clients. In independent
    /// mode this is true when any currently connected route is degraded;
    /// detached retained route history cannot poison the live summary.
    pub renderer_failed: bool,
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
    pub observed_config_epoch: u64,
}

#[derive(Debug, Serialize)]
pub struct ThemeSourceStatus<'a> {
    pub configured: &'a str,
    pub effective: Option<&'a str>,
    pub fallback: bool,
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
    pub connector: Option<&'a str>,
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
    pub connected: bool,
    pub assignment_source: &'a str,
    pub assigned_playlist_id: &'a str,
    pub assigned_playlist: &'a str,
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    pub entry_id: &'a str,
    pub kind: &'a str,
    pub still: String,
    pub motion_active: bool,
    pub route_source: &'a str,
    pub manual_override: bool,
    pub schedule_rule_id: Option<&'a str>,
    pub playback_state: &'a str,
    pub paused: bool,
    pub stopped: bool,
    pub shuffle: bool,
    pub shuffle_default: bool,
    pub shuffle_source: &'a str,
    pub cycle_enabled: bool,
    pub cycle_default: bool,
    pub cycle_source: &'a str,
    pub renderer_failed: bool,
    pub last_error: String,
    pub automatic_retry: Option<AutomaticRetryStatus<'a>>,
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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RouteSource {
    Manual,
    Schedule,
    Assignment,
    Default,
}

impl RouteSource {
    fn as_str(self) -> &'static str {
        match self {
            Self::Manual => "manual",
            Self::Schedule => "schedule",
            Self::Assignment => "assignment",
            Self::Default => "default",
        }
    }
}

#[derive(Clone)]
struct DisplayRoute {
    manual_playlist: Option<String>,
    active_playlist: String,
    schedule_rule_id: Option<String>,
    source: RouteSource,
    cursor: PlaylistCursor,
    playback_state: PlaybackState,
    shuffle_override: Option<bool>,
    cycle_override: Option<bool>,
    renderer_failed: bool,
    last_error: String,
    last_cycle: Instant,
    last_seen: u64,
}

impl DisplayRoute {
    fn shuffle_enabled(&self, configured: bool) -> bool {
        self.shuffle_override.unwrap_or(configured)
    }

    fn cycle_enabled(&self, configured: bool) -> bool {
        self.cycle_override.unwrap_or(configured)
    }
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
    /// In-process config epoch under which the renderer failure was observed,
    /// safely reconciled by resolved media identity, or loaded from app-owned
    /// authoring state.
    observed_config_epoch: u64,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq)]
enum ResolvedMediaIdentity {
    Still(PathBuf),
    Video(PathBuf),
    Scene(String),
}

#[derive(Default)]
struct NondurableFindings {
    records: HashMap<ResolvedMediaIdentity, TabooEntry>,
    /// Oldest-to-newest identity order for the bounded status inventory.
    order: Vec<ResolvedMediaIdentity>,
}

fn configured_taboo(
    config: &Config,
    config_epoch: u64,
) -> (HashMap<EntryKey, TabooEntry>, Vec<EntryKey>) {
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
                    observed_config_epoch: config_epoch,
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
    restore_baseline: bool,
    failures: Vec<ApplyFailure>,
}

#[derive(Clone)]
struct PendingRouteAutomatic {
    connector: String,
    baseline: DisplayRoute,
    candidate: DisplayRoute,
    attempts: u8,
    next_attempt: Instant,
    reason: &'static str,
    restore_baseline: bool,
    failures: Vec<ApplyFailure>,
}

#[derive(Clone)]
struct IndependentSelectionSnapshot {
    routes: HashMap<String, DisplayRoute>,
}

#[derive(Clone, Debug)]
struct ApplyFailure {
    key: EntryKey,
    output: String,
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
    shuffle_override: Option<bool>,
    cycle_override: Option<bool>,
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
    routes: HashMap<String, DisplayRoute>,
    route_generation: u64,
    current_time: NaiveDateTime,
    config_epoch: u64,
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
    pending_routes: HashMap<String, PendingRouteAutomatic>,
    taboo: HashMap<EntryKey, TabooEntry>,
    taboo_order: Vec<EntryKey>,
    authoritative_generation: u64,
    routes: HashMap<String, DisplayRoute>,
    route_generation: u64,
    current_time: NaiveDateTime,
    runtime_instance: String,
    config_epoch: u64,
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
        let independent = config.settings.display_mode == DisplayMode::Independent;
        let mut target_outputs = if independent {
            config
                .displays
                .iter()
                .map(|display| display.connector.clone())
                .collect()
        } else if config.displays.is_empty() {
            vec![String::new()]
        } else {
            config
                .displays
                .iter()
                .map(|display| display.connector.clone())
                .collect()
        };
        target_outputs.sort();
        target_outputs.dedup();
        let config_epoch = 1;
        let (taboo, taboo_order) = configured_taboo(&config, config_epoch);
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
            pending_routes: HashMap::new(),
            taboo,
            taboo_order,
            authoritative_generation: 0,
            routes: HashMap::new(),
            route_generation: 0,
            current_time: at,
            runtime_instance: new_runtime_instance(),
            config_epoch,
            quit: false,
        };
        runtime.rebuild_cursors(&HashMap::new())?;
        if independent {
            let outputs = runtime.target_outputs.clone();
            runtime.reconcile_independent_routes(&outputs)?;
        } else {
            for playlist in runtime.effective_playlist_ids() {
                // Do not launch motion which the app already knows is borked. If
                // every entry is taboo, retain the first and apply its still only.
                let _ = runtime.reset_cursor_automatic(&playlist);
            }
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
        self.shuffle_override = snapshot.shuffle_override;
        self.cycle_override = snapshot.cycle_override;
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
        self.routes = snapshot.routes;
        self.route_generation = snapshot.route_generation;
        self.current_time = snapshot.current_time;
        self.config_epoch = snapshot.config_epoch;
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
        self.pending_routes.clear();
        self.driver.stop();
        self.quit = true;
    }

    /// Continue a non-readiness startup apply through the normal automatic
    /// quarantine policy. The first failed apply has already happened; two
    /// delayed retries follow before the resolved entry is marked taboo and
    /// the route advances. Desktop-readiness failures remain owned by main's
    /// longer readiness window and must not call this method.
    pub fn schedule_initial_apply_retry(&mut self, now: Instant) -> bool {
        if self.last_apply_failures.is_empty() {
            return false;
        }
        if self.is_independent() {
            let failures = self.last_apply_failures.clone();
            let failed_keys: HashSet<EntryKey> =
                failures.iter().map(|failure| failure.key.clone()).collect();
            let targets = self.current_targets(&self.target_outputs);
            let mut scheduled = false;
            for target in targets {
                let key = EntryKey {
                    playlist_id: target.playlist_id,
                    entry_id: target.entry.id,
                };
                if !failed_keys.contains(&key)
                    || !failures
                        .iter()
                        .any(|failure| failure.key == key && failure.output == target.output)
                    || self.pending_routes.contains_key(&target.output)
                {
                    continue;
                }
                let Some(route) = self.routes.get(&target.output).cloned() else {
                    continue;
                };
                let route_failures = failures
                    .iter()
                    .filter(|failure| failure.key == key && failure.output == target.output)
                    .cloned()
                    .collect();
                self.pending_routes.insert(
                    target.output.clone(),
                    PendingRouteAutomatic {
                        connector: target.output,
                        baseline: route.clone(),
                        candidate: route,
                        attempts: 1,
                        next_attempt: now + AUTOMATIC_RETRY_DELAY,
                        reason: "startup",
                        restore_baseline: false,
                        failures: route_failures,
                    },
                );
                scheduled = true;
            }
            return scheduled;
        }

        let selection = self.selection_state();
        self.pending_automatic = Some(PendingAutomatic {
            baseline: selection.clone(),
            candidate: selection,
            attempts: 1,
            next_attempt: now + AUTOMATIC_RETRY_DELAY,
            reason: "startup",
            restore_baseline: false,
            failures: self.last_apply_failures.clone(),
        });
        true
    }

    pub fn handle(&mut self, request: Request, at: NaiveDateTime) -> Response {
        self.current_time = at;
        let no_argument = |usage: &str| {
            if request.argument.is_some() {
                Err(format!("usage: {usage}"))
            } else {
                Ok(())
            }
        };
        if !self.is_independent() && self.accepted_mirrored_runtime_command(&request) {
            // An explicit client command supersedes an automatic candidate
            // waiting between attempts. It must never reappear two seconds
            // later and overwrite the person's choice. Rejected input is not
            // a command: a typo must not silently cancel the promised retry.
            self.cancel_automatic_retry();
        }
        if self.is_independent() && request.verb == "quit" && request.argument.is_none() {
            self.pending_routes.clear();
        }
        if request.verb == "on" {
            return match self.handle_on(request.argument.as_deref(), at) {
                Ok(message) => Response::success(message),
                Err(error) => Response::failure(error),
            };
        }
        if self.is_independent()
            && matches!(
                request.verb.as_str(),
                "playlist-use"
                    | "schedule-follow"
                    | "play"
                    | "pause"
                    | "toggle"
                    | "stop"
                    | "shuffle"
                    | "cycle"
                    | "next"
                    | "previous"
                    | "random"
            )
        {
            return match self.handle_independent_command(None, &request, at) {
                Ok(message) => Response::success(message),
                Err(error) => Response::failure(error),
            };
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

    fn accepted_mirrored_runtime_command(&self, request: &Request) -> bool {
        match request.verb.as_str() {
            "playlist-use" => request
                .argument
                .as_deref()
                .map(str::trim)
                .filter(|reference| !reference.is_empty())
                .is_some_and(|reference| self.config.playlist(reference).is_some()),
            "shuffle" => Self::runtime_boolean_override(
                request.argument.as_deref(),
                "shuffle on|off|default",
            )
            .is_ok(),
            "cycle" => {
                Self::runtime_boolean_override(request.argument.as_deref(), "cycle on|off|default")
                    .is_ok()
            }
            "schedule-follow" | "play" | "pause" | "toggle" | "stop" | "next" | "previous"
            | "random" | "quit" => request.argument.is_none(),
            "status" | "reload" | "on" => false,
            _ => false,
        }
    }

    pub fn tick(&mut self, at: NaiveDateTime, now: Instant) {
        self.current_time = at;
        let mut failures = self.driver.poll_failures();
        if let Some(palette_failure) = self.reapply_palette_after_renderer_failure(&failures) {
            failures.push(palette_failure);
        }
        if self.is_independent() {
            let mut unattributed = Vec::new();
            for failure in &failures {
                if failure.permanent_for_session {
                    if let Some(key) = self.failure_key(&failure.entry_id, &failure.output) {
                        self.mark_taboo(key, &failure.message, "renderer-crash");
                    }
                }
                if let Some(route) = self.routes.get_mut(&failure.output) {
                    route.playback_state = PlaybackState::Stopped;
                    route.renderer_failed = true;
                    route.last_error = truncate_middle(&failure.message, MAX_LAST_ERROR_BYTES);
                } else {
                    unattributed.push(failure.message.clone());
                }
            }
            self.refresh_independent_last_error();
            if !unattributed.is_empty() {
                let mut messages = Vec::new();
                if !self.last_error.is_empty() {
                    messages.push(self.last_error.clone());
                }
                messages.extend(unattributed);
                self.last_error = bounded_failure_summary(&messages);
            }
            self.tick_independent(at, now);
            return;
        }
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
                if routing_changed && !playlist_changed {
                    // Only provenance changed (for example, a schedule rule
                    // naming the already-active default). Commit the truth
                    // shown in status without restarting motion.
                    self.schedule_overrode_default = overrode;
                } else if playlist_changed {
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

    fn reapply_palette_after_renderer_failure(
        &mut self,
        failures: &[RendererFailure],
    ) -> Option<RendererFailure> {
        if failures.is_empty() {
            return None;
        }
        let targets = self.current_targets(&self.target_outputs);
        let target = if self.is_independent() {
            let theme_source = self.effective_theme_source()?;
            if !failures
                .iter()
                .any(|failure| failure.output == theme_source)
            {
                return None;
            }
            targets.iter().find(|target| target.output == theme_source)
        } else {
            // Mirrored mode has one shell-global palette. Reassert the palette
            // paired with the renderer whose fallback still was just applied;
            // the ordinary all-output route is represented by an empty token.
            targets.iter().rev().find(|target| {
                failures
                    .iter()
                    .any(|failure| failure.output == target.output)
            })
        }?;
        self.driver
            .apply_palette_only(&target.entry)
            .err()
            .map(|error| RendererFailure {
                entry_id: target.entry.id.clone(),
                kind: target.entry.kind,
                scene_id: target.entry.scene_id.clone(),
                output: target.output.clone(),
                message: format!(
                    "renderer fallback restored the still but could not restore its palette: {error}"
                ),
                permanent_for_session: false,
            })
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

    fn tick_independent(&mut self, at: NaiveDateTime, now: Instant) {
        self.current_time = at;
        if now.saturating_duration_since(self.last_output_probe) >= OUTPUT_PROBE_INTERVAL {
            self.last_output_probe = now;
            let previous: HashSet<String> = self.target_outputs.iter().cloned().collect();
            self.driver.begin_apply();
            match self.probe_outputs() {
                Ok(outputs) if self.live_outputs.as_ref() != Some(&outputs) => {
                    let _ = self.apply_current_in_batch(Ok(outputs));
                    for connector in self
                        .target_outputs
                        .iter()
                        .filter(|connector| !previous.contains(*connector))
                    {
                        if let Some(route) = self.routes.get_mut(connector) {
                            route.last_cycle = now;
                        }
                    }
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
        let connectors: Vec<String> = self
            .target_outputs
            .iter()
            .filter(|connector| !connector.is_empty())
            .cloned()
            .collect();
        let pending_at_start: HashSet<String> = self.pending_routes.keys().cloned().collect();
        let due: Vec<String> = self
            .pending_routes
            .iter()
            .filter(|(connector, pending)| {
                connectors.contains(connector) && now >= pending.next_attempt
            })
            .map(|(connector, _)| connector.clone())
            .collect();
        for connector in due {
            if let Some(pending) = self.pending_routes.remove(&connector) {
                self.attempt_route_automatic(pending, now);
            }
        }

        for connector in &connectors {
            if pending_at_start.contains(connector) || self.pending_routes.contains_key(connector) {
                continue;
            }
            let Some(route) = self.routes.get(connector) else {
                continue;
            };
            let manual = route.manual_playlist.clone();
            let before = route.clone();
            let decision = self.route_decision(connector, manual.as_deref(), at);
            let Ok((wanted, source, rule_id)) = decision else {
                continue;
            };
            let schedule_changed = route.active_playlist != wanted
                || route.source != source
                || route.schedule_rule_id != rule_id;
            if schedule_changed && manual.is_none() {
                let usable = self.config.playlist(&wanted).is_some_and(|playlist| {
                    playlist
                        .entries
                        .iter()
                        .any(|entry| !self.is_taboo(&playlist.id, &entry.id))
                });
                if !usable {
                    let route = self.routes.get_mut(connector).expect("route exists");
                    route.last_error = format!(
                        "scheduled playlist {wanted:?} has no usable entries; every entry is taboo this session"
                    );
                    self.refresh_independent_last_error();
                    continue;
                }
                if self.refresh_route_decision(connector, true).is_ok() {
                    let candidate = self.routes[connector].clone();
                    let before_entry =
                        self.config
                            .playlist(&before.active_playlist)
                            .and_then(|playlist| {
                                before
                                    .cursor
                                    .order
                                    .get(before.cursor.position)
                                    .and_then(|index| playlist.entries.get(*index))
                                    .map(|entry| entry.id.as_str())
                            });
                    let candidate_entry = self
                        .route_current_entry(connector)
                        .map(|entry| entry.id.as_str());
                    if before.active_playlist == candidate.active_playlist
                        && before_entry == candidate_entry
                    {
                        // The schedule/source/rule label changed, but the
                        // fully resolved target did not. Keep the renderer and
                        // residency interval intact.
                        continue;
                    }
                    self.routes.insert(connector.clone(), before.clone());
                    self.start_route_automatic(
                        connector.clone(),
                        before,
                        candidate,
                        "schedule",
                        now,
                    );
                }
                continue;
            }
            if route.playback_state != PlaybackState::Paused
                && route.cycle_enabled(self.config.settings.cycle_enabled)
                && now.duration_since(route.last_cycle)
                    >= Duration::from_secs(self.config.settings.cycle_interval_seconds)
            {
                if self.move_route_forward(connector, true) {
                    let candidate = self.routes[connector].clone();
                    self.routes.insert(connector.clone(), before.clone());
                    self.start_route_automatic(connector.clone(), before, candidate, "cycle", now);
                } else {
                    let route = self.routes.get_mut(connector).expect("route exists");
                    route.last_cycle = now;
                    route.last_error = "every entry in this playlist is taboo this session".into();
                    self.refresh_independent_last_error();
                }
            }
        }
    }

    fn start_route_automatic(
        &mut self,
        connector: String,
        baseline: DisplayRoute,
        candidate: DisplayRoute,
        reason: &'static str,
        now: Instant,
    ) {
        self.start_route_automatic_with_policy(connector, baseline, candidate, reason, true, now);
    }

    fn start_route_automatic_with_policy(
        &mut self,
        connector: String,
        baseline: DisplayRoute,
        candidate: DisplayRoute,
        reason: &'static str,
        restore_baseline: bool,
        now: Instant,
    ) {
        self.attempt_route_automatic(
            PendingRouteAutomatic {
                connector,
                baseline,
                candidate,
                attempts: 0,
                next_attempt: now,
                reason,
                restore_baseline,
                failures: Vec::new(),
            },
            now,
        );
    }

    fn attempt_route_automatic(&mut self, mut pending: PendingRouteAutomatic, now: Instant) {
        let connector = pending.connector.clone();
        self.routes
            .insert(connector.clone(), pending.candidate.clone());
        let attempted = self.route_current_entry(&connector).map(|entry| EntryKey {
            playlist_id: pending.candidate.active_playlist.clone(),
            entry_id: entry.id.clone(),
        });
        pending.attempts += 1;
        let selected = HashSet::from([connector.clone()]);
        match self.apply_independent_selected(&selected) {
            Ok(_) => {
                self.routes
                    .get_mut(&connector)
                    .expect("candidate route remains installed")
                    .last_cycle = now;
                self.pending_routes.remove(&connector);
            }
            Err(error) => {
                let mut failures = self.last_apply_failures.clone();
                if failures.is_empty() {
                    if let Some(key) = attempted {
                        failures.push(ApplyFailure {
                            key,
                            output: connector.clone(),
                            reason: error.clone(),
                        });
                    }
                }
                pending.failures = failures;
                let rollback = if pending.restore_baseline {
                    self.routes
                        .insert(connector.clone(), pending.baseline.clone());
                    self.apply_independent_selected(&selected).err()
                } else {
                    None
                };
                let rollback_failed = rollback.is_some();
                let diagnostic = match rollback.as_deref() {
                    Some(rollback) => format!(
                        "automatic {} attempt {}/{} failed: {error}; could not restore the previous wallpaper: {rollback}",
                        pending.reason, pending.attempts, AUTOMATIC_APPLY_ATTEMPTS
                    ),
                    None if pending.restore_baseline => format!(
                        "automatic {} attempt {}/{} failed: {error}; previous wallpaper restored",
                        pending.reason, pending.attempts, AUTOMATIC_APPLY_ATTEMPTS
                    ),
                    None => format!(
                        "automatic {} attempt {}/{} failed: {error}; paired still remains the fallback",
                        pending.reason, pending.attempts, AUTOMATIC_APPLY_ATTEMPTS
                    ),
                };
                let mut diagnostic = truncate_middle(&diagnostic, MAX_LAST_ERROR_BYTES);
                if pending.attempts >= AUTOMATIC_APPLY_ATTEMPTS {
                    for failure in &pending.failures {
                        self.mark_taboo(failure.key.clone(), &failure.reason, "automatic-apply");
                    }
                    diagnostic = truncate_middle(
                        &format!(
                            "{diagnostic}; entry marked taboo for this session after {} failed attempts",
                            AUTOMATIC_APPLY_ATTEMPTS
                        ),
                        MAX_LAST_ERROR_BYTES,
                    );
                    if !pending.restore_baseline && self.move_route_forward(&connector, true) {
                        let candidate = self.routes[&connector].clone();
                        let mut next = PendingRouteAutomatic {
                            connector: connector.clone(),
                            baseline: candidate.clone(),
                            candidate,
                            attempts: 0,
                            next_attempt: now + AUTOMATIC_RETRY_DELAY,
                            reason: pending.reason,
                            restore_baseline: false,
                            failures: Vec::new(),
                        };
                        next.baseline.last_error.clear();
                        next.candidate.last_error.clear();
                        self.pending_routes.insert(connector.clone(), next);
                    }
                } else {
                    pending.next_attempt = now + AUTOMATIC_RETRY_DELAY;
                    self.pending_routes.insert(connector.clone(), pending);
                }
                if let Some(route) = self.routes.get_mut(&connector) {
                    route.last_error = diagnostic;
                    route.renderer_failed = rollback_failed;
                }
                self.refresh_independent_last_error();
            }
        }
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
                restore_baseline: true,
                failures: Vec::new(),
            },
            now,
        );
    }

    fn attempt_automatic(&mut self, mut pending: PendingAutomatic, now: Instant) {
        self.restore_selection(&pending.candidate);
        let attempted: Vec<(EntryKey, String)> = self
            .current_targets(&self.target_outputs)
            .into_iter()
            .map(|target| {
                (
                    EntryKey {
                        playlist_id: target.playlist_id,
                        entry_id: target.entry.id,
                    },
                    target.output,
                )
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
                        .map(|(key, output)| ApplyFailure {
                            key,
                            output,
                            reason: error.clone(),
                        })
                        .collect();
                }
                pending.failures = failures;
                let rollback = if pending.restore_baseline {
                    self.restore_selection(&pending.baseline);
                    // Driver apply is necessarily break-before-make. Restore
                    // the last known-good selection immediately after a
                    // rejected transition so status and desktop agree.
                    self.apply_current().err()
                } else {
                    None
                };
                let diagnostic = match (pending.restore_baseline, rollback) {
                    (true, Some(rollback)) => format!(
                        "automatic {} attempt {}/{} failed: {error}; could not restore the previous wallpaper: {rollback}",
                        pending.reason, pending.attempts, AUTOMATIC_APPLY_ATTEMPTS
                    ),
                    (true, None) => format!(
                        "automatic {} attempt {}/{} failed: {error}; previous wallpaper restored",
                        pending.reason, pending.attempts, AUTOMATIC_APPLY_ATTEMPTS
                    ),
                    (false, _) => format!(
                        "automatic {} attempt {}/{} failed: {error}; paired still remains the fallback",
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
                    if !pending.restore_baseline {
                        let mut moved = false;
                        let playlists: HashSet<String> = pending
                            .failures
                            .iter()
                            .map(|failure| failure.key.playlist_id.clone())
                            .collect();
                        for playlist in playlists {
                            moved |= self.move_cursor_forward(&playlist, true);
                        }
                        if moved {
                            let candidate = self.selection_state();
                            self.pending_automatic = Some(PendingAutomatic {
                                baseline: candidate.clone(),
                                candidate,
                                attempts: 0,
                                next_attempt: now + AUTOMATIC_RETRY_DELAY,
                                reason: pending.reason,
                                restore_baseline: false,
                                failures: Vec::new(),
                            });
                        }
                    }
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
        let keys = self.equivalent_entry_keys(&key);
        for key in keys {
            self.mark_taboo_one(key, reason, source);
        }
    }

    fn mark_taboo_one(&mut self, key: EntryKey, reason: &str, source: &'static str) {
        let mut promote = false;
        if let Some(record) = self.taboo.get_mut(&key) {
            if !record.durable {
                record.reason = truncate_middle(reason, 512);
                record.source = source.to_string();
                record.observed_config_epoch = self.config_epoch;
                promote = true;
            }
        } else {
            let record = TabooEntry {
                reason: truncate_middle(reason, 512),
                source: source.to_string(),
                durable: false,
                observed_config_epoch: self.config_epoch,
            };
            self.taboo.insert(key.clone(), record);
            promote = true;
        }
        if !promote {
            return;
        }

        promote_taboo_status_key(&mut self.taboo_order, key);
    }

    fn equivalent_entry_keys(&self, key: &EntryKey) -> Vec<EntryKey> {
        let source = self.config.playlist(&key.playlist_id).and_then(|playlist| {
            playlist
                .entries
                .iter()
                .find(|entry| entry.id == key.entry_id)
        });
        let Some(source) = source else {
            return vec![key.clone()];
        };
        self.config
            .playlists
            .iter()
            .flat_map(|playlist| {
                playlist
                    .entries
                    .iter()
                    .filter(move |candidate| equivalent_resolved_entry(source, candidate))
                    .map(move |candidate| EntryKey {
                        playlist_id: playlist.id.clone(),
                        entry_id: candidate.id.clone(),
                    })
            })
            .collect()
    }

    fn snapshot_nondurable_findings(&self) -> NondurableFindings {
        let mut findings = NondurableFindings::default();
        for (key, record) in &self.taboo {
            if record.durable {
                continue;
            }
            let Some(identity) = resolved_identity_for_key(&self.config, key) else {
                continue;
            };
            findings.records.insert(identity, record.clone());
        }
        for key in &self.taboo_order {
            let Some(record) = self.taboo.get(key) else {
                continue;
            };
            if record.durable {
                continue;
            }
            let Some(identity) = resolved_identity_for_key(&self.config, key) else {
                continue;
            };
            // Equivalent occurrences share one finding. Keep the last position
            // so the bounded inventory preserves the newest observed identity.
            findings.order.retain(|candidate| candidate != &identity);
            findings.order.push(identity);
        }
        findings
    }

    fn reconcile_configured_taboo(&mut self, previous: &NondurableFindings) {
        let (mut taboo, configured_order) = configured_taboo(&self.config, self.config_epoch);
        let mut keys_by_identity: HashMap<ResolvedMediaIdentity, Vec<EntryKey>> = HashMap::new();

        for playlist in &self.config.playlists {
            for entry in &playlist.entries {
                let Some(identity) = resolved_media_identity(entry) else {
                    continue;
                };
                let key = EntryKey {
                    playlist_id: playlist.id.clone(),
                    entry_id: entry.id.clone(),
                };
                keys_by_identity
                    .entry(identity.clone())
                    .or_default()
                    .push(key.clone());
                let Some(previous_record) = previous.records.get(&identity) else {
                    continue;
                };
                // App-authored metadata wins for an exact occurrence. Every
                // session-only record that survived by resolved identity is
                // attributable to the newly adopted epoch.
                taboo.entry(key).or_insert_with(|| {
                    let mut record = previous_record.clone();
                    record.durable = false;
                    record.observed_config_epoch = self.config_epoch;
                    record
                });
            }
        }

        // Durable rows need no acknowledgement, so keep reconciled session
        // findings newest in the bounded status inventory. The complete taboo
        // map remains effective even when an older row is omitted from status.
        let mut order = configured_order;
        for identity in &previous.order {
            let Some(keys) = keys_by_identity.get(identity) else {
                continue;
            };
            for key in keys {
                if taboo.get(key).is_some_and(|record| !record.durable) {
                    order.retain(|candidate| candidate != key);
                    order.push(key.clone());
                }
            }
        }
        if order.len() > MAX_TABOO_STATUS_ENTRIES {
            order.drain(..order.len() - MAX_TABOO_STATUS_ENTRIES);
        }
        self.taboo = taboo;
        self.taboo_order = order;
    }

    fn is_taboo(&self, playlist_id: &str, entry_id: &str) -> bool {
        self.taboo.contains_key(&EntryKey {
            playlist_id: playlist_id.to_string(),
            entry_id: entry_id.to_string(),
        })
    }

    fn taboo_fallback_diagnostic(&self, playlist_id: &str, entry_id: &str) -> Option<String> {
        let record = self.taboo.get(&EntryKey {
            playlist_id: playlist_id.to_string(),
            entry_id: entry_id.to_string(),
        })?;
        Some(truncate_middle(
            &format!(
                "entry {entry_id:?} in playlist {playlist_id:?} is taboo this session ({}): {}; paired still is active and motion was not started",
                record.source, record.reason
            ),
            MAX_LAST_ERROR_BYTES,
        ))
    }

    fn is_independent(&self) -> bool {
        self.config.settings.display_mode == DisplayMode::Independent
    }

    fn route_decision(
        &self,
        connector: &str,
        manual: Option<&str>,
        at: NaiveDateTime,
    ) -> Result<(String, RouteSource, Option<String>), String> {
        if let Some(manual) = manual {
            return Ok((manual.to_string(), RouteSource::Manual, None));
        }
        if let Some(rule) = schedule::resolve_rule_for(&self.config.schedules, Some(connector), at)
            .map_err(|error| error.to_string())?
        {
            return Ok((
                rule.playlist.clone(),
                RouteSource::Schedule,
                Some(rule.id.clone()),
            ));
        }
        if let Some(assignment) = self
            .config
            .displays
            .iter()
            .find(|assignment| assignment.connector == connector)
        {
            return Ok((assignment.playlist.clone(), RouteSource::Assignment, None));
        }
        Ok((
            self.config.default_playlist.clone(),
            RouteSource::Default,
            None,
        ))
    }

    fn new_route_cursor(
        &mut self,
        playlist_id: &str,
        current_entry: Option<&str>,
        shuffle: bool,
        automatic: bool,
    ) -> Result<PlaylistCursor, String> {
        let playlist = self
            .config
            .playlist(playlist_id)
            .ok_or_else(|| format!("display route playlist {playlist_id:?} is missing"))?;
        let entry_ids: Vec<String> = playlist
            .entries
            .iter()
            .map(|entry| entry.id.clone())
            .collect();
        if entry_ids.is_empty() {
            return Err(format!("display route playlist {playlist_id:?} is empty"));
        }
        let current = current_entry
            .and_then(|wanted| entry_ids.iter().position(|entry| entry == wanted))
            .or_else(|| {
                automatic.then(|| {
                    entry_ids
                        .iter()
                        .position(|entry| !self.is_taboo(playlist_id, entry))
                        .unwrap_or(0)
                })
            })
            .unwrap_or(0);
        let (order, position) = if shuffle {
            let mut rest: Vec<usize> = (0..entry_ids.len())
                .filter(|index| *index != current)
                .collect();
            self.rng.shuffle(&mut rest);
            let mut order = Vec::with_capacity(entry_ids.len());
            order.push(current);
            order.extend(rest);
            (order, 0)
        } else {
            ((0..entry_ids.len()).collect(), current)
        };
        Ok(PlaylistCursor {
            order,
            position,
            history: Vec::new(),
            forward: Vec::new(),
        })
    }

    fn refresh_route_decision(&mut self, connector: &str, automatic: bool) -> Result<bool, String> {
        let manual = self
            .routes
            .get(connector)
            .and_then(|route| route.manual_playlist.as_deref());
        let (wanted, source, rule_id) =
            self.route_decision(connector, manual, self.current_time)?;
        let changed = self
            .routes
            .get(connector)
            .is_none_or(|route| route.active_playlist != wanted);
        let new_cursor = if changed {
            let shuffle = self
                .routes
                .get(connector)
                .map_or(self.config.settings.shuffle, |route| {
                    route.shuffle_enabled(self.config.settings.shuffle)
                });
            Some(self.new_route_cursor(&wanted, None, shuffle, automatic)?)
        } else {
            None
        };
        let route = self
            .routes
            .get_mut(connector)
            .ok_or_else(|| format!("display route {connector:?} is missing"))?;
        route.source = source;
        route.schedule_rule_id = rule_id;
        if let Some(cursor) = new_cursor {
            route.active_playlist = wanted;
            route.cursor = cursor;
        }
        Ok(changed)
    }

    fn reconcile_independent_routes(&mut self, outputs: &[String]) -> Result<(), String> {
        let mut connected = outputs.to_vec();
        connected.retain(|connector| !connector.is_empty());
        connected.sort();
        connected.dedup();
        if connected.len() > MAX_LIVE_DISPLAY_ROUTES {
            return Err(format!(
                "more than {MAX_LIVE_DISPLAY_ROUTES} live display routes are unsupported"
            ));
        }
        let connected_set: HashSet<&str> = connected.iter().map(String::as_str).collect();
        self.pending_routes
            .retain(|connector, _| connected_set.contains(connector.as_str()));
        for connector in &connected {
            self.route_generation = self.route_generation.wrapping_add(1);
            if let Some(route) = self.routes.get_mut(connector) {
                route.last_seen = self.route_generation;
            } else {
                let (playlist, source, schedule_rule_id) =
                    self.route_decision(connector, None, self.current_time)?;
                let cursor =
                    self.new_route_cursor(&playlist, None, self.config.settings.shuffle, true)?;
                self.routes.insert(
                    connector.clone(),
                    DisplayRoute {
                        manual_playlist: None,
                        active_playlist: playlist,
                        schedule_rule_id,
                        source,
                        cursor,
                        playback_state: PlaybackState::Playing,
                        shuffle_override: None,
                        cycle_override: None,
                        renderer_failed: false,
                        last_error: String::new(),
                        last_cycle: Instant::now(),
                        last_seen: self.route_generation,
                    },
                );
            }
            self.refresh_route_decision(connector, true)?;
        }
        while self
            .routes
            .keys()
            .filter(|connector| !connected_set.contains(connector.as_str()))
            .count()
            > MAX_RETAINED_DISPLAY_ROUTES
        {
            let Some(evicted) = self
                .routes
                .iter()
                .filter(|(connector, _)| !connected_set.contains(connector.as_str()))
                .min_by_key(|(_, route)| route.last_seen)
                .map(|(connector, _)| connector.clone())
            else {
                return Err("could not evict a detached display route".into());
            };
            self.routes.remove(&evicted);
        }
        Ok(())
    }

    fn rebuild_independent_routes(
        &mut self,
        previous_entries: &HashMap<String, (String, String)>,
    ) -> Result<(), String> {
        let connectors: Vec<String> = self.routes.keys().cloned().collect();
        for connector in connectors {
            let manual = self
                .routes
                .get(&connector)
                .and_then(|route| route.manual_playlist.as_ref())
                .filter(|playlist| self.config.playlist(playlist).is_some())
                .cloned();
            self.routes
                .get_mut(&connector)
                .expect("connector came from route keys")
                .manual_playlist = manual.clone();
            let (playlist, source, schedule_rule_id) =
                self.route_decision(&connector, manual.as_deref(), self.current_time)?;
            let current = previous_entries
                .get(&connector)
                .and_then(|(previous, entry)| (previous == &playlist).then_some(entry.as_str()));
            let shuffle = self.routes[&connector].shuffle_enabled(self.config.settings.shuffle);
            let cursor = self.new_route_cursor(&playlist, current, shuffle, false)?;
            let route = self
                .routes
                .get_mut(&connector)
                .expect("connector came from route keys");
            route.active_playlist = playlist;
            route.source = source;
            route.schedule_rule_id = schedule_rule_id;
            route.cursor = cursor;
        }
        let outputs = self.target_outputs.clone();
        self.reconcile_independent_routes(&outputs)
    }

    fn effective_theme_source(&self) -> Option<&str> {
        if !self.is_independent() {
            return None;
        }
        let configured = self.config.settings.theme_source_connector.as_str();
        if self
            .target_outputs
            .iter()
            .any(|connector| connector == configured)
        {
            Some(configured)
        } else {
            self.target_outputs
                .iter()
                .find(|connector| !connector.is_empty())
                .map(String::as_str)
        }
    }

    fn refresh_independent_last_error(&mut self) {
        if !self.is_independent() {
            return;
        }
        let mut connectors = self.target_outputs.clone();
        connectors.sort();
        let messages: Vec<String> = connectors
            .iter()
            .filter_map(|connector| {
                let route = self.routes.get(connector)?;
                (!route.last_error.is_empty()).then(|| format!("{connector}: {}", route.last_error))
            })
            .collect();
        self.last_error = bounded_failure_summary(&messages);
        // This field belongs to the mirrored compatibility runtime. Independent
        // health is deliberately connector-scoped on DisplayRoute.
        self.renderer_failed = false;
    }

    fn independent_selection_snapshot(
        &self,
        connectors: &[String],
    ) -> IndependentSelectionSnapshot {
        IndependentSelectionSnapshot {
            routes: connectors
                .iter()
                .filter_map(|connector| {
                    self.routes
                        .get(connector)
                        .cloned()
                        .map(|route| (connector.clone(), route))
                })
                .collect(),
        }
    }

    fn restore_independent_selection(&mut self, snapshot: &IndependentSelectionSnapshot) {
        for (connector, route) in &snapshot.routes {
            self.routes.insert(connector.clone(), route.clone());
        }
    }

    fn apply_independent_transaction(
        &mut self,
        selected: &HashSet<String>,
        snapshot: &IndependentSelectionSnapshot,
        action: &str,
    ) -> Result<String, String> {
        match self.apply_independent_selected(selected) {
            Ok(message) => Ok(message),
            Err(error) => {
                self.restore_independent_selection(snapshot);
                let rollback = self.apply_independent_selected(selected).err();
                let rollback_failed = rollback.is_some();
                let diagnostic = match rollback.as_deref() {
                    Some(rollback) => {
                        format!("{action} failed: {error}; rollback also failed: {rollback}")
                    }
                    None => format!("{action} failed: {error}; previous wallpaper restored"),
                };
                let diagnostic = truncate_middle(&diagnostic, MAX_LAST_ERROR_BYTES);
                for connector in selected {
                    if let Some(route) = self.routes.get_mut(connector) {
                        route.last_error.clone_from(&diagnostic);
                        route.renderer_failed = rollback_failed;
                    }
                }
                self.refresh_independent_last_error();
                Err(diagnostic)
            }
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
                && !output.chars().any(char::is_whitespace)
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
        let independent = self.is_independent();
        self.target_outputs = match discovered {
            Ok(outputs) => {
                self.live_outputs = Some(outputs.clone());
                self.output_discovery_error.clear();
                if independent {
                    outputs
                } else if self.config.displays.is_empty() {
                    vec![String::new()]
                } else {
                    outputs
                }
            }
            Err(error) => {
                self.output_discovery_error =
                    truncate_middle(&error, MAX_OUTPUT_DISCOVERY_ERROR_BYTES);
                if independent {
                    if let Some(outputs) = &self.live_outputs {
                        outputs.clone()
                    } else {
                        self.config
                            .displays
                            .iter()
                            .map(|display| display.connector.clone())
                            .collect()
                    }
                } else if self.config.displays.is_empty() {
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
        self.target_outputs.sort();
        self.target_outputs.dedup();
        if independent {
            let outputs = self.target_outputs.clone();
            self.reconcile_independent_routes(&outputs)?;
            let selected: HashSet<String> = outputs.iter().cloned().collect();
            return self.apply_independent_in_batch(&selected);
        }
        let targets = self.current_targets(&self.target_outputs);
        let outputs: Vec<String> = targets.iter().map(|target| target.output.clone()).collect();
        self.driver.retain_outputs(&outputs);
        self.last_apply_failures.clear();
        if targets.is_empty() {
            return self.fail("active display playlists are empty");
        }
        let played = targets[0].entry.id.clone();
        let mut errors = Vec::new();
        let mut static_fallbacks = Vec::new();
        let mut base_settings = self.config.settings.clone();
        if self.playback_state == PlaybackState::Stopped {
            base_settings.dynamics_enabled = false;
        }
        for target in targets {
            let mut settings = base_settings.clone();
            if let Some(diagnostic) =
                self.taboo_fallback_diagnostic(&target.playlist_id, &target.entry.id)
            {
                settings.dynamics_enabled = false;
                static_fallbacks.push(if target.output.is_empty() {
                    diagnostic
                } else {
                    format!("{}: {diagnostic}", target.output)
                });
            }
            if let Err(error) = self.driver.apply(&target.entry, &target.output, &settings) {
                self.last_apply_failures.push(ApplyFailure {
                    key: EntryKey {
                        playlist_id: target.playlist_id,
                        entry_id: target.entry.id,
                    },
                    output: target.output.clone(),
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
            if static_fallbacks.is_empty() {
                self.last_error.clear();
                self.renderer_failed = false;
            } else {
                self.last_error = bounded_failure_summary(&static_fallbacks);
                self.renderer_failed = true;
            }
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

    fn apply_independent_selected(&mut self, selected: &HashSet<String>) -> Result<String, String> {
        self.last_output_probe = Instant::now();
        self.driver.begin_apply();
        let previously_known = self
            .live_outputs
            .clone()
            .unwrap_or_else(|| self.target_outputs.clone());
        let discovered = self.probe_outputs();
        self.target_outputs = match discovered {
            Ok(mut outputs) => {
                outputs.sort();
                outputs.dedup();
                self.live_outputs = Some(outputs.clone());
                self.output_discovery_error.clear();
                outputs
            }
            Err(error) => {
                self.output_discovery_error =
                    truncate_middle(&error, MAX_OUTPUT_DISCOVERY_ERROR_BYTES);
                self.live_outputs.clone().unwrap_or_else(|| {
                    self.config
                        .displays
                        .iter()
                        .map(|display| display.connector.clone())
                        .collect()
                })
            }
        };
        self.target_outputs.sort();
        self.target_outputs.dedup();
        let outputs = self.target_outputs.clone();
        let newly_connected: Vec<String> = outputs
            .iter()
            .filter(|connector| !previously_known.contains(connector))
            .cloned()
            .collect();
        let result = self.reconcile_independent_routes(&outputs).and_then(|()| {
            if selected
                .iter()
                .any(|connector| !outputs.contains(connector))
            {
                Err("a selected display disconnected before the command applied".into())
            } else {
                self.apply_independent_in_batch(selected)
            }
        });
        self.driver.end_apply();
        for connector in newly_connected {
            if selected.contains(&connector) || self.pending_routes.contains_key(&connector) {
                continue;
            }
            if let Some(route) = self.routes.get(&connector).cloned() {
                self.start_route_automatic_with_policy(
                    connector,
                    route.clone(),
                    route,
                    "hotplug",
                    false,
                    Instant::now(),
                );
            }
        }
        result
    }

    fn apply_independent_in_batch(&mut self, selected: &HashSet<String>) -> Result<String, String> {
        let targets = self.current_targets(&self.target_outputs);
        if targets.is_empty() {
            return self.fail("active display playlists are empty");
        }
        let all_outputs: Vec<String> = targets.iter().map(|target| target.output.clone()).collect();
        self.driver.retain_outputs(&all_outputs);
        let mut targets: Vec<Target> = targets
            .into_iter()
            .filter(|target| selected.contains(&target.output))
            .collect();
        if targets.is_empty() {
            return self.fail("none of the selected display routes has a playable entry");
        }
        for output in selected {
            self.driver.stop_output_renderer(output);
        }

        let theme_source = self.effective_theme_source().map(str::to_owned);
        targets.sort_by_key(|target| {
            theme_source
                .as_deref()
                .is_some_and(|connector| connector == target.output)
        });
        self.last_apply_failures.clear();
        let mut errors = Vec::new();
        let mut blocked = HashSet::new();
        let mut static_fallbacks: HashMap<String, String> = HashMap::new();
        for target in &targets {
            if let Err(error) =
                self.driver
                    .apply_still_only(&target.entry, &target.output, &self.config.settings)
            {
                self.last_apply_failures.push(ApplyFailure {
                    key: EntryKey {
                        playlist_id: target.playlist_id.clone(),
                        entry_id: target.entry.id.clone(),
                    },
                    output: target.output.clone(),
                    reason: error.clone(),
                });
                errors.push(format!("{} still: {error}", target.output));
                blocked.insert(target.output.clone());
            }
        }
        if let Some(theme_source) = theme_source
            .as_deref()
            .filter(|connector| selected.contains(*connector))
        {
            if let Some(target) = targets
                .iter()
                .find(|target| target.output == theme_source)
                .filter(|target| !blocked.contains(&target.output))
            {
                if let Err(error) = self.driver.apply_palette_only(&target.entry) {
                    self.last_apply_failures.push(ApplyFailure {
                        key: EntryKey {
                            playlist_id: target.playlist_id.clone(),
                            entry_id: target.entry.id.clone(),
                        },
                        output: target.output.clone(),
                        reason: error.clone(),
                    });
                    errors.push(format!("{theme_source} palette: {error}"));
                    blocked.insert(target.output.clone());
                }
            }
        }

        for target in &targets {
            if blocked.contains(&target.output) {
                continue;
            }
            let Some(route) = self.routes.get(&target.output) else {
                continue;
            };
            let mut settings = self.config.settings.clone();
            if route.playback_state == PlaybackState::Stopped {
                settings.dynamics_enabled = false;
            }
            if let Some(diagnostic) =
                self.taboo_fallback_diagnostic(&target.playlist_id, &target.entry.id)
            {
                settings.dynamics_enabled = false;
                static_fallbacks.insert(target.output.clone(), diagnostic);
            }
            if let Err(error) =
                self.driver
                    .start_motion_only(&target.entry, &target.output, &settings)
            {
                self.last_apply_failures.push(ApplyFailure {
                    key: EntryKey {
                        playlist_id: target.playlist_id.clone(),
                        entry_id: target.entry.id.clone(),
                    },
                    output: target.output.clone(),
                    reason: error.clone(),
                });
                errors.push(format!("{} renderer: {error}", target.output));
                continue;
            }
            if route.playback_state == PlaybackState::Paused
                && settings.dynamics_enabled
                && target.entry.kind != crate::config::EntryKind::Still
            {
                if let Err(error) = self.driver.set_output_paused(&target.output, true) {
                    errors.push(format!("{} pause: {error}", target.output));
                }
            }
        }
        let mut route_errors: HashMap<&str, Vec<&str>> = HashMap::new();
        for error in &errors {
            if let Some((connector, _)) = error.split_once(' ') {
                route_errors.entry(connector).or_default().push(error);
            }
        }
        for connector in selected {
            let Some(route) = self.routes.get_mut(connector) else {
                continue;
            };
            if let Some(route_failures) = route_errors.get(connector.as_str()) {
                route.last_error = bounded_failure_summary(
                    &route_failures
                        .iter()
                        .map(|failure| (*failure).to_string())
                        .collect::<Vec<_>>(),
                );
                route.renderer_failed = true;
            } else if let Some(diagnostic) = static_fallbacks.get(connector) {
                route.last_error.clone_from(diagnostic);
                route.renderer_failed = true;
            } else {
                route.last_error.clear();
                route.renderer_failed = false;
            }
        }
        self.refresh_independent_last_error();
        if errors.is_empty() {
            self.supersede_startup_apply();
            Ok(format!("applied {} display route(s)", selected.len()))
        } else {
            Err(errors.join("; "))
        }
    }

    fn current_targets(&self, outputs: &[String]) -> Vec<Target> {
        let mut targets = Vec::new();
        if self.is_independent() {
            for output in outputs {
                let Some(route) = self.routes.get(output) else {
                    continue;
                };
                let Some(playlist) = self.config.playlist(&route.active_playlist) else {
                    continue;
                };
                let Some(index) = route.cursor.order.get(route.cursor.position) else {
                    continue;
                };
                if let Some(entry) = playlist.entries.get(*index).cloned() {
                    targets.push(Target {
                        playlist_id: playlist.id.clone(),
                        entry,
                        output: output.clone(),
                    });
                }
            }
        } else if self.config.displays.is_empty() {
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

    fn parse_on(argument: Option<&str>) -> Result<(String, Request), String> {
        let argument = argument.ok_or(
            "usage: on <connector> <playlist-use|schedule-follow|play|pause|toggle|stop|shuffle|cycle|next|previous|random> [argument]",
        )?;
        if argument.chars().any(char::is_control) {
            return Err("on command cannot contain control characters".into());
        }
        let trimmed = argument.trim();
        if trimmed != argument {
            return Err("on command cannot have leading or trailing whitespace".into());
        }
        let first = trimmed
            .find(char::is_whitespace)
            .ok_or("usage: on <connector> <runtime verb> [argument]")?;
        let connector = &trimmed[..first];
        let remainder = trimmed[first..].trim_start();
        let (verb, nested_argument) = match remainder.find(char::is_whitespace) {
            Some(index) => {
                let argument = remainder[index..].trim();
                (
                    &remainder[..index],
                    (!argument.is_empty()).then(|| argument.to_string()),
                )
            }
            None => (remainder, None),
        };
        if connector.is_empty() || verb.is_empty() {
            return Err("usage: on <connector> <runtime verb> [argument]".into());
        }
        Ok((
            connector.to_string(),
            Request {
                verb: verb.to_string(),
                argument: nested_argument,
            },
        ))
    }

    fn handle_on(&mut self, argument: Option<&str>, at: NaiveDateTime) -> Result<String, String> {
        if !self.is_independent() {
            return Err("on is available only in independent display mode".into());
        }
        let (connector, nested) = Self::parse_on(argument)?;
        if matches!(
            nested.verb.as_str(),
            "status" | "reload" | "quit" | "on" | "config"
        ) {
            return Err(format!(
                "runtime verb {:?} cannot be targeted with on",
                nested.verb
            ));
        }
        if !self.target_outputs.iter().any(|known| known == &connector) {
            return Err(format!("unknown or disconnected connector {connector:?}"));
        }
        self.handle_independent_command(Some(&connector), &nested, at)
    }

    fn independent_connectors(&self, connector: Option<&str>) -> Result<Vec<String>, String> {
        let connectors = match connector {
            Some(connector) => vec![connector.to_string()],
            None => self
                .target_outputs
                .iter()
                .filter(|connector| !connector.is_empty())
                .cloned()
                .collect(),
        };
        if connectors.is_empty() {
            Err("no connected display routes are available".into())
        } else {
            Ok(connectors)
        }
    }

    fn independent_no_argument(request: &Request, usage: &str) -> Result<(), String> {
        if request.argument.is_some() {
            Err(format!("usage: {usage}"))
        } else {
            Ok(())
        }
    }

    fn handle_independent_command(
        &mut self,
        connector: Option<&str>,
        request: &Request,
        _at: NaiveDateTime,
    ) -> Result<String, String> {
        let connectors = self.independent_connectors(connector)?;
        let snapshot = self.independent_selection_snapshot(&connectors);
        match request.verb.as_str() {
            "playlist-use" => {
                let reference = request
                    .argument
                    .as_deref()
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
                    .ok_or("usage: playlist-use <name>")?;
                let playlist = self
                    .config
                    .playlist(reference)
                    .ok_or_else(|| format!("no such playlist {reference:?}"))?;
                let playlist_id = playlist.id.clone();
                self.cancel_route_retries(&connectors);
                let mut changed = HashSet::new();
                let now = Instant::now();
                for connector in &connectors {
                    let already_active = self.routes.get(connector).is_some_and(|route| {
                        route.manual_playlist.as_deref() == Some(playlist_id.as_str())
                            && route.active_playlist == playlist_id
                            && route.playback_state == PlaybackState::Playing
                            && !route.renderer_failed
                    }) && self.route_current_entry(connector).is_some();
                    if already_active {
                        self.routes
                            .get_mut(connector)
                            .expect("route was checked")
                            .last_cycle = now;
                        continue;
                    }
                    let shuffle = self
                        .routes
                        .get(connector)
                        .ok_or_else(|| format!("display route {connector:?} is missing"))?
                        .shuffle_enabled(self.config.settings.shuffle);
                    let cursor = self.new_route_cursor(&playlist_id, None, shuffle, false)?;
                    let route = self.routes.get_mut(connector).expect("route was checked");
                    route.manual_playlist = Some(playlist_id.clone());
                    route.active_playlist = playlist_id.clone();
                    route.schedule_rule_id = None;
                    route.source = RouteSource::Manual;
                    route.cursor = cursor;
                    route.last_cycle = now;
                    changed.insert(connector.clone());
                }
                if changed.is_empty() {
                    Ok("playlist override already active".into())
                } else {
                    self.apply_independent_transaction(&changed, &snapshot, "playlist override")
                }
            }
            "schedule-follow" => {
                Self::independent_no_argument(request, "schedule-follow")?;
                self.cancel_route_retries(&connectors);
                let mut changed_routes = HashSet::new();
                for connector in &connectors {
                    let route = self
                        .routes
                        .get(connector)
                        .expect("connector came from active routes");
                    let (wanted, source, rule_id) =
                        self.route_decision(connector, None, self.current_time)?;
                    let unchanged = route.manual_playlist.is_none()
                        && route.active_playlist == wanted
                        && route.source == source
                        && route.schedule_rule_id == rule_id;
                    if unchanged {
                        continue;
                    }
                    self.routes
                        .get_mut(connector)
                        .expect("connector came from active routes")
                        .manual_playlist = None;
                    self.refresh_route_decision(connector, true)?;
                    self.routes
                        .get_mut(connector)
                        .expect("route exists")
                        .last_cycle = Instant::now();
                    changed_routes.insert(connector.clone());
                }
                if changed_routes.is_empty() {
                    Ok("already following schedule".into())
                } else {
                    self.apply_independent_transaction(
                        &changed_routes,
                        &snapshot,
                        "return to schedule",
                    )
                }
            }
            "play" => {
                Self::independent_no_argument(request, "play")?;
                self.cancel_route_retries(&connectors);
                self.independent_play(&connectors)
            }
            "pause" => {
                Self::independent_no_argument(request, "pause")?;
                self.cancel_route_retries(&connectors);
                self.independent_pause(&connectors)
            }
            "toggle" => {
                Self::independent_no_argument(request, "toggle")?;
                self.cancel_route_retries(&connectors);
                if connectors.iter().any(|one| {
                    self.routes
                        .get(one)
                        .is_some_and(|route| route.playback_state == PlaybackState::Playing)
                }) {
                    self.independent_pause(&connectors)?;
                } else {
                    self.independent_play(&connectors)?;
                }
                Ok("toggled selected display routes".into())
            }
            "stop" => {
                Self::independent_no_argument(request, "stop")?;
                self.cancel_route_retries(&connectors);
                let changed: HashSet<String> = connectors
                    .iter()
                    .filter(|connector| {
                        self.routes[*connector].playback_state != PlaybackState::Stopped
                    })
                    .cloned()
                    .collect();
                if changed.is_empty() {
                    Ok("selected display motion is already stopped".into())
                } else {
                    for connector in &connectors {
                        if !changed.contains(connector) {
                            continue;
                        }
                        if self.routes[connector].playback_state == PlaybackState::Paused {
                            let _ = self.driver.set_output_paused(connector, false);
                        }
                        self.routes
                            .get_mut(connector)
                            .expect("route exists")
                            .playback_state = PlaybackState::Stopped;
                    }
                    self.apply_independent_transaction(&changed, &snapshot, "stop motion")?;
                    Ok("stopped selected display motion; paired stills remain active".into())
                }
            }
            "shuffle" => {
                let override_value = Self::runtime_boolean_override(
                    request.argument.as_deref(),
                    "shuffle on|off|default",
                )?;
                self.cancel_route_retries(&connectors);
                for connector in &connectors {
                    let current = self
                        .route_current_entry(connector)
                        .map(|entry| entry.id.clone());
                    let before =
                        self.routes[connector].shuffle_enabled(self.config.settings.shuffle);
                    self.routes
                        .get_mut(connector)
                        .expect("route exists")
                        .shuffle_override = override_value;
                    let after =
                        self.routes[connector].shuffle_enabled(self.config.settings.shuffle);
                    if before != after {
                        let playlist = self.routes[connector].active_playlist.clone();
                        let cursor =
                            self.new_route_cursor(&playlist, current.as_deref(), after, false)?;
                        self.routes.get_mut(connector).expect("route exists").cursor = cursor;
                    }
                }
                Ok("shuffle updated for selected display routes".into())
            }
            "cycle" => {
                let override_value = Self::runtime_boolean_override(
                    request.argument.as_deref(),
                    "cycle on|off|default",
                )?;
                self.cancel_route_retries(&connectors);
                for connector in &connectors {
                    let before =
                        self.routes[connector].cycle_enabled(self.config.settings.cycle_enabled);
                    let route = self.routes.get_mut(connector).expect("route exists");
                    route.cycle_override = override_value;
                    let after = route.cycle_enabled(self.config.settings.cycle_enabled);
                    if after && !before {
                        route.last_cycle = Instant::now();
                    }
                }
                Ok("cycle updated for selected display routes".into())
            }
            "next" | "previous" | "random" => {
                Self::independent_no_argument(request, request.verb.as_str())?;
                let mut changed = HashSet::new();
                for connector in &connectors {
                    let moved = match request.verb.as_str() {
                        "next" => self.move_route_forward(connector, false),
                        "previous" => self.move_route_backward(connector),
                        "random" => self.move_route_random(connector),
                        _ => unreachable!(),
                    };
                    if moved {
                        changed.insert(connector.clone());
                    }
                }
                if changed.is_empty() {
                    self.restore_independent_selection(&snapshot);
                    return Err(if request.verb == "previous" {
                        "no previous playback history"
                    } else {
                        "selected display playlists are empty or taboo"
                    }
                    .into());
                }
                self.cancel_route_retries(&connectors);
                self.apply_independent_transaction(&changed, &snapshot, request.verb.as_str())?;
                let now = Instant::now();
                for connector in &changed {
                    self.routes
                        .get_mut(connector)
                        .expect("route exists")
                        .last_cycle = now;
                }
                Ok(format!("{} selected display routes", request.verb))
            }
            _ => Err(format!(
                "unknown targetable runtime verb {:?}",
                request.verb
            )),
        }
        .map(|message| {
            let scope = connector.map_or("all displays".to_string(), |one| one.to_string());
            format!("{scope}: {message}")
        })
    }

    fn runtime_boolean_override(value: Option<&str>, usage: &str) -> Result<Option<bool>, String> {
        match value.map(str::trim).map(str::to_ascii_lowercase).as_deref() {
            Some("on" | "true" | "1") => Ok(Some(true)),
            Some("off" | "false" | "0") => Ok(Some(false)),
            Some("default" | "config" | "follow") => Ok(None),
            _ => Err(format!("usage: {usage}")),
        }
    }

    fn cancel_route_retries(&mut self, connectors: &[String]) {
        for connector in connectors {
            self.pending_routes.remove(connector);
        }
    }

    fn route_current_entry(&self, connector: &str) -> Option<&Entry> {
        let route = self.routes.get(connector)?;
        let playlist = self.config.playlist(&route.active_playlist)?;
        route
            .cursor
            .order
            .get(route.cursor.position)
            .and_then(|index| playlist.entries.get(*index))
    }

    fn independent_play(&mut self, connectors: &[String]) -> Result<String, String> {
        let mut resume = Vec::new();
        let mut restart_connectors = Vec::new();
        for connector in connectors {
            let route = &self.routes[connector];
            match (route.playback_state, route.renderer_failed) {
                (PlaybackState::Playing, false) => {}
                (PlaybackState::Paused, false) => resume.push(connector.clone()),
                _ => restart_connectors.push(connector.clone()),
            }
        }
        let snapshot = self.independent_selection_snapshot(&restart_connectors);
        let restart: HashSet<String> = restart_connectors.iter().cloned().collect();
        let mut errors = Vec::new();
        for connector in &resume {
            if let Err(error) = self.driver.set_output_paused(connector, false) {
                let route = self.routes.get_mut(connector).expect("route exists");
                route.last_error = truncate_middle(&error, MAX_LAST_ERROR_BYTES);
                errors.push(format!("{connector}: {error}"));
            } else {
                let route = self.routes.get_mut(connector).expect("route exists");
                route.playback_state = PlaybackState::Playing;
                route.last_error.clear();
            }
        }
        if !restart.is_empty() {
            for connector in &restart_connectors {
                self.routes
                    .get_mut(connector)
                    .expect("route exists")
                    .playback_state = PlaybackState::Playing;
            }
            if let Err(error) =
                self.apply_independent_transaction(&restart, &snapshot, "resume motion")
            {
                errors.push(error);
            } else {
                let now = Instant::now();
                for connector in connectors {
                    self.routes
                        .get_mut(connector)
                        .expect("route exists")
                        .last_cycle = now;
                }
            }
        }
        self.refresh_independent_last_error();
        if errors.is_empty() {
            Ok("playing selected display routes".into())
        } else {
            Err(errors.join("; "))
        }
    }

    fn independent_pause(&mut self, connectors: &[String]) -> Result<String, String> {
        let mut errors = Vec::new();
        for connector in connectors {
            if self.routes[connector].playback_state != PlaybackState::Playing {
                continue;
            }
            if let Err(error) = self.driver.set_output_paused(connector, true) {
                self.routes
                    .get_mut(connector)
                    .expect("route exists")
                    .last_error = truncate_middle(&error, MAX_LAST_ERROR_BYTES);
                errors.push(format!("{connector}: {error}"));
            } else {
                let route = self.routes.get_mut(connector).expect("route exists");
                route.playback_state = PlaybackState::Paused;
                route.last_error.clear();
            }
        }
        self.refresh_independent_last_error();
        if errors.is_empty() {
            Ok("paused selected display routes".into())
        } else {
            Err(errors.join("; "))
        }
    }

    fn move_route_forward(&mut self, connector: &str, automatic: bool) -> bool {
        let Some(route) = self.routes.get(connector) else {
            return false;
        };
        let playlist_id = route.active_playlist.clone();
        let Some(playlist) = self.config.playlist(&playlist_id) else {
            return false;
        };
        let entry_ids: Vec<String> = playlist
            .entries
            .iter()
            .map(|entry| entry.id.clone())
            .collect();
        let eligible: Vec<bool> = entry_ids
            .iter()
            .map(|entry| !automatic || !self.is_taboo(&playlist_id, entry))
            .collect();
        let shuffle = route.shuffle_enabled(self.config.settings.shuffle);
        let route = self.routes.get_mut(connector).expect("route exists");
        let cursor = &mut route.cursor;
        let Some(&current) = cursor.order.get(cursor.position) else {
            return false;
        };
        while let Some(index) = cursor.forward.pop() {
            if eligible.get(index).copied().unwrap_or(false) {
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
        if shuffle {
            if let Some(position) = ((cursor.position + 1)..cursor.order.len())
                .find(|position| eligible[cursor.order[*position]])
            {
                push_bounded(&mut cursor.history, current);
                cursor.position = position;
                cursor.forward.clear();
                return cursor.order[position] != current;
            }
            let mut next: Vec<usize> = (0..entry_ids.len())
                .filter(|index| eligible[*index])
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
                if eligible[index] && index != current {
                    push_bounded(&mut cursor.history, current);
                    cursor.position = position;
                    cursor.forward.clear();
                    return true;
                }
            }
            false
        }
    }

    fn move_route_backward(&mut self, connector: &str) -> bool {
        let Some(route) = self.routes.get_mut(connector) else {
            return false;
        };
        let cursor = &mut route.cursor;
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

    fn move_route_random(&mut self, connector: &str) -> bool {
        let Some(route) = self.routes.get(connector) else {
            return false;
        };
        let playlist_id = route.active_playlist.clone();
        let Some(playlist) = self.config.playlist(&playlist_id) else {
            return false;
        };
        let eligible: Vec<usize> = playlist
            .entries
            .iter()
            .enumerate()
            .filter_map(|(index, entry)| (!self.is_taboo(&playlist_id, &entry.id)).then_some(index))
            .collect();
        let shuffle = route.shuffle_enabled(self.config.settings.shuffle);
        let route = self.routes.get_mut(connector).expect("route exists");
        let cursor = &mut route.cursor;
        let Some(&current) = cursor.order.get(cursor.position) else {
            return false;
        };
        if shuffle {
            let remaining: Vec<usize> = ((cursor.position + 1)..cursor.order.len())
                .filter(|position| eligible.contains(&cursor.order[*position]))
                .collect();
            if remaining.is_empty() {
                let mut next = eligible;
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
            let chosen = remaining[self.rng.index(remaining.len())];
            let next = cursor.position + 1;
            cursor.order.swap(next, chosen);
            push_bounded(&mut cursor.history, current);
            cursor.forward.clear();
            cursor.position = next;
            true
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
            true
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
        let mode_changed = next.settings.display_mode != self.config.settings.display_mode;
        let old_targets = self.current_targets(&self.target_outputs);
        let old_entries = self.current_entry_ids();
        let old_route_entries: HashMap<String, (String, String)> = self
            .routes
            .iter()
            .filter_map(|(connector, route)| {
                self.route_current_entry(connector).map(|entry| {
                    (
                        connector.clone(),
                        (route.active_playlist.clone(), entry.id.clone()),
                    )
                })
            })
            .collect();
        let next_manual = (!mode_changed)
            .then(|| {
                self.manual_playlist
                    .as_ref()
                    .filter(|manual| next.playlist(manual).is_some())
                    .cloned()
            })
            .flatten();
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
        let display_routing_changed = next.settings.display_mode
            != self.config.settings.display_mode
            || next.settings.theme_source_connector != self.config.settings.theme_source_connector
            || next.schedules != self.config.schedules;
        let nondurable_findings = self.snapshot_nondurable_findings();

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
            shuffle_override: self.shuffle_override,
            cycle_override: self.cycle_override,
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
            routes: self.routes.clone(),
            route_generation: self.route_generation,
            current_time: self.current_time,
            config_epoch: self.config_epoch,
        };
        if mode_changed {
            self.playback_state = PlaybackState::Playing;
            self.shuffle_override = None;
            self.cycle_override = None;
            if self.is_independent() {
                self.routes.clear();
            }
        }
        // The candidate epoch becomes visible only if this transaction reaches
        // the successful return. Rollback restores the snapshot's epoch.
        self.config_epoch = self.config_epoch.saturating_add(1);
        self.reconcile_configured_taboo(&nondurable_findings);
        if let Err(error) = self.rebuild_cursors(&old_entries) {
            self.restore_reload_snapshot(snapshot);
            return Err(error);
        }
        if self.is_independent() {
            if let Err(error) = self.rebuild_independent_routes(&old_route_entries) {
                self.restore_reload_snapshot(snapshot);
                return Err(error);
            }
        }

        let new_targets = self.current_targets(&self.target_outputs);
        let targets_changed = !targets_equal_ignoring_taboo(&old_targets, &new_targets);
        let residency_changed = snapshot.active_playlist != self.active_playlist
            || snapshot.manual_playlist != self.manual_playlist
            || snapshot.schedule_overrode_default != self.schedule_overrode_default
            || targets_changed;
        let apply_needed = renderer_changed
            || dynamics_changed
            || displays_changed
            || display_routing_changed
            || targets_changed;
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
        self.pending_automatic = None;
        self.pending_routes.clear();
        if mode_changed && !self.is_independent() {
            self.routes.clear();
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
        if self.is_independent() {
            let mut seen = HashSet::new();
            return self
                .target_outputs
                .iter()
                .filter_map(|connector| self.routes.get(connector))
                .filter(|route| seen.insert(route.active_playlist.clone()))
                .map(|route| route.active_playlist.clone())
                .collect();
        }
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
        if self.is_independent() {
            return self.status_json_independent(at);
        }
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
                    observed_config_epoch: record.observed_config_epoch,
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
        let taboo_entries_omitted = taboo_total.saturating_sub(taboo_entries.len());
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
                    connected: true,
                    assignment_source: "default",
                    assigned_playlist_id: &assigned.id,
                    assigned_playlist: &assigned.name,
                    playlist_id: &active_playlist.id,
                    playlist: &active_playlist.name,
                    entry_id: &entry.id,
                    kind: entry_kind(entry.kind),
                    still: entry.still.display().to_string(),
                    motion_active: self.driver.motion_active(""),
                    route_source: if self.manual_playlist.is_some() {
                        "manual"
                    } else if selected_rule.is_some() {
                        "schedule"
                    } else {
                        "default"
                    },
                    manual_override: self.manual_playlist.is_some(),
                    schedule_rule_id: selected_rule.map(|rule| rule.id.as_str()),
                    playback_state: playback_state(self.playback_state),
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
                    renderer_failed: self.renderer_failed,
                    last_error: truncate_middle(&self.last_error, MAX_DISPLAY_ERROR_BYTES),
                    automatic_retry: self.pending_automatic.as_ref().map(|pending| {
                        AutomaticRetryStatus {
                            attempt: pending.attempts,
                            maximum_attempts: AUTOMATIC_APPLY_ATTEMPTS,
                            reason: pending.reason,
                        }
                    }),
                });
            }
        } else {
            let mut display_outputs: Vec<&str> =
                self.target_outputs.iter().map(String::as_str).collect();
            display_outputs.extend(
                self.config
                    .displays
                    .iter()
                    .map(|assignment| assignment.connector.as_str()),
            );
            display_outputs.sort();
            display_outputs.dedup();
            for output in display_outputs {
                let explicit = self
                    .config
                    .displays
                    .iter()
                    .find(|display| display.connector == output);
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
                        connected: self.target_outputs.iter().any(|target| target == output),
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
                        motion_active: self.target_outputs.iter().any(|target| target == output)
                            && self.driver.motion_active(output),
                        route_source: if self.manual_playlist.is_some() {
                            "manual"
                        } else if selected_rule.is_some() {
                            "schedule"
                        } else if explicit.is_some() {
                            "assignment"
                        } else {
                            "default"
                        },
                        manual_override: self.manual_playlist.is_some(),
                        schedule_rule_id: selected_rule.map(|rule| rule.id.as_str()),
                        playback_state: playback_state(self.playback_state),
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
                        renderer_failed: self.renderer_failed,
                        last_error: truncate_middle(&self.last_error, MAX_DISPLAY_ERROR_BYTES),
                        automatic_retry: self.pending_automatic.as_ref().map(|pending| {
                            AutomaticRetryStatus {
                                attempt: pending.attempts,
                                maximum_attempts: AUTOMATIC_APPLY_ATTEMPTS,
                                reason: pending.reason,
                            }
                        }),
                    });
                }
            }
        }
        serde_json::to_string(&Status {
            status_version: 2,
            runtime_instance: &self.runtime_instance,
            config_epoch: self.config_epoch,
            config_generation: &self.config.config_generation,
            config_path: self
                .config_path
                .to_str()
                .expect("Runtime::new validated the config path as UTF-8"),
            display_mode: "mirrored",
            theme_source: ThemeSourceStatus {
                configured: &self.config.settings.theme_source_connector,
                effective: None,
                fallback: false,
            },
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
            renderer_failed: self.renderer_failed,
            playback_state: playback_state(self.playback_state),
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
            taboo_entries_omitted,
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

    fn status_json_independent(&self, at: NaiveDateTime) -> Result<String, String> {
        let live_targets = self.current_targets(&self.target_outputs);
        if live_targets.is_empty() {
            return Err("active display playlists are empty".into());
        }
        let mut inventory_outputs = self.target_outputs.clone();
        inventory_outputs.extend(
            self.config
                .displays
                .iter()
                .map(|assignment| assignment.connector.clone()),
        );
        inventory_outputs.sort();
        inventory_outputs.dedup();
        let targets = self.current_targets(&inventory_outputs);
        let summary_playlist = live_targets
            .first()
            .and_then(|first| {
                live_targets
                    .iter()
                    .all(|target| target.playlist_id == first.playlist_id)
                    .then_some(first.playlist_id.as_str())
            })
            .and_then(|playlist| self.config.playlist(playlist));
        let summary_entry = live_targets.first().filter(|first| {
            live_targets.iter().all(|target| {
                target.playlist_id == first.playlist_id && target.entry.id == first.entry.id
            })
        });
        let active_ids: HashSet<&str> = live_targets
            .iter()
            .map(|target| target.playlist_id.as_str())
            .collect();
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
                    observed_config_epoch: record.observed_config_epoch,
                })
            })
            .take(MAX_TABOO_STATUS_ENTRIES)
            .collect();
        let taboo_entries_omitted = taboo_total.saturating_sub(taboo_entries.len());
        let playlists = self
            .config
            .playlists
            .iter()
            .map(|playlist| PlaylistStatus {
                id: &playlist.id,
                name: &playlist.name,
                entries: playlist.entries.len(),
                active: active_ids.contains(playlist.id.as_str()),
            })
            .collect();

        let scheduled: Vec<(String, Option<String>)> = self
            .target_outputs
            .iter()
            .map(|connector| {
                let rule = schedule::resolve_rule_for(
                    &self.config.schedules,
                    Some(connector.as_str()),
                    at,
                )
                .map_err(|error| error.to_string())?;
                let playlist = rule
                    .map(|rule| rule.playlist.clone())
                    .or_else(|| {
                        self.config
                            .displays
                            .iter()
                            .find(|assignment| assignment.connector == *connector)
                            .map(|assignment| assignment.playlist.clone())
                    })
                    .unwrap_or_else(|| self.config.default_playlist.clone());
                Ok((playlist, rule.map(|rule| rule.id.clone())))
            })
            .collect::<Result<_, String>>()?;
        let common_scheduled = scheduled
            .first()
            .filter(|first| scheduled.iter().all(|candidate| candidate.0 == first.0));
        let scheduled_playlist =
            common_scheduled.and_then(|candidate| self.config.playlist(&candidate.0));
        let common_rule_id = scheduled.first().and_then(|first| {
            let rule = first.1.as_deref()?;
            scheduled
                .iter()
                .all(|candidate| candidate.1.as_deref() == Some(rule))
                .then_some(rule)
        });
        let selected_ids: HashSet<&str> = scheduled
            .iter()
            .filter_map(|(_, rule)| rule.as_deref())
            .collect();
        let in_force_ids: HashSet<&str> = self
            .target_outputs
            .iter()
            .zip(scheduled.iter())
            .filter_map(|(connector, (_, rule))| {
                (self.routes.get(connector)?.manual_playlist.is_none())
                    .then_some(rule.as_deref())
                    .flatten()
            })
            .collect();
        let schedules = self
            .config
            .schedules
            .iter()
            .map(|rule| {
                let playlist = self
                    .config
                    .playlist(&rule.playlist)
                    .ok_or_else(|| format!("schedule {} names a missing playlist", rule.id))?;
                Ok(ScheduleRuleStatus {
                    id: &rule.id,
                    connector: (!rule.connector.is_empty()).then_some(rule.connector.as_str()),
                    playlist_id: &playlist.id,
                    playlist: &playlist.name,
                    months: &rule.months,
                    weekdays: &rule.weekdays,
                    start: rule.start.as_deref(),
                    end: rule.end.as_deref(),
                    enabled: rule.enabled,
                    selected: selected_ids.contains(rule.id.as_str()),
                    in_force: in_force_ids.contains(rule.id.as_str()),
                })
            })
            .collect::<Result<Vec<_>, String>>()?;

        let mut displays = Vec::new();
        for target in &targets {
            let route = self
                .routes
                .get(&target.output)
                .ok_or_else(|| format!("display route {:?} is missing", target.output))?;
            let effective = self
                .config
                .playlist(&route.active_playlist)
                .ok_or_else(|| format!("display {} names a missing playlist", target.output))?;
            let explicit = self
                .config
                .displays
                .iter()
                .find(|assignment| assignment.connector == target.output);
            let assigned = self
                .config
                .playlist(
                    explicit.map_or(self.config.default_playlist.as_str(), |assignment| {
                        assignment.playlist.as_str()
                    }),
                )
                .ok_or_else(|| format!("display {} assignment is missing", target.output))?;
            displays.push(DisplayStatus {
                connector: &target.output,
                connected: self.target_outputs.contains(&target.output),
                assignment_source: if explicit.is_some() {
                    "explicit"
                } else {
                    "default"
                },
                assigned_playlist_id: &assigned.id,
                assigned_playlist: &assigned.name,
                playlist_id: &effective.id,
                playlist: &effective.name,
                entry_id: &target.entry.id,
                kind: entry_kind(target.entry.kind),
                still: target.entry.still.display().to_string(),
                motion_active: self.target_outputs.contains(&target.output)
                    && self.driver.motion_active(&target.output),
                route_source: route.source.as_str(),
                manual_override: route.manual_playlist.is_some(),
                schedule_rule_id: route.schedule_rule_id.as_deref(),
                playback_state: playback_state(route.playback_state),
                paused: route.playback_state == PlaybackState::Paused,
                stopped: route.playback_state == PlaybackState::Stopped,
                shuffle: route.shuffle_enabled(self.config.settings.shuffle),
                shuffle_default: self.config.settings.shuffle,
                shuffle_source: if route.shuffle_override.is_some() {
                    "manual"
                } else {
                    "config"
                },
                cycle_enabled: route.cycle_enabled(self.config.settings.cycle_enabled),
                cycle_default: self.config.settings.cycle_enabled,
                cycle_source: if route.cycle_override.is_some() {
                    "manual"
                } else {
                    "config"
                },
                renderer_failed: route.renderer_failed,
                last_error: truncate_middle(&route.last_error, MAX_DISPLAY_ERROR_BYTES),
                automatic_retry: self.pending_routes.get(&target.output).map(|pending| {
                    AutomaticRetryStatus {
                        attempt: pending.attempts,
                        maximum_attempts: AUTOMATIC_APPLY_ATTEMPTS,
                        reason: pending.reason,
                    }
                }),
            });
        }
        let route_states: Vec<&DisplayRoute> = self
            .target_outputs
            .iter()
            .filter_map(|connector| self.routes.get(connector))
            .collect();
        let common_state = route_states.first().and_then(|first| {
            route_states
                .iter()
                .all(|route| route.playback_state == first.playback_state)
                .then_some(first.playback_state)
        });
        let common_shuffle = route_states.first().and_then(|first| {
            let value = first.shuffle_enabled(self.config.settings.shuffle);
            route_states
                .iter()
                .all(|route| route.shuffle_enabled(self.config.settings.shuffle) == value)
                .then_some(value)
        });
        let common_cycle = route_states.first().and_then(|first| {
            let value = first.cycle_enabled(self.config.settings.cycle_enabled);
            route_states
                .iter()
                .all(|route| route.cycle_enabled(self.config.settings.cycle_enabled) == value)
                .then_some(value)
        });
        let effective_theme = self.effective_theme_source();
        let configured_theme = self.config.settings.theme_source_connector.as_str();
        let pending_summary = self
            .target_outputs
            .iter()
            .find_map(|connector| self.pending_routes.get(connector));
        serde_json::to_string(&Status {
            status_version: 2,
            runtime_instance: &self.runtime_instance,
            config_epoch: self.config_epoch,
            config_generation: &self.config.config_generation,
            config_path: self
                .config_path
                .to_str()
                .expect("Runtime::new validated the config path as UTF-8"),
            display_mode: "independent",
            theme_source: ThemeSourceStatus {
                configured: configured_theme,
                effective: effective_theme,
                fallback: effective_theme.is_some_and(|effective| effective != configured_theme),
            },
            playlist_id: summary_playlist.map_or("", |playlist| playlist.id.as_str()),
            playlist: summary_playlist
                .map_or("Multiple displays", |playlist| playlist.name.as_str()),
            source: if route_states
                .iter()
                .all(|route| route.manual_playlist.is_some())
            {
                "manual"
            } else if route_states
                .iter()
                .all(|route| route.manual_playlist.is_none())
            {
                "schedule"
            } else {
                "mixed"
            },
            entry_id: summary_entry.map(|target| target.entry.id.as_str()),
            kind: summary_entry.map(|target| entry_kind(target.entry.kind)),
            still: summary_entry.map(|target| target.entry.still.display().to_string()),
            motion_active: Some(
                self.target_outputs
                    .iter()
                    .any(|output| self.driver.motion_active(output)),
            ),
            renderer_failed: route_states.iter().any(|route| route.renderer_failed),
            playback_state: common_state.map_or("mixed", playback_state),
            paused: common_state == Some(PlaybackState::Paused),
            stopped: common_state == Some(PlaybackState::Stopped),
            shuffle: common_shuffle.unwrap_or(false),
            shuffle_default: self.config.settings.shuffle,
            shuffle_source: if route_states
                .iter()
                .all(|route| route.shuffle_override.is_none())
            {
                "config"
            } else if common_shuffle.is_some() {
                "manual"
            } else {
                "mixed"
            },
            cycle_enabled: common_cycle.unwrap_or(false),
            cycle_default: self.config.settings.cycle_enabled,
            cycle_source: if route_states
                .iter()
                .all(|route| route.cycle_override.is_none())
            {
                "config"
            } else if common_cycle.is_some() {
                "manual"
            } else {
                "mixed"
            },
            last_error: &self.last_error,
            output_discovery_error: &self.output_discovery_error,
            automatic_retry: pending_summary.map(|pending| AutomaticRetryStatus {
                attempt: pending.attempts,
                maximum_attempts: AUTOMATIC_APPLY_ATTEMPTS,
                reason: pending.reason,
            }),
            taboo_entries,
            taboo_entries_omitted,
            playlists,
            schedule: ScheduleStatus {
                following: route_states
                    .iter()
                    .all(|route| route.manual_playlist.is_none()),
                playlist_id: scheduled_playlist.map_or("", |playlist| playlist.id.as_str()),
                playlist: scheduled_playlist
                    .map_or("Multiple displays", |playlist| playlist.name.as_str()),
                rule_id: common_rule_id,
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
            connector: (!rule.connector.is_empty()).then_some(rule.connector.as_str()),
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

fn resolved_media_identity(entry: &Entry) -> Option<ResolvedMediaIdentity> {
    match entry.kind {
        EntryKind::Scene => entry
            .scene_id
            .as_ref()
            .map(|scene_id| ResolvedMediaIdentity::Scene(scene_id.clone())),
        EntryKind::Video => entry
            .motion
            .as_ref()
            .map(|motion| ResolvedMediaIdentity::Video(motion.clone())),
        EntryKind::Still => Some(ResolvedMediaIdentity::Still(entry.still.clone())),
    }
}

fn resolved_identity_for_key(config: &Config, key: &EntryKey) -> Option<ResolvedMediaIdentity> {
    let entry = config.playlist(&key.playlist_id).and_then(|playlist| {
        playlist
            .entries
            .iter()
            .find(|entry| entry.id == key.entry_id)
    })?;
    resolved_media_identity(entry)
}

fn equivalent_resolved_entry(left: &Entry, right: &Entry) -> bool {
    resolved_media_identity(left)
        .zip(resolved_media_identity(right))
        .is_some_and(|(left, right)| left == right)
}

fn targets_equal_ignoring_taboo(left: &[Target], right: &[Target]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter().zip(right).all(|(left, right)| {
        if left.playlist_id != right.playlist_id || left.output != right.output {
            return false;
        }
        let mut left_entry = left.entry.clone();
        let mut right_entry = right.entry.clone();
        left_entry.taboo = None;
        right_entry.taboo = None;
        left_entry == right_entry
    })
}

fn playback_state(state: PlaybackState) -> &'static str {
    match state {
        PlaybackState::Playing => "playing",
        PlaybackState::Paused => "paused",
        PlaybackState::Stopped => "stopped",
    }
}

fn new_runtime_instance() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0x5eed);
    let high = (now >> 64) as u64 ^ u64::from(std::process::id());
    let low = now as u64 ^ high.rotate_left(29);
    format!("{high:016x}{low:016x}")
}

fn push_bounded(values: &mut Vec<usize>, value: usize) {
    if values.len() == PLAYBACK_HISTORY_LIMIT {
        values.remove(0);
    }
    values.push(value);
}

fn promote_taboo_status_key(order: &mut Vec<EntryKey>, key: EntryKey) {
    order.retain(|candidate| candidate != &key);
    if order.len() == MAX_TABOO_STATUS_ENTRIES {
        order.remove(0);
    }
    order.push(key);
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
    use super::{
        promote_taboo_status_key, push_bounded, EntryKey, MAX_TABOO_STATUS_ENTRIES,
        PLAYBACK_HISTORY_LIMIT,
    };

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

    #[test]
    fn revisited_taboo_is_promoted_into_the_newest_status_inventory() {
        let mut order = Vec::new();
        for index in 0..=MAX_TABOO_STATUS_ENTRIES {
            promote_taboo_status_key(
                &mut order,
                EntryKey {
                    playlist_id: "playlist".into(),
                    entry_id: format!("entry-{index}"),
                },
            );
        }
        let total_records = MAX_TABOO_STATUS_ENTRIES + 1;
        assert_eq!(order.len(), MAX_TABOO_STATUS_ENTRIES);
        assert_eq!(total_records - order.len(), 1);
        assert_eq!(order.first().unwrap().entry_id, "entry-1");

        promote_taboo_status_key(
            &mut order,
            EntryKey {
                playlist_id: "playlist".into(),
                entry_id: "entry-0".into(),
            },
        );
        assert_eq!(order.len(), MAX_TABOO_STATUS_ENTRIES);
        assert_eq!(total_records - order.len(), 1);
        assert_eq!(order.first().unwrap().entry_id, "entry-2");
        assert_eq!(order.last().unwrap().entry_id, "entry-0");
    }
}
