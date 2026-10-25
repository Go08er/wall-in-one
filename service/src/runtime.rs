use crate::config::{
    Config, DisplayAssignment, DisplayMode, Entry, EntryKind, MAX_PATH_BYTES,
    NOTHING_TO_PLAY_LABEL, Playlist, SUPPORTED_OVERRIDE_SCHEMAS, SUPPORTED_SCHEMA_VERSIONS,
    ScheduleRule,
};
use crate::outputs::Output;
use crate::power::{PowerObservation, PowerPolicy, PowerSource};
use crate::protocol::{Request, Response, escaped_len, success_excess, success_fits, success_room};
use crate::renderer::{MAX_OUTPUT_NAME_BYTES, MissingRenderer, RendererFailure, WallpaperDriver};
use crate::schedule;
use crate::workshop::Refusal;
use chrono::{DateTime, Local, NaiveDateTime, TimeZone, Utc};
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
/// The longest path `missing_renderers` reports, cut in the middle past it.
/// Two records at this size are a fixed part of the status, inside its
/// structural reserve, whatever program paths the document names (neither
/// status budget counts them). See docs/missing-components.md.
const MAX_MISSING_RENDERER_PATH_BYTES: usize = 1024;
/// The most items status lists in `refused` (what the service skipped in
/// this document generation); `refused_omitted` counts the rest. Like
/// `missing_renderers`, outside the document's status budget: at the reply's
/// limit fewer are listed (`Runtime::status_reply`).
const MAX_REFUSED_STATUS_ENTRIES: usize = 16;
/// The longest reason a `refused` row carries, cut in the middle past it:
/// a reason names the item, and may name a path or an error.
const MAX_REFUSED_REASON_BYTES: usize = 256;
/// Rule checks one status reply may spend finding every display's next
/// schedule boundary (`schedule::next_change`). A profile with a few dozen
/// rules on a few displays needs well under a tenth of it; only hundreds of
/// rules on dozens of displays reach it, and the rest then report no change.
const ROUTE_CHANGE_RULE_CHECKS: usize = 2_000_000;
/// The playlist the app compiles every playable item into
/// (`runtime_config.FALLBACK_PLAYLIST_ID`): after the default, the last
/// stand-in for a display whose own playlist the service refused throughout.
const ALL_MEDIA_PLAYLIST_ID: &str = "all-media";
/// Why a display plays nothing when the service refused every entry it
/// could have played (`Runtime::refused_scenes`), each a web page. Short:
/// status repeats it on every such row, beside no entry (the status budgets
/// count the entry it stands in for, not this).
const REFUSED_WEB_PAGES: &str = "this configuration doesn't allow web wallpapers";
/// The same when not every one of them is a web page: an item the engine
/// wouldn't find, or one the service can't read or doesn't play.
const REFUSED_ITEMS: &str = "no wallpaper here can start";
/// What `next` and `random` answer when every other entry of the playlist
/// was refused: not a fault, so `last_error` doesn't keep it.
const REFUSED_REST: &str = "no other wallpaper here can start";

/// The Wallpaper Engine items the driver would not start in one document
/// generation (`Runtime::refused_scenes`), by the item an entry renders, in
/// the order they were learned: the order status lists them in (`refused`).
#[derive(Default)]
struct Refusals {
    by_item: HashMap<String, Refusal>,
    order: Vec<String>,
}

impl Refusals {
    fn is_empty(&self) -> bool {
        self.order.is_empty()
    }

    fn get(&self, item: &str) -> Option<&Refusal> {
        self.by_item.get(item)
    }

    fn contains(&self, item: &str) -> bool {
        self.by_item.contains_key(item)
    }

    /// Remember `refusal` for `item`; false when it was already known.
    fn insert(&mut self, item: &str, refusal: Refusal) -> bool {
        if self.by_item.contains_key(item) {
            return false;
        }
        self.by_item.insert(item.to_string(), refusal);
        self.order.push(item.to_string());
        true
    }

    fn len(&self) -> usize {
        self.order.len()
    }

    /// Each item and why, in the order learned.
    fn iter(&self) -> impl Iterator<Item = (&str, &Refusal)> {
        self.order
            .iter()
            .filter_map(|item| Some((item.as_str(), self.by_item.get(item)?)))
    }
}

/// Items asked in one walk past refused entries that the driver would start,
/// by item and whether motion was asked for (`dynamics_enabled`): each is
/// asked once a walk (`Runtime::ask_ahead`).
type Cleared = HashSet<(String, bool)>;

/// The entry indexes of a playlist in the order a cursor comes to them from
/// its entry (`move_cursor_forward`, `move_route_forward`): that entry, the
/// entries `previous` unwound (newest first), then the rest of its order
/// after it and, from the start, before it. A shuffled cursor with nothing
/// left after its entry deals the eligible ones again instead; the walk's
/// next round asks along that new order, so it costs a round, not one per
/// entry.
fn visiting_order(cursor: &PlaylistCursor) -> Vec<usize> {
    let count = cursor.order.len();
    let mut seen = vec![false; count];
    let mut found = Vec::with_capacity(count);
    let mut visit = |index: usize| {
        if index < count && !seen[index] {
            seen[index] = true;
            found.push(index);
        }
    };
    if let Some(current) = cursor.order.get(cursor.position) {
        visit(*current);
    }
    for index in cursor.forward.iter().rev() {
        visit(*index);
    }
    for position in (cursor.position.saturating_add(1)..count).chain(0..cursor.position.min(count))
    {
        visit(cursor.order[position]);
    }
    found
}

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
    pub loaded_config_sha256: Option<&'a str>,
    pub supported_config_schemas: &'static [u32],
    /// `runtime-overrides.toml` schemas this service applies. A service that
    /// omits this field never reads that file.
    pub supported_override_schemas: &'static [u32],
    /// Identity of the applied `runtime-overrides.toml`, `None` without one.
    /// Absent, rather than null, from a reply at its size limit
    /// (`ReplyShape::lean`), as `overrides_ignored` is: at least one of the
    /// two is null, which pays for the three schemas 0.4.1 lists beside
    /// 0.4.0's two.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub loaded_overrides_sha256: Option<Option<&'a str>>,
    /// Why a present `runtime-overrides.toml` is not applied, e.g.
    /// `unsupported schema_version 2`; `None` when there is nothing to say.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub overrides_ignored: Option<Option<&'a str>>,
    pub runtime_version: &'static str,
    pub runtime_executable: Option<&'a str>,
    /// What kind of install this service is: `nix`, `system` or `source`
    /// (`crate::INSTALL_KIND`). Added in 0.4.0; a client must not require it.
    pub install_kind: &'static str,
    pub power_source: &'static str,
    pub power_available: bool,
    pub stop_animations_on_battery: bool,
    pub animations_inhibited: bool,
    pub animation_inhibition_reason: &'static str,
    pub status_version: u8,
    pub runtime_instance: &'a str,
    pub config_epoch: u64,
    pub config_generation: &'a str,
    pub config_path: &'a str,
    pub display_mode: &'a str,
    pub theme_source: ThemeSourceStatus<'a>,
    pub playlist_id: &'a str,
    pub playlist: &'a str,
    /// The playlist asked for, when every connected display plays the same
    /// stand-in for it (`playlist_id`): the service refused every entry of
    /// it, so the default playlist or All media plays instead. Absent
    /// otherwise; each display row says it for itself. Added in 0.4.1.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stand_in_for: Option<&'a str>,
    pub source: &'a str,
    pub entry_id: Option<&'a str>,
    /// True when any current connected route resolves to an entry in the
    /// complete taboo set. Unlike `taboo_entries`, this playback-safety bit is
    /// never lost when the diagnostic inventory is capped.
    pub entry_taboo: bool,
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
    /// Effective rotation interval for the summary route. `None` only when
    /// independent routes disagree; their display rows are authoritative.
    pub cycle_interval_seconds: Option<u64>,
    pub last_error: &'a str,
    pub output_discovery_error: &'a str,
    pub automatic_retry: Option<AutomaticRetryStatus<'a>>,
    pub taboo_entries: Vec<TabooStatus<'a>>,
    pub taboo_entries_omitted: usize,
    pub playlists: Vec<PlaylistStatus<'a>>,
    pub schedule: ScheduleStatus<'a>,
    pub schedules: Vec<ScheduleRuleStatus<'a>>,
    pub displays: Vec<DisplayStatus<'a>>,
    /// The compositor's last successful display snapshot, switched-off heads
    /// included (`enabled: false`). Empty before the first one, and left
    /// empty, before any taboo record is left out, when the whole reply
    /// would not fit with it; `output_discovery_error` says when it is
    /// stale. Not part of the configuration's status budget: the
    /// compositor, not the document, decides its size.
    pub outputs: &'a [Output],
    /// The renderer programs the service could not run at all (nothing at
    /// the path, not executable, or unable to run here), mpvpaper first.
    /// Empty when none. Added in 0.4.0. Such a program is never a taboo or a
    /// failure report: the displays that needed it show their still, with
    /// `renderer_failed` and `last_error` saying why. Like `outputs`, never
    /// at a taboo record's expense: at the reply's limit a row's `path` is
    /// left empty, then the rows are left out. See
    /// docs/missing-components.md.
    pub missing_renderers: Vec<MissingRendererStatus>,
    /// Why nothing plays, when every connected display plays nothing (the
    /// document's `nothing_to_play`, schema 8, or the service refused every
    /// entry they could play); null otherwise. Each display row says it for
    /// itself too. Added in 0.4.1. Absent, rather than null, from a reply
    /// at its size limit (`ReplyShape::lean`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nothing_to_play: Option<Option<&'a str>>,
    /// Whether the loaded document allows web wallpapers (`[settings]
    /// allow_web_wallpapers`, schema 8), the one thing that decides whether
    /// the service starts a Wallpaper Engine web page. False for a document
    /// without the key. Status is only ever answered with a document loaded;
    /// after a refused reload it is the one kept. Added in 0.4.1. Absent,
    /// rather than false, from a reply at its size limit
    /// (`ReplyShape::lean`); never while true. The app reads its absence as
    /// a service that doesn't say, and judges the document it holds itself.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub allow_web_wallpapers: Option<bool>,
    /// The Wallpaper Engine items the service would not start in this
    /// document generation, and why, in the order it found them: each entry
    /// of one is skipped (never a taboo, never saved). At most
    /// `MAX_REFUSED_STATUS_ENTRIES`; `refused_omitted` counts the rest. A
    /// reload forgets them and asks again. Added in 0.4.1. Absent, rather
    /// than empty, and the count rather than 0, from a reply at its size
    /// limit (`ReplyShape::lean`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub refused: Option<Vec<RefusedStatus<'a>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub refused_omitted: Option<usize>,
    /// True when this reply left out at least one `stand_in_for` (a row's or
    /// the summary's) to fit its size limit; absent otherwise, never false.
    /// A row without one may then still play a stand-in. Added in 0.4.1.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub stand_in_for_omitted: bool,
    /// True when, to fit at all, this reply left at least one display row's
    /// `schedule_rule_id` null, which the document's status budget doesn't
    /// count; absent otherwise. A row whose `route_source` is `schedule` and
    /// whose `schedule_rule_id` is null may then have had one. Added in 0.4.1.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub schedule_rule_id_omitted: bool,
    /// True when, to fit at all, this reply left at least one `last_error`
    /// (a row's or the summary's) or `output_discovery_error` empty that
    /// wasn't; absent otherwise. An empty one may then not be "no error".
    /// Added in 0.4.1.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub diagnostics_omitted: bool,
}

/// One item the service refused (`Status::refused`).
#[derive(Debug, Serialize)]
pub struct RefusedStatus<'a> {
    /// The Workshop id of the item the entry renders: a preset's base.
    pub scene_id: &'a str,
    /// Whether it is a web page, refused because the document doesn't
    /// allow web wallpapers. Every other is an item the service can't
    /// find, read or play.
    pub web_page: bool,
    /// What the service says, naming the item; at most
    /// `MAX_REFUSED_REASON_BYTES`.
    pub reason: String,
}

#[derive(Debug, Serialize)]
pub struct MissingRendererStatus {
    pub program: &'static str,
    pub path: String,
    /// `not-found`, `not-executable` or `cannot-run`.
    pub reason: &'static str,
}

/// How much of `missing_renderers` a status reply carries.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MissingDetail {
    Full,
    WithoutPaths,
    Left,
}

/// How much a status reply carries of what the document's status budget
/// doesn't bound (`Runtime::status_reply`).
#[derive(Clone, Copy, Debug)]
struct ReplyShape {
    /// Whether the reply leaves out the fields 0.4.1 added while they say
    /// nothing: a row's and the summary's `nothing_to_play` and a row's
    /// `entry_name` while null, `refused` while empty, `refused_omitted`
    /// while 0 and `allow_web_wallpapers` while false; and, of 0.4.0's own,
    /// `loaded_overrides_sha256` and `overrides_ignored` while null, at least
    /// one of which is. Absent, each reads as that value (the app judges the
    /// document itself where `allow_web_wallpapers` isn't said). Every
    /// reply but the whole one is lean (review r13e L6: on 128 rows the
    /// 0.4.1 fields took about 5.8 KB, 30 small taboo records' worth, that
    /// 0.4.0 had room for). A lean reply that carries what 0.4.0's carried
    /// is then at least 21 bytes smaller than 0.4.0's, though its
    /// `supported_config_schemas` lists three more: 0.4.0 loads no document
    /// that allows web wallpapers.
    lean: bool,
    /// The most taboo records listed; `taboo_entries_omitted` counts the
    /// rest.
    taboo: usize,
    /// Whether `outputs` carries the compositor's snapshot.
    outputs: bool,
    missing: MissingDetail,
    /// The most `refused` rows listed; `refused_omitted` counts the rest.
    refused: usize,
    /// Whether the display rows, and the summary, name the playlist a
    /// stand-in plays for (`stand_in_for`).
    stand_in_for: bool,
    /// What a reply that wouldn't fit even without all of the above leaves
    /// out of the rows and the summary.
    rows: RowDetail,
}

/// Which of what the status budget doesn't count on the display rows a
/// reply carries (`Runtime::abridged`): how many rows keep their
/// `schedule_rule_id` and how many their `last_error`, each of the
/// connected rows and of the others (`[connected, others]`), the shortest
/// first; and whether the summary keeps its `output_discovery_error` and
/// its `last_error`. A row id left out is null (`schedule_rule_id_omitted`),
/// an error left out empty (`diagnostics_omitted`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct RowDetail {
    rule_ids: [usize; 2],
    errors: [usize; 2],
    discovery_error: bool,
    last_error: bool,
}

impl RowDetail {
    const WHOLE: Self = Self {
        rule_ids: [usize::MAX; 2],
        errors: [usize::MAX; 2],
        discovery_error: true,
        last_error: true,
    };
    const NONE: Self = Self {
        rule_ids: [0; 2],
        errors: [0; 2],
        discovery_error: false,
        last_error: false,
    };
}

impl ReplyShape {
    const WHOLE: Self = Self {
        lean: false,
        taboo: MAX_TABOO_STATUS_ENTRIES,
        outputs: true,
        missing: MissingDetail::Full,
        refused: MAX_REFUSED_STATUS_ENTRIES,
        stand_in_for: true,
        rows: RowDetail::WHOLE,
    };
}

/// What a reply of `shape` left out of `displays` (`Status`'s `*_omitted`
/// flags), each set only when something was there to leave out.
#[derive(Clone, Copy, Default)]
struct Omitted {
    stand_in_for: bool,
    schedule_rule_id: bool,
    diagnostics: bool,
}

/// What a reply measured as it was built (`Runtime::status_json_measured`):
/// its rows' units, and how many bytes on the wire the lean reply of the
/// same shape takes less.
#[derive(Debug, Default)]
struct Measured {
    units: RowUnits,
    lean_saving: usize,
}

/// The bytes on the wire each of `RowDetail`'s row units adds to a reply
/// that leaves it out (an id beside `null`, an error beside `""`), in the
/// order a reply keeps them: of the connected rows and of the others, the
/// shortest first.
#[derive(Debug, Default)]
struct RowUnits {
    rule_ids: [Vec<usize>; 2],
    errors: [Vec<usize>; 2],
}

/// What `value`'s compact JSON takes on the wire, each of its characters
/// escaped once more inside the reply's message (`escaped_len`).
fn wire_len<T: Serialize + ?Sized>(value: &T) -> usize {
    serde_json::to_string(value).map_or(0, |json| escaped_len(&json))
}

/// What a unit of `RowDetail` adds on the wire beside what is left in its
/// place, `null` or `""`, which take 4 bytes each (`"` is escaped).
fn unit_len<T: Serialize + ?Sized>(value: &T) -> usize {
    wire_len(value).saturating_sub(4)
}

/// What a lean reply saves on the wire by leaving out `"key":value` (and
/// its comma: no such field is the first of its object).
fn absent_len(key: &str, value: &str) -> usize {
    escaped_len(&format!(",\"{key}\":{value}"))
}

/// What a flag `"key":true` takes on the wire.
fn flag_len(key: &str) -> usize {
    absent_len(key, "true")
}

/// `value` as a reply of `shape` carries a field 0.4.1 added: left out
/// (None) from a lean one while it `says_nothing`.
fn lean_field<T>(shape: ReplyShape, value: T, says_nothing: bool) -> Option<T> {
    (!(shape.lean && says_nothing)).then_some(value)
}

/// The summary's fields a lean reply leaves out while they say nothing
/// (`ReplyShape::lean`), as a reply of `shape` carries them, and what
/// leaving those out saves on the wire.
struct LeanSummary<'a> {
    nothing_to_play: Option<Option<&'a str>>,
    allow_web_wallpapers: Option<bool>,
    refused: Option<Vec<RefusedStatus<'a>>>,
    refused_omitted: Option<usize>,
    loaded_overrides_sha256: Option<Option<&'a str>>,
    overrides_ignored: Option<Option<&'a str>>,
    lean_saving: usize,
}

impl<'a> LeanSummary<'a> {
    fn of(
        shape: ReplyShape,
        config: &'a Config,
        nothing: Option<&'a str>,
        refused: Vec<RefusedStatus<'a>>,
        refused_omitted: usize,
    ) -> Self {
        let allow_web = config.settings.allow_web_wallpapers;
        let overrides = config.overrides_sha256.as_deref();
        let ignored = config.overrides_ignored.as_deref();
        let says_nothing = [
            (nothing.is_none(), "nothing_to_play", "null"),
            (!allow_web, "allow_web_wallpapers", "false"),
            (refused.is_empty(), "refused", "[]"),
            (refused_omitted == 0, "refused_omitted", "0"),
            (overrides.is_none(), "loaded_overrides_sha256", "null"),
            (ignored.is_none(), "overrides_ignored", "null"),
        ];
        let lean_saving = says_nothing
            .iter()
            .filter(|(quiet, _, _)| *quiet)
            .map(|(_, key, value)| absent_len(key, value))
            .sum();
        let [
            nothing_quiet,
            web_quiet,
            refused_quiet,
            omitted_quiet,
            loaded_quiet,
            ignored_quiet,
        ] = says_nothing.map(|(quiet, _, _)| quiet);
        Self {
            nothing_to_play: lean_field(shape, nothing, nothing_quiet),
            allow_web_wallpapers: lean_field(shape, allow_web, web_quiet),
            refused: lean_field(shape, refused, refused_quiet),
            refused_omitted: lean_field(shape, refused_omitted, omitted_quiet),
            loaded_overrides_sha256: lean_field(shape, overrides, loaded_quiet),
            overrides_ignored: lean_field(shape, ignored, ignored_quiet),
            lean_saving,
        }
    }
}

/// Leave out of `displays` what `shape` doesn't carry, and say what was
/// left out: of the rows, and of the summary's `output_discovery_error` and
/// `last_error` (`summary`), which its caller leaves out as `shape.rows`
/// says. What the rows' units take is measured before any is left out.
fn abridge_rows(
    displays: &mut [DisplayStatus<'_>],
    shape: ReplyShape,
    summary: [&str; 2],
) -> (Omitted, Measured) {
    let mut omitted = Omitted::default();
    let mut measured = Measured::default();
    if !shape.stand_in_for {
        omitted.stand_in_for = displays.iter().any(|row| row.stand_in_for.is_some());
        for row in displays.iter_mut() {
            row.stand_in_for = None;
        }
    }
    // Each group's units, the shortest first, ties in row order.
    let mut rule_ids: [Vec<(usize, usize)>; 2] = Default::default();
    let mut errors: [Vec<(usize, usize)>; 2] = Default::default();
    for (index, row) in displays.iter().enumerate() {
        let group = usize::from(!row.connected);
        if let Some(rule) = row.schedule_rule_id {
            rule_ids[group].push((unit_len(rule), index));
        }
        if !row.last_error.is_empty() {
            errors[group].push((unit_len(&row.last_error), index));
        }
    }
    for group in 0..2 {
        rule_ids[group].sort_unstable();
        errors[group].sort_unstable();
        for &(_, index) in rule_ids[group].iter().skip(shape.rows.rule_ids[group]) {
            displays[index].schedule_rule_id = None;
            omitted.schedule_rule_id = true;
        }
        for &(_, index) in errors[group].iter().skip(shape.rows.errors[group]) {
            displays[index].last_error.clear();
            omitted.diagnostics = true;
        }
        measured.units.rule_ids[group] = rule_ids[group].iter().map(|unit| unit.0).collect();
        measured.units.errors[group] = errors[group].iter().map(|unit| unit.0).collect();
    }
    let [discovery_error, last_error] = summary;
    omitted.diagnostics |= (!shape.rows.discovery_error && !discovery_error.is_empty())
        || (!shape.rows.last_error && !last_error.is_empty());
    for row in displays.iter_mut() {
        if row.nothing_to_play == Some(None) {
            measured.lean_saving += absent_len("nothing_to_play", "null");
            row.nothing_to_play = lean_field(shape, None, true);
        }
        if row.entry_name == Some(None) {
            measured.lean_saving += absent_len("entry_name", "null");
            row.entry_name = lean_field(shape, None, true);
        }
    }
    (omitted, measured)
}

/// How many characters `count` takes written in decimal.
fn decimal_digits(count: usize) -> usize {
    count
        .checked_ilog10()
        .map_or(1, |digits| digits as usize + 1)
}

impl MissingRendererStatus {
    fn of(record: &MissingRenderer, detail: MissingDetail) -> Self {
        Self {
            program: record.program,
            path: if detail == MissingDetail::Full {
                truncate_middle(&record.path, MAX_MISSING_RENDERER_PATH_BYTES)
            } else {
                String::new()
            },
            reason: record.reason.as_str(),
        }
    }
}

/// What a display that shows its still for a missing renderer says.
fn missing_renderer_diagnostic(record: &MissingRenderer) -> String {
    truncate_middle(&record.diagnostic(), MAX_LAST_ERROR_BYTES)
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
    /// The playlist this display asked for (its pick, rule, assignment or
    /// the default) when `playlist_id` is a stand-in for it: the service
    /// refused every entry of it. Absent while it plays its own playlist,
    /// or nothing (`nothing_to_play` then says why). Added in 0.4.1.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stand_in_for: Option<&'a str>,
    pub entry_id: &'a str,
    /// Exact current-entry safety truth for this route. This is deliberately
    /// independent of renderer failure: an equivalent occurrence can become
    /// taboo because a renderer on another output crashed.
    pub entry_taboo: bool,
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
    /// Effective rotation interval for this route's playlist: its own
    /// override, otherwise the global setting.
    pub cycle_interval_seconds: u64,
    /// This display's opt-in from `runtime-overrides.toml`: its own
    /// (explicitly assigned) playlist beats global, untargeted schedule rules,
    /// while rules aimed at this connector still win. False in mirrored mode,
    /// where the flag is dormant. When the assignment wins, `route_source`
    /// stays `assignment` as before.
    pub beats_global_rules: bool,
    pub renderer_failed: bool,
    pub last_error: String,
    pub automatic_retry: Option<AutomaticRetryStatus<'a>>,
    /// Why this display plays nothing: its playlist has no entries (schema
    /// 8). `entry_id`, `kind` and `still` are then empty, nothing of the
    /// service's is on it, and it has no rotation countdown. Null while
    /// something plays; absent instead from a reply at its size limit
    /// (`ReplyShape::lean`). Added in 0.4.1.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nothing_to_play: Option<Option<&'a str>>,
    /// The name of what is on screen, for a display that plays nothing:
    /// "Nothing to play: <reason>", which the companion shows as the
    /// display's title. Null otherwise (the companion then names a wallpaper
    /// after its still); absent instead from a reply at its size limit, like
    /// `nothing_to_play`. Added in 0.4.1.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub entry_name: Option<Option<String>>,
    /// When and why this display's wallpaper next changes on its own.
    #[serde(flatten)]
    pub timing: DisplayTiming,
}

/// The summary's `stand_in_for`: the playlist every connected row asked for,
/// when each plays the same stand-in for it, the summary's playlist.
fn summary_stand_in<'a>(
    displays: &[DisplayStatus<'a>],
    summary: Option<&Playlist>,
) -> Option<&'a str> {
    let summary = summary?;
    let mut connected = displays.iter().filter(|row| row.connected);
    let first = connected.next()?;
    let asked = first.stand_in_for?;
    (first.playlist_id == summary.id
        && connected.all(|row| row.stand_in_for == Some(asked) && row.playlist_id == summary.id))
    .then_some(asked)
}

/// `entry_name` for a display that plays nothing for `reason`.
fn nothing_name(reason: Option<&str>) -> Option<String> {
    reason.map(|reason| format!("{NOTHING_TO_PLAY_LABEL}{reason}"))
}

/// When a display's automatic route and rotation next move. Appended to each
/// display row; every field is `null` when there is nothing to report.
#[derive(Debug, Default, Serialize)]
pub struct DisplayTiming {
    /// Local wall-clock time (`YYYY-MM-DDTHH:MM:SS`) the clock will show at
    /// the next schedule change of this display's automatic playlist or route
    /// source, at most `schedule::CHANGE_HORIZON_DAYS` ahead, including one
    /// caused by the clock itself jumping. `null` while a manual pick holds,
    /// for a detached display, or with no change in range. The seconds are
    /// real elapsed time.
    pub route_change_at: Option<String>,
    pub route_change_in_s: Option<u64>,
    /// Local wall-clock time the rotation next advances: the elapsed
    /// `next_cycle_in_s` (the authority) projected through UTC into the local
    /// zone, so a daylight-saving change in between is honored. `null` when
    /// the route is paused, does not cycle, or is detached. Due now reads as 0
    /// seconds.
    pub next_cycle_at: Option<String>,
    pub next_cycle_in_s: Option<u64>,
    /// The companion's names: `route_change_at` as local `HH:MM`, only when it
    /// is less than a day away, and `next_cycle_in_s`.
    pub until: Option<String>,
    pub next_change_in_s: Option<u64>,
}

const WALL_CLOCK_FORMAT: &str = "%Y-%m-%dT%H:%M:%S";
const SECONDS_PER_DAY: u64 = 24 * 60 * 60;

impl DisplayTiming {
    /// The same, without a rotation countdown: a display that plays nothing
    /// has nothing to rotate to. A schedule change still is one.
    fn without_rotation(mut self) -> Self {
        self.next_cycle_at = None;
        self.next_cycle_in_s = None;
        self.next_change_in_s = None;
        self
    }

    /// `reference` is the real instant the reply describes, from which an
    /// elapsed rotation countdown is projected into `zone`.
    fn new<Tz: TimeZone>(
        route_change: Option<schedule::Change>,
        cycle_in: Option<Duration>,
        reference: DateTime<Utc>,
        zone: &Tz,
    ) -> Self {
        let route_change_in_s = route_change.map(|change| whole_seconds_up(change.after));
        let next_cycle_in_s =
            cycle_in.map(|remaining| remaining.as_secs() + u64::from(remaining.subsec_nanos() > 0));
        let next_cycle_at = next_cycle_in_s
            .and_then(|seconds| wall_clock_after(reference, seconds, zone))
            .map(|due| due.format(WALL_CLOCK_FORMAT).to_string());
        Self {
            route_change_at: route_change
                .map(|change| change.local.format(WALL_CLOCK_FORMAT).to_string()),
            route_change_in_s,
            next_cycle_at,
            next_cycle_in_s,
            until: route_change
                .zip(route_change_in_s)
                .filter(|(_, seconds)| *seconds < SECONDS_PER_DAY)
                .map(|(change, _)| change.local.format("%H:%M").to_string()),
            next_change_in_s: next_cycle_in_s,
        }
    }
}

/// The local time `zone`'s clock shows `seconds` of elapsed time after
/// `reference`. The addition happens in UTC, so across a daylight-saving
/// change the answer is the clock's real reading, never a skipped or doubled
/// local time.
pub fn wall_clock_after<Tz: TimeZone>(
    reference: DateTime<Utc>,
    seconds: u64,
    zone: &Tz,
) -> Option<NaiveDateTime> {
    let elapsed = chrono::TimeDelta::try_seconds(i64::try_from(seconds).ok()?)?;
    let due = reference.checked_add_signed(elapsed)?;
    Some(due.with_timezone(zone).naive_local())
}

/// `delta` in whole seconds, rounded up; 0 once it has passed.
fn whole_seconds_up(delta: chrono::TimeDelta) -> u64 {
    let seconds = delta.num_seconds();
    let rounded = if delta.subsec_nanos() > 0 {
        seconds + 1
    } else {
        seconds
    };
    u64::try_from(rounded).unwrap_or(0)
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
    schedule_rule: Option<String>,
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
    power_released_outputs: HashSet<String>,
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
    power_released_outputs: HashSet<String>,
    manual_playlist: Option<String>,
    active_playlist: String,
    schedule_rule: Option<String>,
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
    refused_scenes: Refusals,
    routes: HashMap<String, DisplayRoute>,
    route_generation: u64,
    current_time: NaiveDateTime,
    config_epoch: u64,
}

pub struct Runtime<D: WallpaperDriver> {
    config_path: PathBuf,
    config: Config,
    driver: D,
    power: PowerPolicy,
    applied_power_inhibition: bool,
    // Paused routes released by battery policy remain static on AC until Play.
    power_released_outputs: HashSet<String>,
    manual_playlist: Option<String>,
    active_playlist: String,
    /// The schedule rule whose playlist mirrored mode plays over the default
    /// and the assignments, as of the decision `active_playlist` is (a tick,
    /// a reload, Follow schedule); None while none does, or for a pick.
    /// Status names it, as an independent route's own `schedule_rule_id`.
    schedule_rule: Option<String>,
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
    /// bounded fallback across a transient discovery failure.
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
    /// The Wallpaper Engine items the driver would not start in this
    /// document generation, by the item an entry renders
    /// (`Entry::rendered_scene`), with why: while web wallpapers aren't
    /// allowed, a web page, or an item the service can't find or read
    /// (`WallpaperDriver::motion_refusal`). Every entry of one is skipped
    /// (owner: "skip to next, if it's in a playlist alone, then use the
    /// default playlist in schedule"), so each is asked once. Never a taboo
    /// and never saved; a reload forgets them. Status lists them
    /// (`refused`), which is how the person learns of them.
    refused_scenes: Refusals,
    authoritative_generation: u64,
    routes: HashMap<String, DisplayRoute>,
    route_generation: u64,
    current_time: NaiveDateTime,
    /// The latest monotonic instant `tick` was handed. Status measures cycle
    /// deadlines from it or the real clock, whichever is later, so a caller
    /// driving time by hand gets the same answers as the service loop.
    observed_instant: Instant,
    runtime_instance: String,
    runtime_executable: Option<String>,
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
            schedule::resolve_rule(&config.schedules, at).map_err(|error| error.to_string())?;
        let schedule_rule = scheduled.map(|rule| rule.id.clone());
        let active = scheduled
            .map_or(&config.default_playlist, |rule| &rule.playlist)
            .to_string();
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
            power: PowerPolicy::default(),
            applied_power_inhibition: false,
            power_released_outputs: HashSet::new(),
            manual_playlist: None,
            active_playlist: active,
            schedule_rule,
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
            refused_scenes: Refusals::default(),
            authoritative_generation: 0,
            routes: HashMap::new(),
            route_generation: 0,
            current_time: at,
            observed_instant: Instant::now(),
            runtime_instance: new_runtime_instance(),
            runtime_executable: std::env::current_exe()
                .ok()
                .and_then(|path| path.to_str().map(str::to_owned)),
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

    pub fn battery_policy_enabled(&self) -> bool {
        self.config.settings.stop_animations_on_battery
    }

    fn animations_inhibited(&self) -> bool {
        self.power.inhibited(self.battery_policy_enabled())
    }

    fn effective_motion_settings(
        &mut self,
        output: &str,
        playback: PlaybackState,
    ) -> crate::config::Settings {
        let inhibited = self.animations_inhibited();
        self.driver.set_animation_inhibited(inhibited);
        if inhibited {
            self.power_released_outputs.insert(output.to_string());
        } else if playback == PlaybackState::Playing {
            self.power_released_outputs.remove(output);
        }
        let held_pause = playback == PlaybackState::Paused
            && (self.power_released_outputs.contains(output)
                || (!self.is_independent() && self.power_released_outputs.contains("")));
        let mut settings = self.config.settings.clone();
        settings.dynamics_enabled &=
            !inhibited && playback != PlaybackState::Stopped && !held_pause;
        settings
    }

    /// Consume the observer's coalesced snapshot on the runtime thread. Power
    /// transitions never change manual transport, selection, clocks or colors.
    pub fn observe_power(&mut self, observation: PowerObservation) {
        self.power.observation = observation;
        let inhibited = self.animations_inhibited();
        self.driver.set_animation_inhibited(inhibited);
        let mirrored = !self.is_independent();
        self.power_released_outputs.retain(|output| {
            (mirrored && output.is_empty())
                || self.target_outputs.contains(output)
                || self.routes.contains_key(output)
        });
        if inhibited == self.applied_power_inhibition {
            return;
        }
        self.applied_power_inhibition = inhibited;
        if inhibited {
            self.power_released_outputs
                .extend(self.target_outputs.iter().cloned());
            self.driver.stop();
            return;
        }
        let targets = self.current_targets(&self.target_outputs);
        let mut errors = Vec::new();
        let mut missing_notes = Vec::new();
        let mut resumed = false;
        // The displays whose entry the driver won't start: each moves on as
        // an apply would (`skip_refused_mirrored`), once the loop is done.
        let mut refused_outputs = HashSet::new();
        self.driver.begin_apply();
        for target in targets {
            let playback = if self.is_independent() {
                self.routes[&target.output].playback_state
            } else {
                self.playback_state
            };
            if playback != PlaybackState::Playing
                || !self.config.settings.dynamics_enabled
                || target.entry.kind == EntryKind::Still
                || self.is_taboo(&target.playlist_id, &target.entry.id)
                || self.driver.motion_active(&target.output)
            {
                continue;
            }
            if let Some(refusal) = self
                .driver
                .motion_refusal(&target.entry, &self.config.settings)
            {
                self.remember_refusal(&target.playlist_id, &target.entry, refusal);
                refused_outputs.insert(target.output.clone());
                continue;
            }
            let result =
                self.driver
                    .start_motion_only(&target.entry, &target.output, &self.config.settings);
            if result.is_ok()
                && let Some(refusal) = self.driver.motion_refused(&target.output)
            {
                self.remember_refusal(&target.playlist_id, &target.entry, refusal);
                refused_outputs.insert(target.output.clone());
                continue;
            }
            let missing = result
                .is_ok()
                .then(|| {
                    self.driver
                        .motion_missing(&target.output)
                        .map(|missing| missing_renderer_diagnostic(&missing))
                })
                .flatten();
            if let Some(diagnostic) = missing {
                // Still up, as before the restriction; say why it stays.
                self.power_released_outputs.remove(&target.output);
                if let Some(route) = self.routes.get_mut(&target.output) {
                    route.renderer_failed = true;
                    route.last_error.clone_from(&diagnostic);
                }
                missing_notes.push(if target.output.is_empty() {
                    diagnostic
                } else {
                    format!("{}: {diagnostic}", target.output)
                });
            } else if let Err(error) = result {
                let message =
                    format!("could not resume animation after battery restriction: {error}");
                if let Some(route) = self.routes.get_mut(&target.output) {
                    route.renderer_failed = true;
                    route.last_error = truncate_middle(&message, MAX_LAST_ERROR_BYTES);
                }
                errors.push(message);
            } else {
                resumed = true;
                self.power_released_outputs.remove(&target.output);
                if let Some(route) = self.routes.get_mut(&target.output) {
                    route.renderer_failed = false;
                    route.last_error.clear();
                }
            }
        }
        self.driver.end_apply();
        if !errors.is_empty() {
            self.last_error = bounded_failure_summary(&errors);
            self.renderer_failed = true;
        } else if !missing_notes.is_empty() && !self.is_independent() {
            self.last_error = bounded_failure_summary(&missing_notes);
            self.renderer_failed = true;
        } else if resumed && !self.is_independent() {
            self.renderer_failed = false;
            self.last_error.clear();
        }
        if self.is_independent() {
            self.refresh_independent_last_error();
        }
        if !refused_outputs.is_empty() {
            // The apply says what became of them, as any apply does. A
            // mirrored display whose motion just started keeps it, as an
            // independent one does; each refused one is applied.
            let _ = if self.is_independent() {
                self.apply_independent_selected(&refused_outputs)
            } else {
                let resident: Vec<Target> = self
                    .resident_targets()
                    .into_iter()
                    .filter(|target| !refused_outputs.contains(&target.output))
                    .collect();
                self.apply_current_keeping(&resident)
            };
        }
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
        self.power_released_outputs = snapshot.power_released_outputs;
        self.manual_playlist = snapshot.manual_playlist;
        self.active_playlist = snapshot.active_playlist;
        self.schedule_rule = snapshot.schedule_rule;
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
        self.refused_scenes = snapshot.refused_scenes;
        self.routes = snapshot.routes;
        self.route_generation = snapshot.route_generation;
        self.current_time = snapshot.current_time;
        self.config_epoch = snapshot.config_epoch;
    }

    fn selection_state(&self) -> SelectionState {
        SelectionState {
            active_playlist: self.active_playlist.clone(),
            schedule_rule: self.schedule_rule.clone(),
            cursors: self.cursors.clone(),
            rng: self.rng,
        }
    }

    fn restore_selection(&mut self, state: &SelectionState) {
        self.active_playlist.clone_from(&state.active_playlist);
        self.schedule_rule.clone_from(&state.schedule_rule);
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
        let observation = self.power.observation;
        self.handle_with_power(request, at, |_| observation)
    }

    pub fn handle_with_power(
        &mut self,
        request: Request,
        at: NaiveDateTime,
        mut prepare_power: impl FnMut(bool) -> PowerObservation,
    ) -> Response {
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
                return match self.status_reply(at) {
                    Ok(status) => Response::success(status),
                    Err(error) => Response::failure(error),
                };
            }
            "reload" => no_argument("reload").and_then(|()| self.reload(at, &mut prepare_power)),
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
                .and_then(|reference| self.config.playlist(reference))
                .is_some_and(|playlist| self.playlist_selectable(&playlist.id)),
            "shuffle" => Self::runtime_boolean_override(
                request.argument.as_deref(),
                "shuffle on|off|default",
            )
            .is_ok(),
            "cycle" => {
                Self::runtime_boolean_override(request.argument.as_deref(), "cycle on|off|default")
                    .is_ok()
            }
            "schedule-follow" => {
                request.argument.is_none()
                    && self
                        .scheduled_selection(self.current_time)
                        .ok()
                        .is_some_and(|(_, _, playlists)| {
                            !playlists.is_empty()
                                && playlists
                                    .iter()
                                    .all(|playlist| self.playlist_selectable(playlist))
                        })
            }
            "play" => {
                request.argument.is_none()
                    && self
                        .current_targets(&self.target_outputs)
                        .iter()
                        .all(|target| !self.is_taboo(&target.playlist_id, &target.entry.id))
            }
            "pause" | "toggle" | "stop" | "next" | "previous" | "random" | "quit" => {
                request.argument.is_none()
            }
            "status" | "reload" | "on" => false,
            _ => false,
        }
    }

    pub fn tick(&mut self, at: NaiveDateTime, now: Instant) {
        self.current_time = at;
        self.observed_instant = now;
        let mut failures = self.driver.poll_failures();
        if let Some(palette_failure) = self.reapply_palette_after_renderer_failure(&failures) {
            failures.push(palette_failure);
        }
        if self.is_independent() {
            let mut unattributed = Vec::new();
            for failure in &failures {
                if failure.permanent_for_session
                    && let Some(key) = self.failure_key(&failure.entry_id, &failure.output)
                {
                    self.mark_taboo(key, &failure.message, "renderer-crash");
                }
                if let Some(route) = self.routes.get_mut(&failure.output) {
                    // A renderer exit is a failed attempt to satisfy the
                    // route's desired state, not a transport command. The
                    // user's intent (Playing, Paused or Stopped) is left as
                    // it was: renderer_failed/motion_active describe what is
                    // actually resident, and Play retries a failed renderer
                    // from Playing, Paused or Stopped alike. In particular a
                    // paused route must keep holding its cursor, so the
                    // cycle guards below still skip it.
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
                if failure.permanent_for_session
                    && let Some(key) = self.failure_key(&failure.entry_id, &failure.output)
                {
                    self.mark_taboo(key, &failure.message, "renderer-crash");
                }
            }
            // Videos remain retryable: unlike scenes, SystemDriver does not
            // suppress a video after it exits.  Remember that Play has real
            // work to do instead of treating the still fallback as healthy
            // playback.  The transport state (Playing, Paused or Stopped) is
            // the user's intent and is not touched here: a paused playlist
            // keeps its cursor, and Play retries the failed renderer.  Scene
            // retries still reach SystemDriver's session suppression and fail
            // with the attributable scene diagnostic.
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
        if !had_pending
            && self.pending_automatic.is_none()
            && self.manual_playlist.is_none()
            && let Ok(scheduled) = schedule::resolve_rule(&self.config.schedules, at)
        {
            let overrode = scheduled.is_some();
            let rule = scheduled.map(|rule| rule.id.clone());
            let wanted = scheduled
                .map_or(&self.config.default_playlist, |rule| &rule.playlist)
                .to_string();
            let playlist_changed = wanted != self.active_playlist;
            // Which rule decides is part of the decision: a rule handing over
            // to another of the same playlist changes what status names.
            let routing_changed = rule != self.schedule_rule;
            // With assignments, a rule coming into force moves every
            // assigned display onto its playlist, and one going out of force
            // moves them back, even when the playlist in force is the
            // default either way (review r13e N9).
            let assignments_move = routing_changed
                && !self.config.displays.is_empty()
                && overrode != self.schedule_rule.is_some();
            if (playlist_changed || assignments_move && overrode)
                && (overrode || self.config.displays.is_empty())
            {
                // Known before the change is decided, not learned by
                // applying it: a stand-in that already plays isn't started
                // again.
                self.ask_ahead_of_mirrored_change(&wanted);
            }
            // A playlist the service refused throughout plays its stand-in
            // (`stand_in`): a change to one whose stand-in is what already
            // plays changes nothing on screen.
            let shown_changed = (playlist_changed
                && self.shown_playlist_id(&wanted)
                    != self.shown_playlist_id(&self.active_playlist))
                || (assignments_move
                    && self.mirrored_shown(overrode, &wanted)
                        != self.mirrored_shown(!overrode, &self.active_playlist));
            if (routing_changed || playlist_changed) && !shown_changed {
                // Only provenance changed (for example, a schedule rule
                // naming the already-active default). Commit the truth
                // shown in status without restarting motion.
                self.schedule_rule = rule;
                self.active_playlist = wanted;
            } else if shown_changed {
                schedule_transition_attempted = true;
                let baseline = self.selection_state();
                self.schedule_rule = rule;
                let mut candidate_available = true;
                if playlist_changed {
                    self.active_playlist = wanted.clone();
                    candidate_available =
                        self.start_cursor_for(&wanted) || self.nothing_reason(&wanted).is_some();
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
        if !schedule_transition_attempted
            && !had_pending
            && self.pending_automatic.is_none()
            && self.playback_state != PlaybackState::Paused
            && self.cycle_enabled()
            && now.duration_since(self.last_cycle)
                >= Duration::from_secs(self.mirrored_cycle_interval_seconds())
        {
            self.start_automatic_move(now);
        }
        if now.saturating_duration_since(self.last_output_probe) >= OUTPUT_PROBE_INTERVAL {
            self.last_output_probe = now;
            self.driver.begin_apply();
            match self.probe_outputs() {
                Ok(outputs) if self.live_outputs.as_ref() != Some(&outputs) => {
                    // The discovery call populated the driver's batch snapshot;
                    // reuse it for this hand-over instead of asking the
                    // compositor twice. A display that stays keeps its
                    // renderer: only one plugged in starts.
                    let resident = self.resident_targets();
                    let _ = self.apply_current_in_batch(Ok(outputs), &resident);
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
            moved |= self.move_cursor_forward(&playlist);
        }
        if !moved {
            self.restore_selection(&baseline);
            self.last_cycle = now;
            // Nothing to play is not a fault: there is just nothing to move
            // to. Nor is a playlist whose other entries were refused.
            if self.mirrored_nothing_reason().is_none()
                && !self
                    .effective_playlist_ids()
                    .iter()
                    .any(|playlist| self.holds_refused(playlist))
            {
                self.last_error = "every entry in the active playlist is taboo this session".into();
            }
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
            self.driver.begin_apply();
            match self.probe_outputs() {
                Ok(outputs) if self.live_outputs.as_ref() != Some(&outputs) => {
                    self.reconcile_hotplug_in_batch(outputs, now);
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
            if let Some(route) = self.routes.get(connector)
                && route.manual_playlist.is_none()
                && let Ok((wanted, _, _)) = self.automatic_route(connector, at)
                && self
                    .config
                    .playlist(&wanted)
                    .is_some_and(|playlist| playlist.id != route.active_playlist)
            {
                // As in mirrored mode: a rule's playlist the service refuses
                // throughout is known to be before the route changes, so a
                // stand-in that already plays isn't started again.
                self.ask_ahead_of_route_change(connector, &wanted);
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
                let usable = self.playlist_selectable(&wanted);
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
                    >= Duration::from_secs(
                        self.playlist_cycle_interval_seconds(&route.active_playlist),
                    )
            {
                if self.move_route_forward(connector) {
                    let candidate = self.routes[connector].clone();
                    self.routes.insert(connector.clone(), before.clone());
                    self.start_route_automatic(connector.clone(), before, candidate, "cycle", now);
                } else if self.route_nothing_reason(connector).is_some()
                    || self.holds_refused(&self.routes[connector].active_playlist)
                {
                    // Nothing to move to on a route that plays nothing, or
                    // whose other entries were refused.
                    self.routes
                        .get_mut(connector)
                        .expect("route exists")
                        .last_cycle = now;
                } else {
                    let route = self.routes.get_mut(connector).expect("route exists");
                    route.last_cycle = now;
                    route.last_error = "every entry in this playlist is taboo this session".into();
                    self.refresh_independent_last_error();
                }
            }
        }
    }

    /// Reconcile a compositor hotplug using the discovery snapshot already
    /// opened by `tick_independent`. Existing outputs keep their renderer
    /// processes and route state. Only newly connected outputs are applied,
    /// and their first failure enters the same bounded three-attempt machine as
    /// other automatic transitions.
    fn reconcile_hotplug_in_batch(&mut self, mut outputs: Vec<String>, now: Instant) {
        outputs.sort();
        outputs.dedup();
        let previous: HashSet<String> = self.target_outputs.iter().cloned().collect();
        let previous_theme = self.effective_theme_source().map(str::to_owned);

        self.live_outputs = Some(outputs.clone());
        self.output_discovery_error.clear();
        self.target_outputs = outputs;
        let connected = self.target_outputs.clone();
        if let Err(error) = self.reconcile_independent_routes(&connected) {
            self.output_discovery_error = truncate_middle(&error, MAX_OUTPUT_DISCOVERY_ERROR_BYTES);
            return;
        }

        let newly_connected: Vec<String> = connected
            .iter()
            .filter(|connector| !previous.contains(*connector))
            .cloned()
            .collect();
        for connector in &newly_connected {
            if let Err(error) = self.refresh_route_decision(connector, true)
                && let Some(route) = self.routes.get_mut(connector)
            {
                route.last_error = truncate_middle(&error, MAX_LAST_ERROR_BYTES);
                route.renderer_failed = true;
            }
        }

        let selected: HashSet<String> = newly_connected.iter().cloned().collect();
        if selected.is_empty() {
            // retain_outputs is what tears down renderers belonging to a
            // disconnected output. It does not disturb survivors.
            self.driver.retain_outputs(&connected);
        } else {
            let result = self.apply_independent_in_batch(&selected);
            self.remember_hotplug_attempt(&selected, result.as_ref().err(), now);
        }

        let current_theme = self.effective_theme_source().map(str::to_owned);
        if previous_theme != current_theme
            && current_theme
                .as_ref()
                .is_some_and(|connector| !selected.contains(connector))
        {
            let palette_result = current_theme
                .as_deref()
                .and_then(|connector| {
                    self.current_targets(&self.target_outputs)
                        .into_iter()
                        .find(|target| target.output == connector)
                })
                .map(|target| (target.output, self.driver.apply_palette_only(&target.entry)));
            if let Some((connector, Err(error))) = palette_result
                && let Some(route) = self.routes.get_mut(&connector)
            {
                route.last_error = truncate_middle(
                    &format!(
                        "theme source changed after an output hotplug, but its palette could not be applied: {error}"
                    ),
                    MAX_LAST_ERROR_BYTES,
                );
            }
        }
        self.refresh_independent_last_error();
    }

    fn remember_hotplug_attempt(
        &mut self,
        selected: &HashSet<String>,
        batch_error: Option<&String>,
        now: Instant,
    ) {
        let recorded_failures = self.last_apply_failures.clone();
        for connector in selected {
            let mut failures: Vec<ApplyFailure> = recorded_failures
                .iter()
                .filter(|failure| failure.output == *connector)
                .cloned()
                .collect();
            if failures.is_empty()
                && recorded_failures.is_empty()
                && let (Some(error), Some(entry)) =
                    (batch_error, self.route_current_entry(connector))
            {
                failures.push(ApplyFailure {
                    key: EntryKey {
                        playlist_id: self.routes[connector].active_playlist.clone(),
                        entry_id: entry.id.clone(),
                    },
                    output: connector.clone(),
                    reason: error.clone(),
                });
            }

            if failures.is_empty() {
                if let Some(route) = self.routes.get_mut(connector) {
                    route.last_cycle = now;
                }
                self.pending_routes.remove(connector);
                continue;
            }

            let Some(candidate) = self.routes.get(connector).cloned() else {
                continue;
            };
            let reason = bounded_failure_summary(
                &failures
                    .iter()
                    .map(|failure| failure.reason.clone())
                    .collect::<Vec<_>>(),
            );
            self.pending_routes.insert(
                connector.clone(),
                PendingRouteAutomatic {
                    connector: connector.clone(),
                    baseline: candidate.clone(),
                    candidate,
                    attempts: 1,
                    next_attempt: now + AUTOMATIC_RETRY_DELAY,
                    reason: "hotplug",
                    restore_baseline: false,
                    failures,
                },
            );
            if let Some(route) = self.routes.get_mut(connector) {
                route.last_error = truncate_middle(
                    &format!(
                        "automatic hotplug attempt 1/{AUTOMATIC_APPLY_ATTEMPTS} failed: {reason}; paired still remains the fallback"
                    ),
                    MAX_LAST_ERROR_BYTES,
                );
                route.renderer_failed = true;
            }
        }
        self.refresh_independent_last_error();
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
                if failures.is_empty()
                    && let Some(key) = attempted
                {
                    failures.push(ApplyFailure {
                        key,
                        output: connector.clone(),
                        reason: error.clone(),
                    });
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
                let renderer_failed = !pending.restore_baseline || rollback_failed;
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
                    if !pending.restore_baseline && self.move_route_forward(&connector) {
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
                    route.renderer_failed = renderer_failed;
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
        // A display the hand-over doesn't change keeps its renderer (review
        // r13f N13): what shows now is the baseline's, unless a retry after
        // a taboo has none of its own.
        let resident = if pending.restore_baseline {
            self.resident_targets()
        } else {
            Vec::new()
        };
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
        match self.apply_current_keeping(&resident) {
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
                let rollback_failed = rollback.is_some();
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
                self.renderer_failed = !pending.restore_baseline || rollback_failed;

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
                            moved |= self.move_cursor_forward(&playlist);
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

    /// Whether the driver refused `entry`'s item this generation
    /// (`refused_scenes`).
    fn is_refused(&self, entry: &Entry) -> bool {
        self.refusal_of(entry).is_some()
    }

    /// Why the driver refused `entry`'s item this generation, if it did.
    fn refusal_of(&self, entry: &Entry) -> Option<&Refusal> {
        if self.refused_scenes.is_empty() || entry.kind != EntryKind::Scene {
            return None;
        }
        self.refused_scenes.get(entry.rendered_scene()?)
    }

    /// Whether a cursor steps over `entry`: it is taboo or refused.
    fn cant_play(&self, playlist_id: &str, entry: &Entry) -> bool {
        self.is_taboo(playlist_id, &entry.id) || self.is_refused(entry)
    }

    /// Whether `playlist` has an entry the driver refused.
    fn holds_refused(&self, playlist_id: &str) -> bool {
        !self.refused_scenes.is_empty()
            && self
                .config
                .playlist(playlist_id)
                .is_some_and(|playlist| playlist.entries.iter().any(|entry| self.is_refused(entry)))
    }

    /// Whether nothing of `playlist` can play this generation because of a
    /// refusal: every entry is refused or taboo, and one at least is
    /// refused. A playlist whose every entry is only taboo keeps the taboo
    /// rules (its first still, motion off).
    fn refused_throughout(&self, playlist: &Playlist) -> bool {
        !self.refused_scenes.is_empty()
            && playlist
                .entries
                .iter()
                .all(|entry| self.cant_play(&playlist.id, entry))
            && playlist.entries.iter().any(|entry| self.is_refused(entry))
    }

    /// The playlists that may stand in for `playlist` (`stand_in`), in turn:
    /// the default playlist, then All media, each when it isn't `playlist`
    /// and has entries.
    fn stand_in_candidates<'a>(
        &'a self,
        playlist: &'a Playlist,
    ) -> impl Iterator<Item = &'a Playlist> {
        let default = self.config.playlist(&self.config.default_playlist);
        let all_media = self
            .config
            .playlists
            .iter()
            .find(|candidate| candidate.id == ALL_MEDIA_PLAYLIST_ID);
        [default, all_media]
            .into_iter()
            .flatten()
            .filter(move |candidate| candidate.id != playlist.id && !candidate.entries.is_empty())
    }

    /// What a display on `reference` plays: that playlist, unless the driver
    /// refused it throughout; then the default playlist, then All media,
    /// each only if something of it can play. None when nothing can: the
    /// display then plays nothing (`nothing_reason`). Owner: "skip to next,
    /// if it's in a playlist alone, then use the default playlist in
    /// schedule."
    fn stand_in(&self, reference: &str) -> Option<&Playlist> {
        let playlist = self.config.playlist(reference)?;
        if !self.refused_throughout(playlist) {
            return Some(playlist);
        }
        self.stand_in_candidates(playlist)
            .find(|candidate| !self.refused_throughout(candidate))
    }

    /// `stand_in`'s id, or `reference`'s own when nothing stands in.
    fn shown_playlist_id(&self, reference: &str) -> String {
        self.stand_in(reference)
            .or_else(|| self.config.playlist(reference))
            .map_or_else(|| reference.to_string(), |playlist| playlist.id.clone())
    }

    /// What each connected display shows in mirrored mode with `active` in
    /// force, as `current_targets` chooses: with a rule deciding (`ruled`)
    /// or a pick, `active` on every display; otherwise, with assignments,
    /// each display's own playlist or the default. Each as
    /// `shown_playlist_id` names it: a stand-in for one refused throughout.
    fn mirrored_shown(&self, ruled: bool, active: &str) -> Vec<String> {
        if self.config.displays.is_empty() {
            return vec![self.shown_playlist_id(active)];
        }
        self.target_outputs
            .iter()
            .map(|output| {
                let reference = if self.manual_playlist.is_some() || ruled {
                    active
                } else {
                    self.config
                        .displays
                        .iter()
                        .find(|display| display.connector == *output)
                        .map_or(self.config.default_playlist.as_str(), |display| {
                            display.playlist.as_str()
                        })
                };
                self.shown_playlist_id(reference)
            })
            .collect()
    }

    /// The playlist asked for (`reference`, a pick, a rule's, an assignment
    /// or the default) when a display plays a stand-in for it, the service
    /// having refused it throughout: what status names as `stand_in_for`.
    /// None when it plays its own, or nothing.
    fn stood_in_for<'a>(&'a self, reference: &str, shown: &str) -> Option<&'a str> {
        let asked = self.config.playlist(reference)?;
        (asked.id != shown && self.shown_playlist_id(&asked.id) == shown)
            .then_some(asked.id.as_str())
    }

    /// The playlist `route` asked for when it last decided what `connector`
    /// plays: its pick, its rule's, its assignment or the default, as its
    /// `source` and `schedule_rule_id` say. The same moment as its
    /// `active_playlist`, which a fresh `automatic_route` isn't between a
    /// rule's boundary and the tick that applies it.
    fn route_asked_for<'a>(&'a self, connector: &str, route: &'a DisplayRoute) -> Option<&'a str> {
        match route.source {
            RouteSource::Manual => route.manual_playlist.as_deref(),
            RouteSource::Schedule => {
                let rule_id = route.schedule_rule_id.as_deref()?;
                self.config
                    .schedules
                    .iter()
                    .find(|rule| rule.id == rule_id)
                    .map(|rule| rule.playlist.as_str())
            }
            RouteSource::Assignment => self
                .config
                .displays
                .iter()
                .find(|assignment| assignment.connector == connector)
                .map(|assignment| assignment.playlist.as_str()),
            RouteSource::Default => Some(self.config.default_playlist.as_str()),
        }
    }

    /// Why a display on `playlist` plays nothing when every entry it could
    /// play was refused (`stand_in` is None): over the refusals of what it
    /// tried, its own playlist and each that could stand in, not over every
    /// refusal of the document (another display's are not this one's).
    fn refused_nothing_reason(&self, playlist: &Playlist) -> &'static str {
        let web_only = std::iter::once(playlist)
            .chain(self.stand_in_candidates(playlist))
            .flat_map(|tried| tried.entries.iter())
            .filter_map(|entry| self.refusal_of(entry))
            .all(|refusal| refusal.web_page);
        if web_only {
            REFUSED_WEB_PAGES
        } else {
            REFUSED_ITEMS
        }
    }

    /// Remember that the driver won't start `entry`'s item, once a
    /// generation, and say so in the journal. True when it is new.
    fn remember_refusal(&mut self, playlist_id: &str, entry: &Entry, refusal: Refusal) -> bool {
        let Some(scene) = entry.rendered_scene() else {
            return false;
        };
        if self.refused_scenes.contains(scene) {
            return false;
        }
        eprintln!(
            "wall-in-one-service: skipped entry {:?} of playlist {playlist_id:?}: {refusal}",
            entry.id
        );
        self.refused_scenes.insert(scene, refusal)
    }

    /// Ask the driver about the entries of `playlist_id` at `visiting`
    /// (entry indexes, in the order a cursor comes to them), up to the first
    /// it would start, and remember each it refuses: one pass, so a cursor
    /// then moves past them all in one step instead of one refusal a round.
    /// Taboo and known-refused entries are passed over without asking, as a
    /// cursor passes them; a taboo first entry stays where it is when
    /// `taboo_stays` (it shows its still with motion off, and isn't asked).
    /// `cleared` holds what this walk already found startable. True when
    /// something new was refused.
    fn ask_ahead(
        &mut self,
        playlist_id: &str,
        visiting: &[usize],
        settings: &crate::config::Settings,
        taboo_stays: bool,
        cleared: &mut Cleared,
    ) -> bool {
        let mut learned = false;
        for (step, index) in visiting.iter().enumerate() {
            let Some(entry) = self
                .config
                .playlist(playlist_id)
                .and_then(|playlist| playlist.entries.get(*index))
            else {
                break;
            };
            if self.is_taboo(playlist_id, &entry.id) {
                if step == 0 && taboo_stays {
                    break;
                }
                continue;
            }
            if self.is_refused(entry) {
                continue;
            }
            let key = (entry.kind == EntryKind::Scene)
                .then(|| entry.rendered_scene())
                .flatten()
                .map(|scene| (scene.clone(), settings.dynamics_enabled));
            if key.as_ref().is_some_and(|key| cleared.contains(key)) {
                break;
            }
            match self.driver.motion_refusal(entry, settings) {
                Some(refusal) => {
                    let entry = entry.clone();
                    learned |= self.remember_refusal(playlist_id, &entry, refusal);
                }
                None => {
                    if let Some(key) = key {
                        cleared.insert(key);
                    }
                    break;
                }
            }
        }
        learned
    }

    /// The settings motion on `output` would be started with under
    /// `playback`, as `effective_motion_settings` gives them, without its
    /// bookkeeping: for asking ahead of a change that may not happen.
    fn motion_settings_for(
        &self,
        output: &str,
        playback: PlaybackState,
    ) -> crate::config::Settings {
        let inhibited = self.animations_inhibited();
        let held_pause = playback == PlaybackState::Paused
            && (self.power_released_outputs.contains(output)
                || (!self.is_independent() && self.power_released_outputs.contains("")));
        let mut settings = self.config.settings.clone();
        settings.dynamics_enabled &=
            !inhibited && playback != PlaybackState::Stopped && !held_pause;
        settings
    }

    /// Before a schedule change puts `reference` in force on the mirrored
    /// displays, ask about its entries from where its cursor would start
    /// (`reset_cursor_automatic`), so a playlist the service refuses
    /// throughout is known to be before the change is decided: a change
    /// whose stand-in is what already plays then changes nothing on screen.
    fn ask_ahead_of_mirrored_change(&mut self, reference: &str) {
        let Some(playlist) = self.config.playlist(reference) else {
            return;
        };
        let playlist_id = playlist.id.clone();
        if self.shown_playlist_id(&playlist_id) != playlist_id {
            return;
        }
        let Some(visiting) = self
            .cursors
            .get(&playlist_id)
            .map(|cursor| cursor.order.clone())
        else {
            return;
        };
        let mut settings = self.motion_settings_for("", self.playback_state);
        settings.dynamics_enabled = self.target_outputs.iter().any(|output| {
            self.motion_settings_for(output, self.playback_state)
                .dynamics_enabled
        });
        self.ask_ahead(
            &playlist_id,
            &visiting,
            &settings,
            false,
            &mut Cleared::new(),
        );
    }

    /// The same for an independent route about to follow `reference`, from
    /// where its new cursor would start (`new_route_cursor`).
    fn ask_ahead_of_route_change(&mut self, connector: &str, reference: &str) {
        let Some(playlist) = self.config.playlist(reference) else {
            return;
        };
        let playlist_id = playlist.id.clone();
        let visiting: Vec<usize> = (0..playlist.entries.len()).collect();
        let Some(playback) = self.routes.get(connector).map(|route| route.playback_state) else {
            return;
        };
        if self.shown_playlist_id(&playlist_id) != playlist_id {
            return;
        }
        let settings = self.motion_settings_for(connector, playback);
        self.ask_ahead(
            &playlist_id,
            &visiting,
            &settings,
            false,
            &mut Cleared::new(),
        );
    }

    /// Put the cursor of what a display on `reference` plays at its start:
    /// that playlist's first entry that can play (`reset_cursor_automatic`).
    /// A stand-in (`stand_in`) keeps its rotation where it is when its entry
    /// there can play: the person chose `reference`, not the stand-in, and
    /// the stand-in may already be playing. True when there is an entry.
    fn start_cursor_for(&mut self, reference: &str) -> bool {
        let shown = self.shown_playlist_id(reference);
        let own = self
            .config
            .playlist(reference)
            .map(|playlist| playlist.id.as_str());
        if own.is_some_and(|own| own != shown)
            && self
                .current_entry_for(&shown)
                .is_some_and(|entry| !self.cant_play(&shown, entry))
        {
            return true;
        }
        self.reset_cursor_automatic(&shown)
    }

    fn playlist_has_usable_entry(&self, playlist_id: &str) -> bool {
        self.config.playlist(playlist_id).is_some_and(|playlist| {
            playlist
                .entries
                .iter()
                .any(|entry| !self.is_taboo(&playlist.id, &entry.id))
        })
    }

    /// Why a display on `reference`'s playlist plays nothing (schema 8: no
    /// entries, and its reason; or every entry it could play was refused,
    /// `stand_in`), or None when it has something to play.
    fn nothing_reason(&self, reference: &str) -> Option<&str> {
        let playlist = self.config.playlist(reference)?;
        if let Some(reason) = playlist.nothing_to_play() {
            return Some(reason);
        }
        self.stand_in(&playlist.id)
            .is_none()
            .then(|| self.refused_nothing_reason(playlist))
    }

    /// Whether a command may choose `playlist_id`: something in it can play,
    /// or it plays nothing on purpose (a display on it then shows nothing).
    /// A playlist whose every entry is taboo is still refused.
    fn playlist_selectable(&self, playlist_id: &str) -> bool {
        self.playlist_has_usable_entry(playlist_id) || self.nothing_reason(playlist_id).is_some()
    }

    /// The reason, when every effective mirrored playlist plays nothing.
    fn mirrored_nothing_reason(&self) -> Option<&str> {
        let effective = self.effective_playlist_ids();
        let mut reason = None;
        for playlist in &effective {
            let own = self.nothing_reason(playlist)?;
            reason.get_or_insert(own);
        }
        reason
    }

    /// Why one independent display route plays nothing, or None.
    fn route_nothing_reason(&self, connector: &str) -> Option<&str> {
        let route = self.routes.get(connector)?;
        self.nothing_reason(&route.active_playlist)
    }

    /// The reason, when every one of `connectors` plays nothing.
    fn routes_nothing_reason<'c>(
        &self,
        connectors: impl IntoIterator<Item = &'c String>,
    ) -> Option<&str> {
        let mut reason = None;
        for connector in connectors {
            let own = self.route_nothing_reason(connector)?;
            reason.get_or_insert(own);
        }
        reason
    }

    fn taboo_playlist_error(&self, playlist_id: &str) -> String {
        let name = self
            .config
            .playlist(playlist_id)
            .map_or(playlist_id, |playlist| playlist.name.as_str());
        format!("playlist {name:?} has no usable entries; every entry is marked taboo (Borked)")
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

    /// A playlist's saved shuffle default: its own override, otherwise
    /// the global setting. A live `shuffle on|off` override still wins.
    fn playlist_shuffle_default(&self, reference: &str) -> bool {
        self.config
            .playlist(reference)
            .and_then(|playlist| playlist.shuffle)
            .unwrap_or(self.config.settings.shuffle)
    }

    /// A playlist's rotation interval: its own override, otherwise the
    /// global setting.
    fn playlist_cycle_interval_seconds(&self, reference: &str) -> u64 {
        self.config
            .playlist(reference)
            .and_then(|playlist| playlist.cycle_interval_seconds)
            .unwrap_or(self.config.settings.cycle_interval_seconds)
    }

    /// Effective shuffle for one independent route's current playlist.
    fn route_shuffle(&self, route: &DisplayRoute) -> bool {
        route.shuffle_enabled(self.playlist_shuffle_default(&route.active_playlist))
    }

    /// Effective mirrored shuffle for one playlist's cursor.
    fn mirrored_shuffle(&self, reference: &str) -> bool {
        self.shuffle_override
            .unwrap_or_else(|| self.playlist_shuffle_default(reference))
    }

    /// The one value every effective mirrored playlist agrees on, else the
    /// global fallback. Mirrored mode advances every output on one timer;
    /// with several effective playlists (Rust-authored display assignments
    /// beneath no schedule) a per-playlist value applies only when they all
    /// share it. The app compiles no mirrored assignments, so it always has
    /// exactly one effective playlist here.
    fn mirrored_common<T: PartialEq + Copy>(&self, value: impl Fn(&str) -> T, fallback: T) -> T {
        if !self.config.has_rotation_overrides {
            // Every playlist uses the global value: skip the per-tick lookup.
            return fallback;
        }
        let effective = self.effective_playlist_ids();
        let mut values = effective.iter().map(|playlist| value(playlist));
        match values.next() {
            Some(first) if values.all(|other| other == first) => first,
            _ => fallback,
        }
    }

    fn mirrored_cycle_interval_seconds(&self) -> u64 {
        self.mirrored_common(
            |playlist| self.playlist_cycle_interval_seconds(playlist),
            self.config.settings.cycle_interval_seconds,
        )
    }

    fn mirrored_shuffle_default(&self) -> bool {
        self.mirrored_common(
            |playlist| self.playlist_shuffle_default(playlist),
            self.config.settings.shuffle,
        )
    }

    fn shuffle_enabled(&self) -> bool {
        self.shuffle_override
            .unwrap_or_else(|| self.mirrored_shuffle_default())
    }

    /// Whether `connector`'s explicit assignment opted in to beating global
    /// schedule rules. Mirrored mode never routes per connector, so the flag
    /// stays dormant there.
    fn beats_global_rules(&self, connector: &str) -> bool {
        self.is_independent()
            && self.config.displays.iter().any(|assignment| {
                assignment.connector == connector && assignment.beats_global_rules
            })
    }

    /// The automatic (non-manual) winner for one independent connector.
    ///
    /// Without the display's opt-in this is the established order: the last
    /// matching rule visible to the connector (global or targeted, in authored
    /// order), then its assignment, then the default playlist. With the
    /// opt-in, only rules aimed at this connector can beat its own playlist;
    /// global rules are outranked by it. Routing and status both use this, so
    /// a beaten global rule is never reported as selected for this display.
    fn automatic_route(
        &self,
        connector: &str,
        at: NaiveDateTime,
    ) -> Result<(String, RouteSource, Option<String>), String> {
        let assignment = self
            .config
            .displays
            .iter()
            .find(|assignment| assignment.connector == connector);
        let rule = if self.beats_global_rules(connector) {
            schedule::resolve_targeted_rule(&self.config.schedules, connector, at)
        } else {
            schedule::resolve_rule_for(&self.config.schedules, Some(connector), at)
        }
        .map_err(|error| error.to_string())?;
        if let Some(rule) = rule {
            return Ok((
                rule.playlist.clone(),
                RouteSource::Schedule,
                Some(rule.id.clone()),
            ));
        }
        if let Some(assignment) = assignment {
            return Ok((assignment.playlist.clone(), RouteSource::Assignment, None));
        }
        Ok((
            self.config.default_playlist.clone(),
            RouteSource::Default,
            None,
        ))
    }

    /// The playlist `connector` plays, and why: its pick, rule, assignment
    /// or the default, or that one's stand-in once the service refused it
    /// throughout (`stand_in`). The reason stays the one that chose it.
    fn route_decision(
        &self,
        connector: &str,
        manual: Option<&str>,
        at: NaiveDateTime,
    ) -> Result<(String, RouteSource, Option<String>), String> {
        let (wanted, source, rule_id) = match manual {
            Some(manual) => (manual.to_string(), RouteSource::Manual, None),
            None => self.automatic_route(connector, at)?,
        };
        Ok((self.shown_playlist_id(&wanted), source, rule_id))
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
        let entries = playlist.entries.len();
        if entries == 0 {
            if playlist.nothing_to_play().is_some() {
                // A route that plays nothing: no position to hold.
                return Ok(PlaylistCursor {
                    order: Vec::new(),
                    position: 0,
                    history: Vec::new(),
                    forward: Vec::new(),
                });
            }
            return Err(format!("display route playlist {playlist_id:?} is empty"));
        }
        let current = current_entry
            .and_then(|wanted| playlist.entries.iter().position(|entry| entry.id == wanted))
            .or_else(|| {
                automatic.then(|| {
                    playlist
                        .entries
                        .iter()
                        .position(|entry| !self.cant_play(playlist_id, entry))
                        .unwrap_or(0)
                })
            })
            .unwrap_or(0);
        let (order, position) = if shuffle {
            let mut rest: Vec<usize> = (0..entries).filter(|index| *index != current).collect();
            self.rng.shuffle(&mut rest);
            let mut order = Vec::with_capacity(entries);
            order.push(current);
            order.extend(rest);
            (order, 0)
        } else {
            ((0..entries).collect(), current)
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
            let default = self.playlist_shuffle_default(&wanted);
            let shuffle = self
                .routes
                .get(connector)
                .map_or(default, |route| route.shuffle_enabled(default));
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
                self.insert_independent_route(connector)?;
            }
        }
        // A configured assignment has a route while its display is detached,
        // however it got there: a start seeds one from the assignments, and a
        // reload or a later probe must too, or status loses its row.
        let unrouted: Vec<String> = self
            .config
            .displays
            .iter()
            .map(|display| display.connector.clone())
            .filter(|connector| !connector.is_empty() && !self.routes.contains_key(connector))
            .collect();
        for connector in &unrouted {
            self.route_generation = self.route_generation.wrapping_add(1);
            self.insert_independent_route(connector)?;
        }
        // Never evicted: they number at most MAX_DISPLAYS, which is no more
        // than MAX_RETAINED_DISPLAY_ROUTES, so the loop always ends.
        let assigned: HashSet<&str> = self
            .config
            .displays
            .iter()
            .map(|display| display.connector.as_str())
            .collect();
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
                .filter(|(connector, _)| {
                    !connected_set.contains(connector.as_str())
                        && !assigned.contains(connector.as_str())
                })
                .min_by_key(|(_, route)| route.last_seen)
                .map(|(connector, _)| connector.clone())
            else {
                return Err("could not evict a detached display route".into());
            };
            self.routes.remove(&evicted);
        }
        Ok(())
    }

    /// A new route for `connector`, as its rules and assignment decide now.
    /// The session's Cycle override, when there is one, holds for it too.
    fn insert_independent_route(&mut self, connector: &str) -> Result<(), String> {
        let (playlist, source, schedule_rule_id) = self
            .route_decision(connector, None, self.current_time)
            .map_err(|error| format!("display {connector}: {error}"))?;
        let shuffle = self.playlist_shuffle_default(&playlist);
        let cursor = self
            .new_route_cursor(&playlist, None, shuffle, true)
            .map_err(|error| format!("display {connector}: {error}"))?;
        self.routes.insert(
            connector.to_string(),
            DisplayRoute {
                manual_playlist: None,
                active_playlist: playlist,
                schedule_rule_id,
                source,
                cursor,
                playback_state: PlaybackState::Playing,
                shuffle_override: None,
                cycle_override: self.cycle_override,
                renderer_failed: false,
                last_error: String::new(),
                last_cycle: Instant::now(),
                last_seen: self.route_generation,
            },
        );
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
            let (playlist, source, schedule_rule_id) = self
                .route_decision(&connector, manual.as_deref(), self.current_time)
                .map_err(|error| format!("display {connector}: {error}"))?;
            let current = previous_entries
                .get(&connector)
                .and_then(|(previous, entry)| (previous == &playlist).then_some(entry.as_str()));
            let shuffle =
                self.routes[&connector].shuffle_enabled(self.playlist_shuffle_default(&playlist));
            let cursor = self
                .new_route_cursor(&playlist, current, shuffle, false)
                .map_err(|error| format!("display {connector}: {error}"))?;
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
            power_released_outputs: connectors
                .iter()
                .filter(|connector| self.power_released_outputs.contains(*connector))
                .cloned()
                .collect(),
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
            // A failed Play may have cleared the battery-released marker
            // before motion failed. Restore the selected route's residency
            // along with its transport, without touching unselected outputs.
            if snapshot.power_released_outputs.contains(connector) {
                self.power_released_outputs.insert(connector.clone());
            } else {
                self.power_released_outputs.remove(connector);
            }
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
        self.apply_current_keeping(&[])
    }

    /// `apply_current`, where a mirrored display whose target is one of
    /// `resident` (what it shows now, `resident_targets`) keeps its renderer.
    fn apply_current_keeping(&mut self, resident: &[Target]) -> Result<String, String> {
        self.last_output_probe = Instant::now();
        self.driver.begin_apply();
        let discovered = self.probe_outputs();
        let result = self.apply_current_in_batch(discovered, resident);
        self.driver.end_apply();
        result
    }

    /// What the mirrored displays show now, as far as the runtime knows: the
    /// targets of the selection last applied, each with the entry it plays.
    /// Only with assignments (each display then has a target of its own),
    /// after an apply that succeeded, and while nothing has failed since: a
    /// failed display is applied again. Empty otherwise, which applies every
    /// display.
    fn resident_targets(&self) -> Vec<Target> {
        if self.is_independent()
            || self.config.displays.is_empty()
            || self.renderer_failed
            || self.authoritative_generation == 0
        {
            return Vec::new();
        }
        self.current_targets(&self.target_outputs)
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
        resident: &[Target],
    ) -> Result<String, String> {
        let independent = self.is_independent();
        let previous_outputs: HashSet<String> = self.target_outputs.iter().cloned().collect();
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
                    // The compositor may not be ready during login. Preserve the former
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
            for connector in outputs
                .iter()
                .filter(|connector| !previous_outputs.contains(*connector))
            {
                self.refresh_route_decision(connector, true)?;
            }
            let selected: HashSet<String> = outputs.iter().cloned().collect();
            return self.apply_independent_in_batch(&selected);
        }
        self.skip_refused_mirrored();
        let mut result = self.apply_mirrored_targets(resident);
        // An item whose type changed between the driver's answer and its
        // start is refused there instead, its still already up: it is
        // skipped the same way. Each round remembers one item more.
        while self.learn_refusals_after_apply() {
            self.skip_refused_mirrored();
            result = self.apply_mirrored_targets(resident);
        }
        result
    }

    /// Before anything is shown, move every mirrored playlist in force off
    /// the entries the driver would not start now (`motion_refusal`): to its
    /// next entry, then to its stand-in (`stand_in`), or to nothing. A
    /// taboo entry shows its still with motion off, so it isn't asked.
    ///
    /// Each round asks ahead along each playlist in force (`ask_ahead`), up
    /// to its first entry that can start, then moves each in one step: a run
    /// of refused entries costs one pass, and each item is asked once a
    /// walk. A round after the first only looks at a stand-in just put in
    /// force, so there are at most as many as a playlist has stand-ins.
    fn skip_refused_mirrored(&mut self) {
        let mut cleared = Cleared::new();
        loop {
            self.step_over_refused_mirrored();
            let mut learned = false;
            for target in self.current_targets(&self.target_outputs) {
                if self.is_taboo(&target.playlist_id, &target.entry.id) {
                    continue;
                }
                let settings = self.effective_motion_settings(&target.output, self.playback_state);
                let Some(visiting) = self.cursors.get(&target.playlist_id).map(visiting_order)
                else {
                    continue;
                };
                learned |= self.ask_ahead(
                    &target.playlist_id,
                    &visiting,
                    &settings,
                    true,
                    &mut cleared,
                );
            }
            if !learned {
                return;
            }
        }
    }

    /// Move each mirrored playlist in force whose entry is refused to its
    /// next one that can play. One that has none gives way to its stand-in,
    /// which is then looked at too; each playlist moves once.
    fn step_over_refused_mirrored(&mut self) {
        let mut stepped = HashSet::new();
        loop {
            let refused: Vec<String> = self
                .effective_playlist_ids()
                .into_iter()
                .filter(|playlist| {
                    self.current_entry_for(playlist)
                        .is_some_and(|entry| self.is_refused(entry))
                })
                .filter(|playlist| stepped.insert(playlist.clone()))
                .collect();
            if refused.is_empty() {
                return;
            }
            for playlist in refused {
                self.move_cursor_forward(&playlist);
            }
        }
    }

    /// Remember what the driver refused at the start itself (`motion_refused`)
    /// for the targets just applied. True when something new was refused.
    fn learn_refusals_after_apply(&mut self) -> bool {
        let mut learned = false;
        for target in self.current_targets(&self.target_outputs) {
            if let Some(refusal) = self.driver.motion_refused(&target.output) {
                learned |= self.remember_refusal(&target.playlist_id, &target.entry, refusal);
            }
        }
        learned
    }

    /// Apply every mirrored display's target, break before make. One whose
    /// target is in `resident`, which it already shows, keeps its renderer
    /// (review r13f N13): the shell is handed its still and palette again
    /// (`apply_keeping_renderer`), in its turn, so the shell shows what an
    /// apply of every display would leave, its one palette included, even
    /// where it lost a still meanwhile. A taboo entry's still is applied all
    /// the same.
    fn apply_mirrored_targets(&mut self, resident: &[Target]) -> Result<String, String> {
        let targets = self.current_targets(&self.target_outputs);
        let outputs: Vec<String> = targets.iter().map(|target| target.output.clone()).collect();
        // Every renderer of ours on an output without a target stops here,
        // a display that plays nothing included.
        self.driver.retain_outputs(&outputs);
        self.last_apply_failures.clear();
        if targets.is_empty() {
            if let Some(reason) = self.mirrored_nothing_reason().map(str::to_owned) {
                // Nothing to play is a state, not a failure: the displays show
                // nothing of ours, and status says why (`nothing_to_play`).
                self.supersede_startup_apply();
                self.last_error.clear();
                self.renderer_failed = false;
                return Ok(format!("nothing to play: {reason}"));
            }
            return self.fail("active display playlists are empty");
        }
        let played = targets[0].entry.id.clone();
        let mut errors = Vec::new();
        let mut static_fallbacks = Vec::new();
        let mut missing_programs: Vec<String> = Vec::new();
        for target in targets {
            let mut settings = self.effective_motion_settings(&target.output, self.playback_state);
            let mut kept = resident.contains(&target);
            if let Some(diagnostic) =
                self.taboo_fallback_diagnostic(&target.playlist_id, &target.entry.id)
            {
                settings.dynamics_enabled = false;
                kept = false;
                static_fallbacks.push(if target.output.is_empty() {
                    diagnostic
                } else {
                    format!("{}: {diagnostic}", target.output)
                });
            }
            let applied = if kept {
                self.driver
                    .apply_keeping_renderer(&target.entry, &target.output)
            } else {
                self.driver.apply(&target.entry, &target.output, &settings)
            };
            match applied {
                Ok(()) => {
                    // A refusal at the start itself is skipped by the caller
                    // (`learn_refusals_after_apply`), never said as a fault.
                    // The renderer program is not there: the still is up and
                    // the entry is fine, so this is said like a taboo's still,
                    // never recorded against the entry.
                    if let Some(missing) = self.driver.motion_missing(&target.output) {
                        let diagnostic = missing_renderer_diagnostic(&missing);
                        static_fallbacks.push(if target.output.is_empty() {
                            diagnostic
                        } else {
                            format!("{}: {diagnostic}", target.output)
                        });
                        let short = missing.short();
                        if !missing_programs.contains(&short) {
                            missing_programs.push(short);
                        }
                    }
                }
                Err(error) => {
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
        }
        if self.playback_state == PlaybackState::Paused
            && !self.animations_inhibited()
            && let Err(error) = self.driver.set_paused(true)
        {
            // The newly started renderer could not be paused. That is a failed
            // attempt to honor the user's Paused intent, not a Play command:
            // keep Paused (the cursor stays held, as in independent mode) and
            // report the failure through renderer_failed below, so Play is
            // the retry. A multi-output pause can fail after an earlier child
            // paused, so undo it on a best-effort basis; a resumed child
            // under a Paused status is released by Play's retry.
            let rollback = self
                .driver
                .set_paused(false)
                .err()
                .map(|detail| format!("; resume rollback also failed: {detail}"))
                .unwrap_or_default();
            errors.push(format!("could not pause renderer: {error}{rollback}"));
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
            let reply = match self.playback_state {
                PlaybackState::Playing => format!("playing {played}"),
                PlaybackState::Paused => format!("paused {played}"),
                PlaybackState::Stopped => format!("showing {played}; motion stopped"),
            };
            // A command that landed on a video or scene whose renderer can't
            // run succeeds, and says that only the still shows.
            Ok(if missing_programs.is_empty() {
                reply
            } else {
                format!(
                    "{reply}; only its still shows: {}",
                    missing_programs.join(", ")
                )
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
            for connector in &newly_connected {
                self.refresh_route_decision(connector, true)?;
            }
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
        self.skip_refused_routes(selected);
        let mut result = self.apply_routes(selected);
        // As in mirrored mode (`apply_current_in_batch`).
        while self.learn_route_refusals_after_apply(selected) {
            self.skip_refused_routes(selected);
            result = self.apply_routes(selected);
        }
        result
    }

    /// `skip_refused_mirrored` for the `selected` independent routes: each
    /// moves to its next entry that can play, then to its stand-in through
    /// its decision (`route_decision`), or to nothing. Asked ahead the same
    /// way, one pass along each route's playlist a round.
    fn skip_refused_routes(&mut self, selected: &HashSet<String>) {
        let mut connectors: Vec<String> = selected.iter().cloned().collect();
        connectors.sort();
        let mut cleared = Cleared::new();
        loop {
            for connector in &connectors {
                self.step_route_over_refused(connector);
            }
            let mut learned = false;
            for target in self.current_targets(&connectors) {
                let Some((playback, visiting)) = self
                    .routes
                    .get(&target.output)
                    .map(|route| (route.playback_state, visiting_order(&route.cursor)))
                else {
                    continue;
                };
                if self.is_taboo(&target.playlist_id, &target.entry.id) {
                    continue;
                }
                let settings = self.effective_motion_settings(&target.output, playback);
                learned |= self.ask_ahead(
                    &target.playlist_id,
                    &visiting,
                    &settings,
                    true,
                    &mut cleared,
                );
            }
            if !learned {
                return;
            }
        }
    }

    /// Move `connector`'s route off a refused entry: to the next of its
    /// playlist that can play, else to its stand-in, at most the default and
    /// All media after its own.
    fn step_route_over_refused(&mut self, connector: &str) {
        for _ in 0..3 {
            if !self
                .route_current_entry(connector)
                .is_some_and(|entry| self.is_refused(entry))
            {
                return;
            }
            if self.move_route_forward(connector) {
                return;
            }
            // Nothing of its playlist can play: its decision now names the
            // stand-in, or the same playlist when nothing can stand in.
            if !matches!(self.refresh_route_decision(connector, true), Ok(true)) {
                return;
            }
        }
    }

    /// `learn_refusals_after_apply` for the `selected` routes.
    fn learn_route_refusals_after_apply(&mut self, selected: &HashSet<String>) -> bool {
        let connectors: Vec<String> = selected.iter().cloned().collect();
        let mut learned = false;
        for target in self.current_targets(&connectors) {
            if let Some(refusal) = self.driver.motion_refused(&target.output) {
                learned |= self.remember_refusal(&target.playlist_id, &target.entry, refusal);
            }
        }
        learned
    }

    fn apply_routes(&mut self, selected: &HashSet<String>) -> Result<String, String> {
        let targets = self.current_targets(&self.target_outputs);
        let plays_nothing = self
            .target_outputs
            .iter()
            .any(|connector| self.route_nothing_reason(connector).is_some());
        if targets.is_empty() && !plays_nothing {
            return self.fail("active display playlists are empty");
        }
        // A route that plays nothing has no target, so its renderer stops.
        let all_outputs: Vec<String> = targets.iter().map(|target| target.output.clone()).collect();
        self.driver.retain_outputs(&all_outputs);
        let mut targets: Vec<Target> = targets
            .into_iter()
            .filter(|target| selected.contains(&target.output))
            .collect();
        if targets.is_empty() {
            let Some(reason) = self.routes_nothing_reason(selected).map(str::to_owned) else {
                return self.fail("none of the selected display routes has a playable entry");
            };
            for connector in selected {
                self.driver.stop_output_renderer(connector);
                if let Some(route) = self.routes.get_mut(connector) {
                    route.last_error.clear();
                    route.renderer_failed = false;
                }
            }
            self.refresh_independent_last_error();
            self.supersede_startup_apply();
            return Ok(format!(
                "nothing to play on {} display route(s): {reason}",
                selected.len()
            ));
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
            && let Some(target) = targets
                .iter()
                .find(|target| target.output == theme_source)
                .filter(|target| !blocked.contains(&target.output))
            && let Err(error) = self.driver.apply_palette_only(&target.entry)
        {
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

        for target in &targets {
            if blocked.contains(&target.output) {
                continue;
            }
            let Some(playback) = self
                .routes
                .get(&target.output)
                .map(|route| route.playback_state)
            else {
                continue;
            };
            let mut settings = self.effective_motion_settings(&target.output, playback);
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
            if let Some(missing) = self.driver.motion_missing(&target.output) {
                // Its still stays up; the entry is not at fault (see above).
                static_fallbacks
                    .insert(target.output.clone(), missing_renderer_diagnostic(&missing));
                continue;
            }
            if self.driver.motion_refused(&target.output).is_some() {
                // Skipped by the caller (`learn_route_refusals_after_apply`).
                continue;
            }
            if playback == PlaybackState::Paused
                && settings.dynamics_enabled
                && target.entry.kind != crate::config::EntryKind::Still
                && let Err(error) = self.driver.set_output_paused(&target.output, true)
            {
                errors.push(format!("{} pause: {error}", target.output));
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
                // A route that plays nothing because every entry it could
                // play was refused (a stand-in is installed by its decision,
                // `route_decision`).
                if self.stand_in(&playlist.id).is_none() {
                    continue;
                }
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
            if let Some(playlist) = self.stand_in(&self.active_playlist)
                && let Some(entry) = self.current_entry_for(&playlist.id).cloned()
            {
                targets.push(Target {
                    playlist_id: playlist.id.clone(),
                    entry,
                    output: String::new(),
                });
            }
        } else {
            for output in outputs {
                let reference = if self.manual_playlist.is_some() || self.schedule_rule.is_some() {
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
                if let Some(playlist) = self.stand_in(reference)
                    && let Some(entry) = self.current_entry_for(&playlist.id).cloned()
                {
                    targets.push(Target {
                        playlist_id: playlist.id.clone(),
                        entry,
                        output: output.clone(),
                    });
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

    fn refuse_taboo_routes(&self, connectors: &[String]) -> Result<(), String> {
        let blocked: Vec<String> = connectors
            .iter()
            .filter_map(|connector| {
                let route = self.routes.get(connector)?;
                let entry = self.route_current_entry(connector)?;
                self.is_taboo(&route.active_playlist, &entry.id).then(|| {
                    format!(
                        "{connector}: entry {:?} in playlist {:?} is marked taboo (Borked) and cannot play",
                        entry.id, route.active_playlist
                    )
                })
            })
            .collect();
        if blocked.is_empty() {
            Ok(())
        } else {
            Err(format!(
                "{}; use Next to choose a usable wallpaper, or remove/uninstall the Borked item",
                blocked.join("; ")
            ))
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
                if !self.playlist_selectable(&playlist_id) {
                    return Err(self.taboo_playlist_error(&playlist_id));
                }
                self.cancel_route_retries(&connectors);
                let mut changed = HashSet::new();
                let now = Instant::now();
                // What the displays play: the pick, or its stand-in once the
                // service refused it throughout (`stand_in`).
                let shown = self.shown_playlist_id(&playlist_id);
                for connector in &connectors {
                    let already_active = self.routes.get(connector).is_some_and(|route| {
                        route.manual_playlist.as_deref() == Some(playlist_id.as_str())
                            && route.active_playlist == shown
                            && route.playback_state == PlaybackState::Playing
                            && !route.renderer_failed
                    }) && self
                        .route_current_entry(connector)
                        .is_some_and(|entry| !self.cant_play(&shown, entry));
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
                        .shuffle_enabled(self.playlist_shuffle_default(&shown));
                    let cursor = self.new_route_cursor(&shown, None, shuffle, true)?;
                    let route = self.routes.get_mut(connector).expect("route was checked");
                    route.manual_playlist = Some(playlist_id.clone());
                    route.active_playlist = shown.clone();
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
                let mut decisions = HashMap::new();
                for connector in &connectors {
                    let decision = self.route_decision(connector, None, self.current_time)?;
                    if !self.playlist_selectable(&decision.0) {
                        return Err(format!(
                            "{connector}: {}",
                            self.taboo_playlist_error(&decision.0)
                        ));
                    }
                    decisions.insert(connector.clone(), decision);
                }
                self.cancel_route_retries(&connectors);
                let mut changed_routes = HashSet::new();
                let mut provenance_changed = false;
                for connector in &connectors {
                    let route = self
                        .routes
                        .get(connector)
                        .expect("connector came from active routes");
                    let (wanted, source, rule_id) = decisions
                        .get(connector)
                        .expect("every selected connector was preflighted")
                        .clone();
                    let current_is_usable = self
                        .route_current_entry(connector)
                        .is_some_and(|entry| !self.cant_play(&route.active_playlist, entry));
                    let unchanged_target = route.active_playlist == wanted && current_is_usable;
                    let unchanged_provenance = route.manual_playlist.is_none()
                        && route.active_playlist == wanted
                        && route.source == source
                        && route.schedule_rule_id == rule_id;
                    if unchanged_target {
                        if !unchanged_provenance {
                            let route = self
                                .routes
                                .get_mut(connector)
                                .expect("connector came from active routes");
                            route.manual_playlist = None;
                            route.source = source;
                            route.schedule_rule_id = rule_id;
                            provenance_changed = true;
                        }
                        continue;
                    }
                    let shuffle = route.shuffle_enabled(self.playlist_shuffle_default(&wanted));
                    let cursor = self.new_route_cursor(&wanted, None, shuffle, true)?;
                    let route = self
                        .routes
                        .get_mut(connector)
                        .expect("connector came from active routes");
                    route.manual_playlist = None;
                    route.active_playlist = wanted;
                    route.source = source;
                    route.schedule_rule_id = rule_id;
                    route.cursor = cursor;
                    route.last_cycle = Instant::now();
                    changed_routes.insert(connector.clone());
                }
                if changed_routes.is_empty() {
                    Ok(if provenance_changed {
                        "following schedule; wallpaper unchanged"
                    } else {
                        "already following schedule"
                    }
                    .into())
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
                self.refuse_taboo_routes(&connectors)?;
                self.cancel_route_retries(&connectors);
                let played = self.independent_play(&connectors)?;
                Ok(match self.routes_nothing_reason(&connectors) {
                    Some(reason) => format!("nothing to play: {reason}"),
                    None => played,
                })
            }
            "pause" => {
                Self::independent_no_argument(request, "pause")?;
                self.cancel_route_retries(&connectors);
                self.independent_pause(&connectors)
            }
            "toggle" => {
                Self::independent_no_argument(request, "toggle")?;
                if connectors.iter().any(|one| {
                    self.routes
                        .get(one)
                        .is_some_and(|route| route.playback_state == PlaybackState::Playing)
                }) {
                    self.cancel_route_retries(&connectors);
                    self.independent_pause(&connectors)?;
                } else {
                    self.refuse_taboo_routes(&connectors)?;
                    self.cancel_route_retries(&connectors);
                    self.independent_play(&connectors)?;
                }
                Ok(match self.routes_nothing_reason(&connectors) {
                    Some(reason) => format!("nothing to play: {reason}"),
                    None => "toggled selected display routes".into(),
                })
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
                    let before = self.route_shuffle(&self.routes[connector]);
                    self.routes
                        .get_mut(connector)
                        .expect("route exists")
                        .shuffle_override = override_value;
                    let after = self.route_shuffle(&self.routes[connector]);
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
                // Without a connector it is the session's override: every
                // route takes it, detached ones included, and so does each
                // route made later (`insert_independent_route`).
                let selected: Vec<String> = if connector.is_none() {
                    self.cycle_override = override_value;
                    self.routes.keys().cloned().collect()
                } else {
                    connectors.clone()
                };
                for connector in &selected {
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
                        "next" => self.move_route_forward(connector),
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
                    if let Some(reason) = self.routes_nothing_reason(&connectors) {
                        return Err(format!("nothing to play: {reason}"));
                    }
                    let refused = connectors.iter().any(|connector| {
                        self.routes
                            .get(connector)
                            .is_some_and(|route| self.holds_refused(&route.active_playlist))
                    });
                    return Err(if request.verb == "previous" {
                        "no previous playback history"
                    } else if refused {
                        REFUSED_REST
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
        .map(|message| {
            // A display that landed on a video or scene whose renderer can't
            // run shows its still, and the reply says so.
            let stills: Vec<String> = connectors
                .iter()
                .filter_map(|connector| {
                    self.driver
                        .motion_missing(connector)
                        .map(|missing| format!("{connector} shows its still: {}", missing.short()))
                })
                .collect();
            if stills.is_empty() {
                message
            } else {
                format!("{message}; {}", stills.join("; "))
            }
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
                (PlaybackState::Paused, false)
                    if !self.power_released_outputs.contains(connector) =>
                {
                    resume.push(connector.clone())
                }
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
                route.last_cycle = Instant::now();
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
                for connector in &restart_connectors {
                    self.routes
                        .get_mut(connector)
                        .expect("route exists")
                        .last_cycle = now;
                    if !self.animations_inhibited() {
                        self.power_released_outputs.remove(connector);
                    }
                }
            }
        }
        self.refresh_independent_last_error();
        if errors.is_empty() {
            Ok(if self.animations_inhibited() {
                "play requested; animations stopped on battery"
            } else {
                "playing selected display routes"
            }
            .into())
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
            let result = if self.animations_inhibited() {
                Ok(())
            } else {
                self.driver.set_output_paused(connector, true)
            };
            if let Err(error) = result {
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

    fn move_route_forward(&mut self, connector: &str) -> bool {
        let Some(route) = self.routes.get(connector) else {
            return false;
        };
        let playlist_id = route.active_playlist.clone();
        let Some(playlist) = self.config.playlist(&playlist_id) else {
            return false;
        };
        let entry_count = playlist.entries.len();
        let eligible: Vec<bool> = playlist
            .entries
            .iter()
            .map(|entry| !self.cant_play(&playlist_id, entry))
            .collect();
        let shuffle = self.route_shuffle(route);
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
            let mut next: Vec<usize> = (0..entry_count).filter(|index| eligible[*index]).collect();
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
        let Some(route) = self.routes.get(connector) else {
            return false;
        };
        let playlist_id = route.active_playlist.clone();
        let Some(playlist) = self.config.playlist(&playlist_id) else {
            return false;
        };
        let eligible: Vec<bool> = playlist
            .entries
            .iter()
            .map(|entry| !self.cant_play(&playlist_id, entry))
            .collect();
        let route = self.routes.get_mut(connector).expect("route exists");
        let cursor = &mut route.cursor;
        let Some(&current) = cursor.order.get(cursor.position) else {
            return false;
        };
        while let Some(previous) = cursor.history.pop() {
            if !eligible.get(previous).copied().unwrap_or(false) || previous == current {
                continue;
            }
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
            return true;
        }
        false
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
            .filter_map(|(index, entry)| (!self.cant_play(&playlist_id, entry)).then_some(index))
            .collect();
        let shuffle = self.route_shuffle(route);
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
                if next.is_empty() {
                    return false;
                }
                self.rng.shuffle(&mut next);
                if next[0] == current {
                    if next.len() == 1 {
                        return false;
                    }
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
        if !self.playlist_selectable(&playlist_id) {
            return self.fail(self.taboo_playlist_error(&playlist_id));
        }
        let resident = self.resident_targets();
        let baseline = self.selection_state();
        let previous_manual = self.manual_playlist.clone();
        self.manual_playlist = Some(playlist_id.clone());
        self.active_playlist = playlist_id.clone();
        self.schedule_rule = None;
        if !self.start_cursor_for(&playlist_id) && self.nothing_reason(&playlist_id).is_none() {
            self.manual_playlist = previous_manual;
            self.restore_selection(&baseline);
            return self.fail(self.taboo_playlist_error(&playlist_id));
        }
        // A display the pick doesn't change keeps its renderer.
        let result = self.apply_current_keeping(&resident);
        if result.is_ok() {
            self.last_cycle = Instant::now();
        }
        result
    }

    fn scheduled_selection(
        &self,
        at: NaiveDateTime,
    ) -> Result<(String, Option<String>, Vec<String>), String> {
        let scheduled = schedule::resolve_rule(&self.config.schedules, at)
            .map_err(|error| error.to_string())?;
        let rule = scheduled.map(|rule| rule.id.clone());
        let active = scheduled
            .map_or(&self.config.default_playlist, |rule| &rule.playlist)
            .to_string();
        if rule.is_some() || self.config.displays.is_empty() {
            return Ok((active.clone(), rule, vec![active]));
        }

        // With no schedule override, mirrored mode returns to each connected
        // display's assignment (or the default for an unassigned output), not
        // merely to `active`. Preflight and reset those effective playlists so
        // an inactive cursor which became taboo cannot be selected on return.
        let mut seen = HashSet::new();
        let effective = self
            .target_outputs
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
                seen.insert(playlist.id.clone())
                    .then(|| playlist.id.clone())
            })
            .collect();
        Ok((active, None, effective))
    }

    fn follow_schedule(&mut self, at: NaiveDateTime) -> Result<String, String> {
        let (playlist_id, rule, effective) = self.scheduled_selection(at)?;
        if let Some(blocked) = effective
            .iter()
            .find(|playlist| !self.playlist_selectable(playlist))
        {
            return self.fail(self.taboo_playlist_error(blocked));
        }
        let resident = self.resident_targets();
        let baseline = self.selection_state();
        let previous_manual = self.manual_playlist.take();
        self.schedule_rule = rule;
        self.active_playlist.clone_from(&playlist_id);
        for effective_playlist in &effective {
            if !self.start_cursor_for(effective_playlist)
                && self.nothing_reason(effective_playlist).is_none()
            {
                self.manual_playlist = previous_manual;
                self.restore_selection(&baseline);
                return self.fail(self.taboo_playlist_error(effective_playlist));
            }
        }
        let result = self.apply_current_keeping(&resident);
        if result.is_ok() {
            self.last_cycle = Instant::now();
        }
        result
    }

    /// Every playlist's effective mirrored shuffle, in configured order.
    fn mirrored_shuffles(&self) -> Vec<bool> {
        self.config
            .playlists
            .iter()
            .map(|playlist| self.mirrored_shuffle(&playlist.id))
            .collect()
    }

    fn set_shuffle(&mut self, value: Option<&str>) -> Result<String, String> {
        let before = self.mirrored_shuffles();
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
        if self.mirrored_shuffles() != before {
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
        if let Some(target) = self
            .current_targets(&self.target_outputs)
            .into_iter()
            .find(|target| self.is_taboo(&target.playlist_id, &target.entry.id))
        {
            return self.fail(format!(
                "entry {:?} in playlist {:?} is marked taboo (Borked) and cannot play; use Next to choose a usable wallpaper, or remove/uninstall it",
                target.entry.id, target.playlist_id
            ));
        }
        match self.playback_state {
            PlaybackState::Playing => {
                if self.renderer_failed {
                    self.apply_current()?;
                    self.last_cycle = Instant::now();
                }
            }
            PlaybackState::Paused => {
                if self.renderer_failed || !self.power_released_outputs.is_empty() {
                    let released_outputs = self.power_released_outputs.clone();
                    self.playback_state = PlaybackState::Playing;
                    if let Err(error) = self.apply_current() {
                        // One output can fail after another resumed. The
                        // baseline was already still-only, so release every
                        // surviving child before restoring Paused intent.
                        self.driver.stop();
                        self.playback_state = PlaybackState::Paused;
                        self.power_released_outputs = released_outputs;
                        self.power_released_outputs
                            .extend(self.target_outputs.iter().cloned());
                        return Err(error);
                    }
                    if !self.animations_inhibited() {
                        self.power_released_outputs.clear();
                    }
                } else if let Err(error) = self.driver.set_paused(false) {
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
                    // A failed resume did not restore motion everywhere: one
                    // output can fail after another started. As for Paused,
                    // release every surviving child, then keep status honest
                    // and the still-only state for another try.
                    self.driver.stop();
                    self.playback_state = PlaybackState::Stopped;
                    return Err(error);
                }
                self.last_cycle = Instant::now();
            }
        }
        if let Some(reason) = self.mirrored_nothing_reason() {
            // Play has nothing to start: the document names nothing here.
            return Ok(format!("nothing to play: {reason}"));
        }
        Ok(if self.animations_inhibited() {
            "play requested; animations stopped on battery"
        } else {
            "playing"
        }
        .into())
    }

    fn pause(&mut self) -> Result<String, String> {
        if self.playback_state == PlaybackState::Stopped {
            return Ok("stopped; use play to resume motion".into());
        }
        let result = if self.animations_inhibited() {
            Ok(())
        } else {
            self.driver.set_paused(true)
        };
        if let Err(error) = result {
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
        // A display on a playlist that doesn't move (one entry) keeps its
        // renderer.
        let resident = self.resident_targets();
        let mut moved = false;
        for playlist in self.effective_playlist_ids() {
            moved |= if delta < 0 {
                self.move_cursor_backward(&playlist)
            } else {
                self.move_cursor_forward(&playlist)
            };
        }
        if !moved {
            if let Some(reason) = self.mirrored_nothing_reason() {
                // Not a fault to keep in `last_error`: there is nothing here.
                return Err(format!("nothing to play: {reason}"));
            }
            if delta >= 0
                && self
                    .effective_playlist_ids()
                    .iter()
                    .any(|playlist| self.holds_refused(playlist))
            {
                // Nor is a playlist whose other entries were refused.
                return Err(REFUSED_REST.into());
            }
            return self.fail(if delta < 0 {
                "no previous playback history"
            } else {
                "active display playlists are empty or taboo"
            });
        }
        let result = self.apply_current_keeping(&resident);
        if result.is_ok() {
            self.last_cycle = now;
        }
        result
    }

    fn random_entry(&mut self) -> Result<String, String> {
        let resident = self.resident_targets();
        let mut moved = false;
        for playlist in self.effective_playlist_ids() {
            moved |= self.move_cursor_random(&playlist);
        }
        if !moved {
            if let Some(reason) = self.mirrored_nothing_reason() {
                return Err(format!("nothing to play: {reason}"));
            }
            if self
                .effective_playlist_ids()
                .iter()
                .any(|playlist| self.holds_refused(playlist))
            {
                return Err(REFUSED_REST.into());
            }
            return self.fail("active display playlists are empty or taboo");
        }
        let result = self.apply_current_keeping(&resident);
        if result.is_ok() {
            self.last_cycle = Instant::now();
        }
        result
    }

    fn reload(
        &mut self,
        at: NaiveDateTime,
        prepare_power: &mut impl FnMut(bool) -> PowerObservation,
    ) -> Result<String, String> {
        let next = Config::load(&self.config_path).map_err(|error| error.to_string())?;
        // Sample against this exact decoded candidate before any renderer
        // changes. Sampling a separate pre-read file could race atomic rename.
        self.power.observation = prepare_power(next.settings.stop_animations_on_battery);
        let mode_changed = next.settings.display_mode != self.config.settings.display_mode;
        let old_targets = self.current_targets(&self.target_outputs);
        let resident = self.resident_targets();
        let old_theme_source = self.effective_theme_source().map(str::to_owned);
        let old_theme_entry = old_theme_source.as_deref().and_then(|connector| {
            old_targets
                .iter()
                .find(|target| target.output == connector)
                .map(|target| target.entry.clone())
        });
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
        let (next_active, next_schedule_rule) = if let Some(manual) = &next_manual {
            (manual.clone(), None)
        } else {
            let scheduled =
                schedule::resolve_rule(&next.schedules, at).map_err(|error| error.to_string())?;
            (
                scheduled
                    .map_or(&next.default_playlist, |rule| &rule.playlist)
                    .to_string(),
                scheduled.map(|rule| rule.id.clone()),
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
        // Whether a web page may start is decided when its engine starts, so
        // a change of it starts every display again (`crate::workshop`).
        let web_policy_changed =
            next.settings.allow_web_wallpapers != self.config.settings.allow_web_wallpapers;
        let nondurable_findings = self.snapshot_nondurable_findings();

        // Adopt the candidate only in memory until every required driver change
        // succeeds. A valid TOML document can still be unplayable (for example,
        // media deleted after compilation), so a decoded config is not yet the
        // new last-known-good generation. An empty playlist is not that: below
        // schema 8 the load refuses it, and at 8 it plays nothing on purpose.
        let old_config = std::mem::replace(&mut self.config, next);
        let old_manual_playlist = std::mem::replace(&mut self.manual_playlist, next_manual);
        let old_active_playlist = std::mem::replace(&mut self.active_playlist, next_active);
        let old_schedule_rule = std::mem::replace(&mut self.schedule_rule, next_schedule_rule);
        let old_cursors = std::mem::take(&mut self.cursors);
        let snapshot = ReloadSnapshot {
            power_released_outputs: self.power_released_outputs.clone(),
            config: old_config,
            manual_playlist: old_manual_playlist,
            active_playlist: old_active_playlist,
            schedule_rule: old_schedule_rule,
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
            // Taken, not copied: the candidate is a new generation, whose
            // items are asked again. A rollback puts these back.
            refused_scenes: std::mem::take(&mut self.refused_scenes),
            routes: self.routes.clone(),
            route_generation: self.route_generation,
            current_time: self.current_time,
            config_epoch: self.config_epoch,
        };
        if mode_changed {
            // The session's Cycle override is kept: it holds until the service
            // exits or `cycle default`, whichever mode the routes are in.
            self.playback_state = PlaybackState::Playing;
            self.shuffle_override = None;
            if self.is_independent() {
                self.routes.clear();
            }
        }
        // The candidate epoch becomes visible only if this transaction reaches
        // the successful return. Rollback restores the snapshot's epoch.
        self.config_epoch = self.config_epoch.saturating_add(1);
        self.reconcile_configured_taboo(&nondurable_findings);
        if mode_changed || (displays_changed && self.live_outputs.is_none()) {
            self.target_outputs = if self.is_independent() {
                self.live_outputs.clone().unwrap_or_else(|| {
                    self.config
                        .displays
                        .iter()
                        .map(|display| display.connector.clone())
                        .collect()
                })
            } else if self.config.displays.is_empty() {
                vec![String::new()]
            } else {
                self.live_outputs.clone().unwrap_or_else(|| {
                    self.config
                        .displays
                        .iter()
                        .map(|display| display.connector.clone())
                        .collect()
                })
            };
            self.target_outputs.sort();
            self.target_outputs.dedup();
        }
        // Refused as every other reload refusal reads, with what kept going.
        if let Err(error) = self.rebuild_cursors(&old_entries) {
            self.restore_reload_snapshot(snapshot);
            return Err(format!(
                "reload rejected: {error}; previous configuration restored"
            ));
        }
        if self.is_independent()
            && let Err(error) = self.rebuild_independent_routes(&old_route_entries)
        {
            self.restore_reload_snapshot(snapshot);
            return Err(format!(
                "reload rejected: {error}; previous configuration restored"
            ));
        }

        let new_targets = self.current_targets(&self.target_outputs);
        let targets_changed = !targets_equal_ignoring_taboo(&old_targets, &new_targets);
        let changed_outputs = changed_target_outputs(&old_targets, &new_targets);
        let output_removed = old_targets
            .iter()
            .any(|old| !new_targets.iter().any(|new| new.output == old.output));
        let new_theme_source = self.effective_theme_source().map(str::to_owned);
        let residency_changed = snapshot.active_playlist != self.active_playlist
            || snapshot.manual_playlist != self.manual_playlist
            || snapshot.schedule_rule.is_some() != self.schedule_rule.is_some()
            || targets_changed;
        let independent_apply_outputs: HashSet<String> = if self.is_independent() {
            if mode_changed || renderer_changed || dynamics_changed || web_policy_changed {
                self.target_outputs.iter().cloned().collect()
            } else {
                changed_outputs
                    .into_iter()
                    .filter(|output| self.target_outputs.contains(output))
                    .collect()
            }
        } else {
            HashSet::new()
        };
        // A program the driver could not start may be there now (on Arch,
        // the package put it at the very path the document already named, so
        // the recompiled document is the same): the displays that show a
        // still for it are applied again. A renderer change applies every
        // display anyway, after a reconfigure that forgets every record.
        let recovered: Vec<String> = if renderer_changed {
            Vec::new()
        } else {
            self.driver.recheck_missing_renderers()
        };
        let mut independent_apply_outputs = independent_apply_outputs;
        if self.is_independent() {
            independent_apply_outputs.extend(
                recovered
                    .iter()
                    .filter(|output| self.target_outputs.contains(output))
                    .cloned(),
            );
        }
        let apply_needed = if self.is_independent() {
            !independent_apply_outputs.is_empty()
        } else {
            mode_changed
                || renderer_changed
                || dynamics_changed
                || web_policy_changed
                || targets_changed
                || !recovered.is_empty()
        };
        // Noctalia owns one shell-global palette. Changing only which display
        // supplies it must not tear down any otherwise unchanged renderer.
        // If that route is already part of a necessary hand-over, the normal
        // staged apply has applied its palette and no extra command is needed.
        let palette_only_needed = self.is_independent()
            && old_theme_source != new_theme_source
            && new_theme_source
                .as_ref()
                .is_some_and(|connector| !independent_apply_outputs.contains(connector));
        let mut apply_attempted = false;
        let mut palette_only_attempted = false;
        let mut failure = None;

        if renderer_changed {
            self.driver.reconfigure(self.config.renderer.clone());
        } else if video_audio_changed
            && let Err(error) = self.driver.set_video_audio(
                self.config.renderer.video_muted,
                self.config.renderer.video_volume,
            )
        {
            failure = Some(error);
        }
        if failure.is_none() && self.is_independent() && output_removed && !apply_needed {
            self.driver.retain_outputs(&self.target_outputs);
        }
        if failure.is_none() && apply_needed {
            apply_attempted = true;
            let applied = if self.is_independent() {
                self.apply_independent_selected(&independent_apply_outputs)
            } else if mode_changed
                || renderer_changed
                || dynamics_changed
                || web_policy_changed
                || !recovered.is_empty()
                || self.animations_inhibited() != self.applied_power_inhibition
            {
                self.apply_current()
            } else {
                // Only what some displays show changed: as independent
                // displays do, the others keep their renderers.
                self.apply_current_keeping(&resident)
            };
            if let Err(error) = applied {
                failure = Some(error);
            }
        }
        if failure.is_none() && palette_only_needed {
            palette_only_attempted = true;
            let applied = new_theme_source
                .as_deref()
                .and_then(|connector| {
                    self.current_targets(&self.target_outputs)
                        .into_iter()
                        .find(|target| target.output == connector)
                })
                .ok_or_else(|| "the selected theme-source display has no active entry".to_string())
                .and_then(|target| self.driver.apply_palette_only(&target.entry));
            if let Err(error) = applied {
                failure = Some(format!(
                    "could not apply the selected theme-source palette: {error}"
                ));
            }
        }

        if let Some(error) = failure {
            let candidate_palette_may_have_changed = palette_only_attempted
                || (apply_attempted
                    && self.is_independent()
                    && new_theme_source
                        .as_ref()
                        .is_some_and(|connector| independent_apply_outputs.contains(connector)));
            let previous_last_error = snapshot.last_error.clone();
            let previous_renderer_failed = snapshot.renderer_failed;
            self.restore_reload_snapshot(snapshot);

            let mut rollback_errors = Vec::new();
            if renderer_changed {
                self.driver.reconfigure(self.config.renderer.clone());
            } else if video_audio_changed
                && let Err(rollback) = self.driver.set_video_audio(
                    self.config.renderer.video_muted,
                    self.config.renderer.video_volume,
                )
            {
                rollback_errors.push(format!("could not restore video audio: {rollback}"));
            }
            let scoped_independent_rollback =
                apply_attempted && !mode_changed && self.is_independent();
            let mut wallpaper_rollback_failed = false;
            if apply_attempted {
                let rollback = if scoped_independent_rollback {
                    self.apply_independent_selected(&independent_apply_outputs)
                } else {
                    self.apply_current()
                };
                if let Err(rollback) = rollback {
                    wallpaper_rollback_failed = true;
                    rollback_errors.push(format!("could not restore wallpaper: {rollback}"));
                }
            }
            let wallpaper_rollback_restored_palette = apply_attempted
                && !wallpaper_rollback_failed
                && (!scoped_independent_rollback
                    || old_theme_source
                        .as_ref()
                        .is_some_and(|connector| independent_apply_outputs.contains(connector)));
            if candidate_palette_may_have_changed && !wallpaper_rollback_restored_palette {
                match old_theme_entry.as_ref() {
                    Some(entry) => {
                        if let Err(rollback) = self.driver.apply_palette_only(entry) {
                            rollback_errors
                                .push(format!("could not restore previous palette: {rollback}"));
                        }
                    }
                    None => rollback_errors.push(
                        "could not restore previous palette: previous theme source is missing"
                            .into(),
                    ),
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
                let current = keep_entries
                    .get(&playlist.id)
                    .and_then(|wanted| {
                        playlist
                            .entries
                            .iter()
                            .position(|entry| &entry.id == wanted)
                    })
                    .unwrap_or(0);
                (
                    playlist.id.clone(),
                    playlist.entries.len(),
                    current,
                    self.mirrored_shuffle(&playlist.id),
                )
            })
            .collect();
        let mut cursors = HashMap::new();
        for (id, entries, current, shuffle) in specifications {
            let (order, position) = if shuffle && entries > 0 {
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

    fn reset_cursor_automatic(&mut self, reference: &str) -> bool {
        let Some(playlist) = self.config.playlist(reference) else {
            return false;
        };
        let playlist_id = playlist.id.clone();
        let eligible: HashSet<usize> = playlist
            .entries
            .iter()
            .enumerate()
            .filter_map(|(index, entry)| (!self.cant_play(&playlist_id, entry)).then_some(index))
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

    fn move_cursor_forward(&mut self, reference: &str) -> bool {
        let Some(playlist) = self.config.playlist(reference) else {
            return false;
        };
        let playlist_id = playlist.id.clone();
        let entry_count = playlist.entries.len();
        let usable: Vec<bool> = playlist
            .entries
            .iter()
            .map(|entry| !self.cant_play(&playlist_id, entry))
            .collect();
        let shuffle_enabled = self.mirrored_shuffle(&playlist_id);
        let eligible = |index: usize| usable.get(index).copied().unwrap_or(false);
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

            let mut next: Vec<usize> = (0..entry_count).filter(|index| eligible(*index)).collect();
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
        let playlist_id = playlist.id.clone();
        let eligible: Vec<bool> = playlist
            .entries
            .iter()
            .map(|entry| !self.cant_play(&playlist_id, entry))
            .collect();
        let Some(cursor) = self.cursors.get_mut(&playlist_id) else {
            return false;
        };
        let Some(&current) = cursor.order.get(cursor.position) else {
            return false;
        };
        while let Some(previous) = cursor.history.pop() {
            if !eligible.get(previous).copied().unwrap_or(false) || previous == current {
                continue;
            }
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
            return true;
        }
        false
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
            .filter_map(|(index, entry)| (!self.cant_play(&playlist_id, entry)).then_some(index))
            .collect();
        let shuffle = self.mirrored_shuffle(&playlist_id);
        let Some(cursor) = self.cursors.get_mut(&playlist_id) else {
            return false;
        };
        let Some(&current) = cursor.order.get(cursor.position) else {
            return false;
        };
        if shuffle {
            let remaining_positions: Vec<usize> = ((cursor.position + 1)..cursor.order.len())
                .filter(|position| eligible.contains(&cursor.order[*position]))
                .collect();
            if remaining_positions.is_empty() {
                let mut next: Vec<usize> = eligible;
                if next.is_empty() {
                    return false;
                }
                self.rng.shuffle(&mut next);
                if next[0] == current {
                    if next.len() == 1 {
                        return false;
                    }
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
        // Each as its displays play it: a stand-in for one the service
        // refused throughout (`stand_in`).
        if self.config.displays.is_empty()
            || self.manual_playlist.is_some()
            || self.schedule_rule.is_some()
        {
            return vec![self.shown_playlist_id(&self.active_playlist)];
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
                let shown = self.shown_playlist_id(&playlist.id);
                if seen.insert(shown.clone()) {
                    Some(shown)
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

    /// The moment one status reply describes at the local reading `at`: the
    /// monotonic instant cycle deadlines are measured from (the real clock, or
    /// a later instant `tick` was handed), the real UTC time they are
    /// projected from, and the local clock's readings the schedule will see.
    fn status_moment(&self, at: NaiveDateTime) -> StatusMoment {
        let utc = Utc::now();
        StatusMoment {
            instant: self.observed_instant.max(Instant::now()),
            utc,
            timeline: schedule::Timeline::from_reading(at, utc, &Local),
        }
    }

    fn playlist_id(&self, reference: &str) -> Option<&str> {
        self.config
            .playlist(reference)
            .map(|playlist| playlist.id.as_str())
    }

    /// The automatic route a mirrored row reports at `at`: the playlist and
    /// `route_source` its status would show, ignoring a manual pick. The
    /// all-output row has no assignment and falls back to the default.
    fn mirrored_route_key(
        &self,
        explicit: Option<&DisplayAssignment>,
        at: NaiveDateTime,
    ) -> Result<(Option<&str>, &'static str), String> {
        let rule = schedule::resolve_rule(&self.config.schedules, at)
            .map_err(|error| error.to_string())?;
        Ok(match (rule, explicit) {
            (Some(rule), _) => (self.playlist_id(&rule.playlist), "schedule"),
            (None, Some(assignment)) => (self.playlist_id(&assignment.playlist), "assignment"),
            (None, None) => (self.playlist_id(&self.config.default_playlist), "default"),
        })
    }

    /// One mirrored row's timing. Mirrored mode has one rotation timer, and
    /// its schedule is not consulted while a manual pick holds.
    fn mirrored_timing(
        &self,
        explicit: Option<&DisplayAssignment>,
        connected: bool,
        moment: &StatusMoment,
        budget: &mut usize,
    ) -> DisplayTiming {
        if !connected {
            return DisplayTiming::default();
        }
        let route_change = if self.manual_playlist.is_some() {
            None
        } else {
            schedule::next_change(
                &self.config.schedules,
                &moment.timeline,
                budget,
                |instant| self.mirrored_route_key(explicit, instant),
            )
            .ok()
            .flatten()
        };
        let cycle =
            (self.playback_state != PlaybackState::Paused && self.cycle_enabled()).then(|| {
                cycle_remaining(
                    self.last_cycle,
                    self.mirrored_cycle_interval_seconds(),
                    moment.instant,
                )
            });
        DisplayTiming::new(route_change, cycle, moment.utc, &Local)
    }

    /// One independent route's timing: its own calendar winner (with the
    /// display's opt-in, through `automatic_route`) and its own timer.
    fn route_timing(
        &self,
        connector: &str,
        route: &DisplayRoute,
        connected: bool,
        moment: &StatusMoment,
        budget: &mut usize,
    ) -> DisplayTiming {
        if !connected {
            return DisplayTiming::default();
        }
        let route_change = if route.manual_playlist.is_some() {
            None
        } else {
            schedule::next_change(
                &self.config.schedules,
                &moment.timeline,
                budget,
                |instant| {
                    self.automatic_route(connector, instant)
                        .map(|(playlist, source, _rule)| (self.playlist_id(&playlist), source))
                },
            )
            .ok()
            .flatten()
        };
        let cycle = (route.playback_state != PlaybackState::Paused
            && route.cycle_enabled(self.config.settings.cycle_enabled))
        .then(|| {
            cycle_remaining(
                route.last_cycle,
                self.playlist_cycle_interval_seconds(&route.active_playlist),
                moment.instant,
            )
        });
        DisplayTiming::new(route_change, cycle, moment.utc, &Local)
    }

    /// The status, within the reply's size limit.
    ///
    /// The configuration's status budget bounds everything a status repeats
    /// from it except the taboo records' own text, which a document at the
    /// schema's ceilings can push over the limit. Then the oldest records are
    /// left out -- as many as needed, and counted in `taboo_entries_omitted`,
    /// as the 64-record cap already does -- instead of the reply failing.
    /// `entry_taboo` stays exact.
    ///
    /// What the budget doesn't bound at all is fitted here, and goes first.
    /// The fields added in 0.4.1 give way before anything a 0.4.0 reply
    /// carries, so the budget stays 0.4.0's and a document that loaded then
    /// still loads (docs/runtime-config.md). In order:
    ///
    /// 0. those of them that say nothing (`ReplyShape::lean`): with them
    ///    gone, a reply carrying what 0.4.0's carried is no larger than
    ///    0.4.0's, given a display row (review r13e L6);
    /// 1. `refused` rows, the last listed first, each counted in
    ///    `refused_omitted`;
    /// 2. every `stand_in_for`, on the rows and in the summary, which
    ///    `stand_in_for_omitted` then says (its bytes are part of every reply
    ///    measured without them);
    /// 3. the compositor's display snapshot (`outputs`);
    /// 4. `missing_renderers`' paths, then its rows (the renderers the
    ///    service could not run);
    /// 5. the oldest taboo records.
    ///
    /// None of the first four is ever kept at a taboo record's expense. With
    /// all of them gone, what is left (the floor) is what the budget bounds,
    /// apart from what it doesn't count on the rows: when even the floor
    /// wouldn't fit, `abridged` leaves out as little of that as it then
    /// needs (6 and 7 there). Whatever room the reply then has is given back
    /// in the reverse order, as 0.4.0 gave its room back to
    /// `missing_renderers`: taboo records, `missing_renderers` whole or
    /// without its paths, `outputs`, the stand-ins, `refused` rows.
    ///
    /// Each count and each piece given back is found by measuring it, not
    /// by building the reply again for each one tried (review r13d N8): a
    /// reply at the limit is built at most nine times (`everything`, without
    /// the additions, without `outputs`, the floor, the floor abridged, its
    /// taboo records, with what is given back to 4 and 3, with the
    /// stand-ins, its `refused` rows), six with nothing refused; beside that
    /// each taboo record, `refused` row, `missing_renderers` and `outputs` is
    /// serialized once on its own. A lean reply that fits whole is built
    /// once more, and returned. Below the limit, once. The reply chosen is
    /// always built and checked: a measure that were ever short would cost
    /// a build, never the limit.
    fn status_reply(&self, at: NaiveDateTime) -> Result<String, String> {
        let (everything, measured) = self.status_json_measured(at, ReplyShape::WHOLE)?;
        if success_fits(&everything) {
            return Ok(everything);
        }
        // 0: the lean reply is built only when it fits.
        if success_excess(&everything) <= measured.lean_saving {
            let lean = self.status_json(
                at,
                ReplyShape {
                    lean: true,
                    ..ReplyShape::WHOLE
                },
            )?;
            if success_fits(&lean) {
                return Ok(lean);
            }
        }
        // Nothing refused, nothing to add: no row stands in (a stand-in
        // needs a refused entry), and `refused` is empty.
        let additions = !self.refused_scenes.is_empty();
        let (shape, best) = self.fitting_without_additions(at, additions)?;
        if !success_fits(&best) || !additions {
            return Ok(best);
        }
        // What room is left: every `stand_in_for`, then as many `refused`
        // rows as fit, beside the stand-ins or, when they don't fit, without
        // them (review r13e L6: the rows were left out with 109 KB to spare).
        let with_stand_ins = ReplyShape {
            stand_in_for: true,
            ..shape
        };
        let candidate = self.status_json(at, with_stand_ins)?;
        let (shape, best) = if success_fits(&candidate) {
            (with_stand_ins, candidate)
        } else {
            (shape, best)
        };
        let rows = self.refused_rows(MAX_REFUSED_STATUS_ENTRIES);
        let (_, best) = self.most_listed_that_fit(
            at,
            best,
            &rows,
            self.refused_scenes.len(),
            shape.lean.then_some(["refused", "refused_omitted"]),
            |refused| ReplyShape { refused, ..shape },
        )?;
        Ok(best)
    }

    /// The reply 0.4.0 would fit, lean and without `refused` rows or
    /// `stand_in_for`: `outputs` left out first, then as many taboo records
    /// kept as fit without `missing_renderers`, which then gets what room
    /// is left (whole, without its paths, or not at all), and `outputs`
    /// what room is left after that. When not even the floor fits, the
    /// floor abridged (`abridged`) takes the place of the floor. Its shape,
    /// and that reply; one that doesn't fit only when not even the
    /// abridged floor does. `with_additions` is false when the lean
    /// `everything`, which didn't fit, was already this reply with
    /// `outputs`.
    fn fitting_without_additions(
        &self,
        at: NaiveDateTime,
        with_additions: bool,
    ) -> Result<(ReplyShape, String), String> {
        let mut shape = ReplyShape {
            lean: true,
            refused: 0,
            stand_in_for: false,
            ..ReplyShape::WHOLE
        };
        if with_additions {
            let everything = self.status_json(at, shape)?;
            if success_fits(&everything) {
                return Ok((shape, everything));
            }
        }
        shape.outputs = false;
        let full = self.status_json(at, shape)?;
        if success_fits(&full) {
            return Ok((shape, full));
        }
        shape.taboo = 0;
        shape.missing = MissingDetail::Left;
        let (mut floor, measured) = self.status_json_measured(at, shape)?;
        if !success_fits(&floor) {
            (shape.rows, floor) = self.abridged(at, shape, &floor, &measured.units)?;
            if !success_fits(&floor) {
                return Ok((shape, floor));
            }
        }
        let records = self.taboo_rows(MAX_TABOO_STATUS_ENTRIES);
        let (records, best) =
            self.most_listed_that_fit(at, floor, &records, self.taboo.len(), None, |taboo| {
                ReplyShape { taboo, ..shape }
            })?;
        shape.taboo = records;
        // 4, then 3, given back, measured: each is one array of the reply,
        // `[]` in it until then, so it adds its own JSON less those 2 bytes.
        let mut room = success_room(&best).unwrap_or(0);
        let mut given = shape;
        let mut added = 0;
        for detail in [MissingDetail::Full, MissingDetail::WithoutPaths] {
            let cost = wire_len(&self.missing_rows(detail)).saturating_sub(2);
            if let Some(left) = room.checked_sub(cost) {
                (given.missing, room, added) = (detail, left, cost);
                break;
            }
        }
        let cost = wire_len(self.driver.published_outputs()).saturating_sub(2);
        if cost <= room {
            (given.outputs, added) = (true, added + cost);
        }
        if added == 0 {
            // Nothing given back takes a byte: the same reply.
            return Ok((given, best));
        }
        let candidate = self.status_json(at, given)?;
        if success_fits(&candidate) {
            Ok((given, candidate))
        } else {
            Ok((shape, best))
        }
    }

    /// The floor (`shape`, `floor`, which doesn't fit) with as little left
    /// out as it then needs of what the status budget doesn't count on the
    /// rows (reviews r13d L5 and r13e L6), measured from the floor's own
    /// rows (`units`):
    ///
    /// 6. rows' `schedule_rule_id`s, left null (a rule's id is up to 256
    ///    bytes, on as many as 128 rows; `schedule_rule_id_omitted`), and
    ///    rows' `last_error`s, left empty (`diagnostics_omitted`), together:
    ///    the longest first, an error before an id as long, another row's
    ///    before a connected row's;
    /// 7. then the summary's own: `output_discovery_error`, then its
    ///    `last_error`, also left empty.
    ///
    /// Measured with every one of them out, each is then given back, the
    /// last first, wherever it still fits: so as few go as the room needs,
    /// and a short id stays where a longer error will do (review r13e N6:
    /// 128 one-byte ids went for 128 bytes, beside 110 KB to spare). Both
    /// flags' bytes are counted until their kind is given back whole, and
    /// a kind's flag pays for its own last units: what is still out of a
    /// kind comes back whole where it fits beside the room its flag then
    /// gives up (review r13f N12: one 565-byte id stayed out beside 548
    /// bytes and its 34-byte flag). The rows' detail chosen, and that
    /// reply; with every unit left out when even that is ever over.
    ///
    /// With all of them gone what is left is the budget's: its configured
    /// text, at most four times over on the wire as the budget counts it,
    /// the fixed keys and the structure of at most 578 playlists, 512 rules
    /// and 128 rows; and what neither budget counts that every reply
    /// carries (review r13e N7): the rows' timing and retry fields, the top
    /// level `schedule`'s second copy of a playlist's id and name and of a
    /// rule's id, `theme_source`'s two connectors, `config_path`, and
    /// `runtime_executable`, which alone may hold control characters (up
    /// to about 28.6 KB on the wire). At those ceilings, with every one of
    /// them at its longest on the wire at once, that reply still has about
    /// 13 KB to spare (16 KB independent; docs/runtime-config.md).
    fn abridged(
        &self,
        at: NaiveDateTime,
        shape: ReplyShape,
        floor: &str,
        units: &RowUnits,
    ) -> Result<(RowDetail, String), String> {
        // The summary's units: `output_discovery_error`, then `last_error`.
        let summary = [
            self.output_discovery_error.as_str(),
            self.last_error.as_str(),
        ]
        .map(|text| (!text.is_empty()).then(|| unit_len(text)));
        let rule_total: usize = units.rule_ids.iter().flatten().sum();
        let error_total: usize = units
            .errors
            .iter()
            .flatten()
            .chain(summary.iter().flatten())
            .sum();
        let rule_flag = if rule_total > 0 {
            flag_len("schedule_rule_id_omitted")
        } else {
            0
        };
        let error_flag = if error_total > 0 {
            flag_len("diagnostics_omitted")
        } else {
            0
        };
        // The room left with every unit out, which sets both flags.
        let room =
            (rule_total + error_total).checked_sub(success_excess(floor) + rule_flag + error_flag);
        if let Some(mut room) = room {
            let mut rows = RowDetail::NONE;
            if room + rule_flag + error_flag >= rule_total + error_total {
                rows = RowDetail::WHOLE;
            } else {
                // Where a unit is: the summary's (0 `output_discovery_error`,
                // 1 `last_error`), or a row's of a group (0 the connected).
                #[derive(Clone, Copy)]
                enum Place {
                    Summary(usize),
                    Row(usize),
                }
                // Each unit as its cost, its kind (0 an id, 1 a diagnostic)
                // and its place, in the order given back: the summary's own
                // first (`last_error`, then `output_discovery_error`), then
                // the rows' ids and errors together, the shortest first (a
                // connected row's before another's, an id before an error
                // as long), each group's kept in its own order.
                let mut order: Vec<(usize, usize, usize, usize)> = Vec::new();
                for group in 0..2 {
                    for (kind, costs) in [&units.rule_ids[group], &units.errors[group]]
                        .into_iter()
                        .enumerate()
                    {
                        order.extend(
                            costs
                                .iter()
                                .enumerate()
                                .map(|(place, cost)| (*cost, group, kind, place)),
                        );
                    }
                }
                order.sort_unstable();
                let given: Vec<(usize, usize, Place)> = [1, 0]
                    .into_iter()
                    .filter_map(|unit| summary[unit].map(|cost| (cost, 1, Place::Summary(unit))))
                    .chain(
                        order
                            .into_iter()
                            .map(|(cost, group, kind, _)| (cost, kind, Place::Row(group))),
                    )
                    .collect();
                let mut back = vec![false; given.len()];
                // What each kind still has out, and its flag's bytes while
                // it does.
                let mut out = [rule_total, error_total];
                let mut flags = [rule_flag, error_flag];
                loop {
                    let mut changed = false;
                    for index in 0..given.len() {
                        if back[index] {
                            continue;
                        }
                        let (cost, kind, _) = given[index];
                        if let Some(left) = room.checked_sub(cost) {
                            (back[index], room, changed) = (true, left, true);
                            out[kind] -= cost;
                        } else if let Some(left) = (room + flags[kind]).checked_sub(out[kind]) {
                            // It doesn't fit on its own, but what is still
                            // out of its kind does, beside its flag's bytes:
                            // the kind comes back whole, and its flag goes
                            // (review r13f N12).
                            for (other, (_, other_kind, _)) in given.iter().enumerate() {
                                back[other] |= *other_kind == kind;
                            }
                            (room, out[kind], flags[kind], changed) = (left, 0, 0, true);
                        }
                    }
                    // A kind given back whole, unit by unit, gives its flag's
                    // bytes back too.
                    for kind in 0..2 {
                        if out[kind] == 0 && flags[kind] > 0 {
                            room += std::mem::take(&mut flags[kind]);
                            changed = true;
                        }
                    }
                    if !changed {
                        break;
                    }
                }
                for (&(_, kind, place), _) in given.iter().zip(&back).filter(|(_, back)| **back) {
                    match place {
                        Place::Summary(0) => rows.discovery_error = true,
                        Place::Summary(_) => rows.last_error = true,
                        Place::Row(group) if kind == 0 => rows.rule_ids[group] += 1,
                        Place::Row(group) => rows.errors[group] += 1,
                    }
                }
            }
            let candidate = self.status_json(at, ReplyShape { rows, ..shape })?;
            if success_fits(&candidate) {
                return Ok((rows, candidate));
            }
        }
        let least = self.status_json(
            at,
            ReplyShape {
                rows: RowDetail::NONE,
                ..shape
            },
        )?;
        Ok((RowDetail::NONE, least))
    }

    /// The most of `rows` (each one element of one of the reply's arrays,
    /// in the order it lists them) that fit beside `zero`, the reply with
    /// none of them, which fits; a reply shaped `shape(count)` lists the
    /// first `count`, and counts the other `total - count` in a number of
    /// its own. That count, and that reply.
    ///
    /// Measured, not tried: the message is compact JSON, so listing a row
    /// adds its own JSON and, after the first, a comma, and the count of
    /// those left out gets shorter by however many digits it loses. Every
    /// one of those is escaped once more on the wire, character by
    /// character. `lean_keys` names the array and the count when the reply
    /// is lean, which leaves the array out while it is empty and the count
    /// while it is 0 (`ReplyShape::lean`): then the first row brings its
    /// array's key, and listing the last takes its count's away. The reply
    /// chosen is built and checked all the same, and one fewer is tried if
    /// it were ever over.
    fn most_listed_that_fit<T: Serialize>(
        &self,
        at: NaiveDateTime,
        zero: String,
        rows: &[T],
        total: usize,
        lean_keys: Option<[&str; 2]>,
        shape: impl Fn(usize) -> ReplyShape,
    ) -> Result<(usize, String), String> {
        let room = success_room(&zero).unwrap_or(0);
        let opening = lean_keys.map_or(0, |[array, _]| absent_len(array, "[]"));
        let mut added = 0_usize;
        let mut most = 0;
        for (index, row) in rows.iter().enumerate() {
            let row = serde_json::to_string(row).map_err(|error| error.to_string())?;
            added += escaped_len(&row) + if index > 0 { 1 } else { opening };
            let listed = index + 1;
            let left = total.saturating_sub(listed);
            let saved = match lean_keys {
                Some([_, count]) if left == 0 => absent_len(count, &total.to_string()),
                _ => decimal_digits(total) - decimal_digits(left),
            };
            if added.saturating_sub(saved) <= room {
                most = listed;
            }
        }
        while most > 0 {
            let candidate = self.status_json(at, shape(most))?;
            if success_fits(&candidate) {
                return Ok((most, candidate));
            }
            most -= 1;
        }
        Ok((0, zero))
    }

    /// The taboo records status lists, newest first: at most `limit`.
    fn taboo_rows(&self, limit: usize) -> Vec<TabooStatus<'_>> {
        self.taboo_order
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
            .take(limit.min(MAX_TABOO_STATUS_ENTRIES))
            .collect()
    }

    /// What the service refused this generation, as status lists it: at
    /// most `limit` rows.
    fn refused_rows(&self, limit: usize) -> Vec<RefusedStatus<'_>> {
        self.refused_scenes
            .iter()
            .take(limit.min(MAX_REFUSED_STATUS_ENTRIES))
            .map(|(item, refusal)| RefusedStatus {
                scene_id: item,
                web_page: refusal.web_page,
                reason: truncate_middle(&refusal.reason, MAX_REFUSED_REASON_BYTES),
            })
            .collect()
    }

    /// How many more were refused than `refused_rows(limit)` lists.
    fn refused_omitted(&self, limit: usize) -> usize {
        self.refused_scenes
            .len()
            .saturating_sub(limit.min(MAX_REFUSED_STATUS_ENTRIES))
    }

    /// `missing_renderers` as a reply of shape `detail` carries it.
    fn missing_rows(&self, detail: MissingDetail) -> Vec<MissingRendererStatus> {
        if detail == MissingDetail::Left {
            return Vec::new();
        }
        self.driver
            .missing_renderers()
            .iter()
            .map(|record| MissingRendererStatus::of(record, detail))
            .collect()
    }

    fn status_json(&self, at: NaiveDateTime, shape: ReplyShape) -> Result<String, String> {
        self.status_json_measured(at, shape).map(|(json, _)| json)
    }

    /// The status of `shape`, and what was measured building it
    /// (`Measured`): its rows' units and the lean saving.
    fn status_json_measured(
        &self,
        at: NaiveDateTime,
        shape: ReplyShape,
    ) -> Result<(String, Measured), String> {
        if self.is_independent() {
            return self.status_json_independent(at, shape);
        }
        let moment = self.status_moment(at);
        let mut budget = ROUTE_CHANGE_RULE_CHECKS;
        // What plays: the playlist in force, or its stand-in (`stand_in`).
        let in_force = self.playlist()?;
        let active_playlist = self.stand_in(&in_force.id).unwrap_or(in_force);
        let effective_ids = self.effective_playlist_ids();
        let summary_playlist = if self.manual_playlist.is_some()
            || self.schedule_rule.is_some()
            || self.config.displays.is_empty()
        {
            Some(active_playlist)
        } else if effective_ids.len() == 1 {
            self.config.playlist(&effective_ids[0])
        } else {
            None
        };
        let entry = summary_playlist
            .filter(|playlist| self.nothing_reason(&playlist.id).is_none())
            .and_then(|playlist| self.current_entry_for(&playlist.id));
        let kind = entry.map(|entry| entry_kind(entry.kind));
        let active_ids: HashSet<_> = effective_ids.into_iter().collect();
        let taboo_total = self.taboo.len();
        let taboo_entries = self.taboo_rows(shape.taboo);
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
        let mut entry_taboo = false;
        if self.config.displays.is_empty() {
            let nothing = entry
                .is_none()
                .then(|| self.nothing_reason(&active_playlist.id))
                .flatten();
            if entry.is_some() || nothing.is_some() {
                let display_entry_taboo =
                    entry.is_some_and(|entry| self.is_taboo(&active_playlist.id, &entry.id));
                entry_taboo |= display_entry_taboo;
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
                    stand_in_for: self.stood_in_for(&in_force.id, &active_playlist.id),
                    entry_id: entry.map_or("", |entry| entry.id.as_str()),
                    entry_taboo: display_entry_taboo,
                    kind: entry.map_or("", |entry| entry_kind(entry.kind)),
                    still: entry
                        .map_or_else(String::new, |entry| entry.still.display().to_string()),
                    motion_active: self.driver.motion_active(""),
                    route_source: if self.manual_playlist.is_some() {
                        "manual"
                    } else if self.schedule_rule.is_some() {
                        "schedule"
                    } else {
                        "default"
                    },
                    manual_override: self.manual_playlist.is_some(),
                    schedule_rule_id: self.schedule_rule.as_deref(),
                    playback_state: playback_state(self.playback_state),
                    paused: self.playback_state == PlaybackState::Paused,
                    stopped: self.playback_state == PlaybackState::Stopped,
                    shuffle: self.mirrored_shuffle(&active_playlist.id),
                    shuffle_default: self.playlist_shuffle_default(&active_playlist.id),
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
                    cycle_interval_seconds: self.mirrored_cycle_interval_seconds(),
                    beats_global_rules: false,
                    renderer_failed: self.renderer_failed,
                    last_error: truncate_middle(&self.last_error, MAX_DISPLAY_ERROR_BYTES),
                    automatic_retry: self.pending_automatic.as_ref().map(|pending| {
                        AutomaticRetryStatus {
                            attempt: pending.attempts,
                            maximum_attempts: AUTOMATIC_APPLY_ATTEMPTS,
                            reason: pending.reason,
                        }
                    }),
                    nothing_to_play: Some(nothing),
                    entry_name: Some(nothing_name(nothing)),
                    timing: {
                        let timing = self.mirrored_timing(None, true, &moment, &mut budget);
                        if nothing.is_some() {
                            timing.without_rotation()
                        } else {
                            timing
                        }
                    },
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
                let reference = if self.manual_playlist.is_some() || self.schedule_rule.is_some() {
                    &self.active_playlist
                } else {
                    assigned_reference
                };
                let decided = self
                    .config
                    .playlist(reference)
                    .ok_or_else(|| format!("display {output} names a missing playlist"))?;
                let effective = self.stand_in(&decided.id).unwrap_or(decided);
                let nothing = self.nothing_reason(&effective.id);
                let entry = nothing
                    .is_none()
                    .then(|| self.current_entry_for(&effective.id))
                    .flatten();
                if entry.is_some() || nothing.is_some() {
                    let display_entry_taboo =
                        entry.is_some_and(|entry| self.is_taboo(&effective.id, &entry.id));
                    if self.target_outputs.iter().any(|target| target == output) {
                        entry_taboo |= display_entry_taboo;
                    }
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
                        stand_in_for: self.stood_in_for(&decided.id, &effective.id),
                        entry_id: entry.map_or("", |entry| entry.id.as_str()),
                        entry_taboo: display_entry_taboo,
                        kind: entry.map_or("", |entry| entry_kind(entry.kind)),
                        still: entry
                            .map_or_else(String::new, |entry| entry.still.display().to_string()),
                        motion_active: self.target_outputs.iter().any(|target| target == output)
                            && self.driver.motion_active(output),
                        route_source: if self.manual_playlist.is_some() {
                            "manual"
                        } else if self.schedule_rule.is_some() {
                            "schedule"
                        } else if explicit.is_some() {
                            "assignment"
                        } else {
                            "default"
                        },
                        manual_override: self.manual_playlist.is_some(),
                        schedule_rule_id: self.schedule_rule.as_deref(),
                        playback_state: playback_state(self.playback_state),
                        paused: self.playback_state == PlaybackState::Paused,
                        stopped: self.playback_state == PlaybackState::Stopped,
                        shuffle: self.mirrored_shuffle(&effective.id),
                        shuffle_default: self.playlist_shuffle_default(&effective.id),
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
                        cycle_interval_seconds: self.mirrored_cycle_interval_seconds(),
                        beats_global_rules: false,
                        renderer_failed: self.renderer_failed,
                        last_error: truncate_middle(&self.last_error, MAX_DISPLAY_ERROR_BYTES),
                        automatic_retry: self.pending_automatic.as_ref().map(|pending| {
                            AutomaticRetryStatus {
                                attempt: pending.attempts,
                                maximum_attempts: AUTOMATIC_APPLY_ATTEMPTS,
                                reason: pending.reason,
                            }
                        }),
                        nothing_to_play: Some(nothing),
                        entry_name: Some(nothing_name(nothing)),
                        timing: {
                            let timing = self.mirrored_timing(
                                explicit,
                                self.target_outputs.iter().any(|target| target == output),
                                &moment,
                                &mut budget,
                            );
                            if nothing.is_some() {
                                timing.without_rotation()
                            } else {
                                timing
                            }
                        },
                    });
                }
            }
        }
        let (omitted, mut measured) = abridge_rows(
            &mut displays,
            shape,
            [&self.output_discovery_error, &self.last_error],
        );
        let quiet = LeanSummary::of(
            shape,
            &self.config,
            self.mirrored_nothing_reason(),
            self.refused_rows(shape.refused),
            self.refused_omitted(shape.refused),
        );
        measured.lean_saving += quiet.lean_saving;
        let json = serde_json::to_string(&Status {
            loaded_config_sha256: self.config.source_sha256.as_deref(),
            supported_config_schemas: SUPPORTED_SCHEMA_VERSIONS,
            supported_override_schemas: SUPPORTED_OVERRIDE_SCHEMAS,
            loaded_overrides_sha256: quiet.loaded_overrides_sha256,
            overrides_ignored: quiet.overrides_ignored,
            runtime_version: env!("CARGO_PKG_VERSION"),
            runtime_executable: self.runtime_executable.as_deref(),
            install_kind: crate::INSTALL_KIND,
            power_source: self.power.observation.source.name(),
            power_available: self.power.observation.source != PowerSource::Unknown,
            stop_animations_on_battery: self.battery_policy_enabled(),
            animations_inhibited: self.animations_inhibited(),
            animation_inhibition_reason: self.power.reason(self.battery_policy_enabled()),
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
            stand_in_for: summary_stand_in(&displays, summary_playlist),
            source: if self.manual_playlist.is_some() {
                "manual"
            } else {
                "schedule"
            },
            entry_id: entry.map(|entry| entry.id.as_str()),
            entry_taboo,
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
            shuffle_default: self.mirrored_shuffle_default(),
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
            cycle_interval_seconds: Some(self.mirrored_cycle_interval_seconds()),
            last_error: if shape.rows.last_error {
                &self.last_error
            } else {
                ""
            },
            output_discovery_error: if shape.rows.discovery_error {
                &self.output_discovery_error
            } else {
                ""
            },
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
            outputs: if shape.outputs {
                self.driver.published_outputs()
            } else {
                &[]
            },
            missing_renderers: self.missing_rows(shape.missing),
            nothing_to_play: quiet.nothing_to_play,
            allow_web_wallpapers: quiet.allow_web_wallpapers,
            refused: quiet.refused,
            refused_omitted: quiet.refused_omitted,
            stand_in_for_omitted: omitted.stand_in_for,
            schedule_rule_id_omitted: omitted.schedule_rule_id,
            diagnostics_omitted: omitted.diagnostics,
        })
        .map_err(|error| error.to_string())?;
        Ok((json, measured))
    }

    fn status_json_independent(
        &self,
        at: NaiveDateTime,
        shape: ReplyShape,
    ) -> Result<(String, Measured), String> {
        let moment = self.status_moment(at);
        let mut budget = ROUTE_CHANGE_RULE_CHECKS;
        let live_targets = self.current_targets(&self.target_outputs);
        // Connected routes that play nothing (schema 8) have no target.
        let live_nothing: Vec<&str> = self
            .target_outputs
            .iter()
            .filter(|connector| self.route_nothing_reason(connector).is_some())
            .filter_map(|connector| self.routes.get(connector))
            .filter_map(|route| self.config.playlist(&route.active_playlist))
            .map(|playlist| playlist.id.as_str())
            .collect();
        if live_targets.is_empty() && live_nothing.is_empty() {
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
        let live_playlists: Vec<&str> = live_targets
            .iter()
            .map(|target| target.playlist_id.as_str())
            .chain(live_nothing.iter().copied())
            .collect();
        let summary_playlist = live_playlists
            .first()
            .filter(|first| live_playlists.iter().all(|playlist| playlist == *first))
            .and_then(|playlist| self.config.playlist(playlist));
        let summary_entry = live_targets.first().filter(|first| {
            live_nothing.is_empty()
                && live_targets.iter().all(|target| {
                    target.playlist_id == first.playlist_id && target.entry.id == first.entry.id
                })
        });
        let active_ids: HashSet<&str> = live_playlists.iter().copied().collect();
        let entry_taboo = live_targets
            .iter()
            .any(|target| self.is_taboo(&target.playlist_id, &target.entry.id));
        let taboo_total = self.taboo.len();
        let taboo_entries = self.taboo_rows(shape.taboo);
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
                let (playlist, _source, rule) = self.automatic_route(connector, at)?;
                Ok((playlist, rule))
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
        for output in &inventory_outputs {
            let entry = targets
                .iter()
                .find(|target| &target.output == output)
                .map(|target| &target.entry);
            let nothing = entry
                .is_none()
                .then(|| self.route_nothing_reason(output))
                .flatten();
            if entry.is_none() && nothing.is_none() {
                continue;
            }
            let route = self
                .routes
                .get(output)
                .ok_or_else(|| format!("display route {output:?} is missing"))?;
            let effective = self
                .config
                .playlist(&route.active_playlist)
                .ok_or_else(|| format!("display {output} names a missing playlist"))?;
            let explicit = self
                .config
                .displays
                .iter()
                .find(|assignment| &assignment.connector == output);
            let assigned = self
                .config
                .playlist(
                    explicit.map_or(self.config.default_playlist.as_str(), |assignment| {
                        assignment.playlist.as_str()
                    }),
                )
                .ok_or_else(|| format!("display {output} assignment is missing"))?;
            displays.push(DisplayStatus {
                connector: output,
                connected: self.target_outputs.contains(output),
                assignment_source: if explicit.is_some() {
                    "explicit"
                } else {
                    "default"
                },
                assigned_playlist_id: &assigned.id,
                assigned_playlist: &assigned.name,
                playlist_id: &effective.id,
                playlist: &effective.name,
                stand_in_for: self
                    .route_asked_for(output, route)
                    .and_then(|asked| self.stood_in_for(asked, &effective.id)),
                entry_id: entry.map_or("", |entry| entry.id.as_str()),
                entry_taboo: entry.is_some_and(|entry| self.is_taboo(&effective.id, &entry.id)),
                kind: entry.map_or("", |entry| entry_kind(entry.kind)),
                still: entry.map_or_else(String::new, |entry| entry.still.display().to_string()),
                motion_active: self.target_outputs.contains(output)
                    && self.driver.motion_active(output),
                route_source: route.source.as_str(),
                manual_override: route.manual_playlist.is_some(),
                schedule_rule_id: route.schedule_rule_id.as_deref(),
                playback_state: playback_state(route.playback_state),
                paused: route.playback_state == PlaybackState::Paused,
                stopped: route.playback_state == PlaybackState::Stopped,
                shuffle: self.route_shuffle(route),
                shuffle_default: self.playlist_shuffle_default(&route.active_playlist),
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
                cycle_interval_seconds: self
                    .playlist_cycle_interval_seconds(&route.active_playlist),
                beats_global_rules: self.beats_global_rules(output),
                renderer_failed: route.renderer_failed,
                last_error: truncate_middle(&route.last_error, MAX_DISPLAY_ERROR_BYTES),
                automatic_retry: self.pending_routes.get(output).map(|pending| {
                    AutomaticRetryStatus {
                        attempt: pending.attempts,
                        maximum_attempts: AUTOMATIC_APPLY_ATTEMPTS,
                        reason: pending.reason,
                    }
                }),
                nothing_to_play: Some(nothing),
                entry_name: Some(nothing_name(nothing)),
                timing: {
                    let timing = self.route_timing(
                        output,
                        route,
                        self.target_outputs.contains(output),
                        &moment,
                        &mut budget,
                    );
                    if nothing.is_some() {
                        timing.without_rotation()
                    } else {
                        timing
                    }
                },
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
            let value = self.route_shuffle(first);
            route_states
                .iter()
                .all(|route| self.route_shuffle(route) == value)
                .then_some(value)
        });
        let common_shuffle_default = route_states.first().and_then(|first| {
            let value = self.playlist_shuffle_default(&first.active_playlist);
            route_states
                .iter()
                .all(|route| self.playlist_shuffle_default(&route.active_playlist) == value)
                .then_some(value)
        });
        let common_interval = match route_states.first() {
            None => Some(self.config.settings.cycle_interval_seconds),
            Some(first) => {
                let value = self.playlist_cycle_interval_seconds(&first.active_playlist);
                route_states
                    .iter()
                    .all(|route| {
                        self.playlist_cycle_interval_seconds(&route.active_playlist) == value
                    })
                    .then_some(value)
            }
        };
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
        let (omitted, mut measured) = abridge_rows(
            &mut displays,
            shape,
            [&self.output_discovery_error, &self.last_error],
        );
        let quiet = LeanSummary::of(
            shape,
            &self.config,
            self.routes_nothing_reason(&self.target_outputs),
            self.refused_rows(shape.refused),
            self.refused_omitted(shape.refused),
        );
        measured.lean_saving += quiet.lean_saving;
        let json = serde_json::to_string(&Status {
            loaded_config_sha256: self.config.source_sha256.as_deref(),
            supported_config_schemas: SUPPORTED_SCHEMA_VERSIONS,
            supported_override_schemas: SUPPORTED_OVERRIDE_SCHEMAS,
            loaded_overrides_sha256: quiet.loaded_overrides_sha256,
            overrides_ignored: quiet.overrides_ignored,
            runtime_version: env!("CARGO_PKG_VERSION"),
            runtime_executable: self.runtime_executable.as_deref(),
            install_kind: crate::INSTALL_KIND,
            power_source: self.power.observation.source.name(),
            power_available: self.power.observation.source != PowerSource::Unknown,
            stop_animations_on_battery: self.battery_policy_enabled(),
            animations_inhibited: self.animations_inhibited(),
            animation_inhibition_reason: self.power.reason(self.battery_policy_enabled()),
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
            stand_in_for: summary_stand_in(&displays, summary_playlist),
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
            entry_taboo,
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
            shuffle_default: common_shuffle_default.unwrap_or(self.config.settings.shuffle),
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
            cycle_interval_seconds: common_interval,
            last_error: if shape.rows.last_error {
                &self.last_error
            } else {
                ""
            },
            output_discovery_error: if shape.rows.discovery_error {
                &self.output_discovery_error
            } else {
                ""
            },
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
            outputs: if shape.outputs {
                self.driver.published_outputs()
            } else {
                &[]
            },
            missing_renderers: self.missing_rows(shape.missing),
            nothing_to_play: quiet.nothing_to_play,
            allow_web_wallpapers: quiet.allow_web_wallpapers,
            refused: quiet.refused,
            refused_omitted: quiet.refused_omitted,
            stand_in_for_omitted: omitted.stand_in_for,
            schedule_rule_id_omitted: omitted.schedule_rule_id,
            diagnostics_omitted: omitted.diagnostics,
        })
        .map_err(|error| error.to_string())?;
        Ok((json, measured))
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

/// The moment one status reply describes, on every clock it needs.
struct StatusMoment {
    instant: Instant,
    utc: DateTime<Utc>,
    timeline: schedule::Timeline,
}

/// Time left on a rotation timer started at `last_cycle`; zero once due.
fn cycle_remaining(last_cycle: Instant, interval_seconds: u64, now: Instant) -> Duration {
    last_cycle
        .checked_add(Duration::from_secs(interval_seconds))
        .map_or(Duration::ZERO, |due| due.saturating_duration_since(now))
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
    left.iter()
        .zip(right)
        .all(|(left, right)| target_equal_ignoring_taboo(left, right))
}

fn target_equal_ignoring_taboo(left: &Target, right: &Target) -> bool {
    if left.playlist_id != right.playlist_id || left.output != right.output {
        return false;
    }
    let mut left_entry = left.entry.clone();
    let mut right_entry = right.entry.clone();
    left_entry.taboo = None;
    right_entry.taboo = None;
    left_entry == right_entry
}

fn changed_target_outputs(left: &[Target], right: &[Target]) -> HashSet<String> {
    let left_by_output: HashMap<&str, &Target> = left
        .iter()
        .map(|target| (target.output.as_str(), target))
        .collect();
    let right_by_output: HashMap<&str, &Target> = right
        .iter()
        .map(|target| (target.output.as_str(), target))
        .collect();
    left_by_output
        .keys()
        .chain(right_by_output.keys())
        .filter_map(|output| {
            let changed = match (left_by_output.get(output), right_by_output.get(output)) {
                (Some(left), Some(right)) => !target_equal_ignoring_taboo(left, right),
                _ => true,
            };
            changed.then(|| (*output).to_string())
        })
        .collect()
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
        DisplayTiming, EntryKey, MAX_TABOO_STATUS_ENTRIES, PLAYBACK_HISTORY_LIMIT, cycle_remaining,
        decimal_digits, promote_taboo_status_key, push_bounded, wall_clock_after, whole_seconds_up,
    };
    use crate::schedule::Change;
    use crate::schedule::test_zones::{Apia2011, Chicago2026, LordHowe2026};
    use chrono::{NaiveDate, NaiveDateTime, TimeDelta, Utc};
    use std::time::{Duration, Instant};

    /// Timing judged at `at`, with UTC as both the zone and the reference.
    fn utc_timing(
        at: NaiveDateTime,
        route_change: Option<NaiveDateTime>,
        cycle_in: Option<Duration>,
    ) -> DisplayTiming {
        let change = route_change.map(|local| Change {
            local,
            after: local - at,
        });
        DisplayTiming::new(change, cycle_in, at.and_utc(), &Utc)
    }

    fn day(month: u32, day: u32, hour: u32, minute: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(2026, month, day)
            .unwrap()
            .and_hms_opt(hour, minute, 0)
            .unwrap()
    }

    #[test]
    fn the_cycle_deadline_is_projected_across_both_chicago_changes() {
        let ten_minutes = Some(Duration::from_secs(600));
        // 01:55 CST on 2026-03-08 (07:55 UTC): ten minutes later the clock
        // has jumped to daylight time. Adding to the local reading would say
        // 02:05, a time that never shows.
        let spring =
            DisplayTiming::new(None, ten_minutes, day(3, 8, 7, 55).and_utc(), &Chicago2026);
        assert_eq!(spring.next_cycle_at.as_deref(), Some("2026-03-08T03:05:00"));
        assert_eq!(spring.next_cycle_in_s, Some(600));
        // 01:55 CDT on 2026-11-01 (06:55 UTC): ten minutes later the clock
        // has gone back to 01:05 standard time, not on to 02:05.
        let fall = DisplayTiming::new(None, ten_minutes, day(11, 1, 6, 55).and_utc(), &Chicago2026);
        assert_eq!(fall.next_cycle_at.as_deref(), Some("2026-11-01T01:05:00"));
        assert_eq!(fall.next_cycle_in_s, Some(600));
        // The same reading an hour later, in standard time, simply adds.
        assert_eq!(
            wall_clock_after(day(11, 1, 7, 55).and_utc(), 600, &Chicago2026),
            Some(day(11, 1, 2, 5))
        );
        // Away from a change, the projection is plain addition.
        assert_eq!(
            wall_clock_after(day(8, 3, 14, 0).and_utc(), 300, &Chicago2026),
            Some(day(8, 3, 9, 5))
        );
    }

    #[test]
    fn the_cycle_deadline_is_projected_across_half_hour_and_date_line_changes() {
        let at_utc = |year, month, day, hour, minute| {
            NaiveDate::from_ymd_opt(year, month, day)
                .unwrap()
                .and_hms_opt(hour, minute, 0)
                .unwrap()
        };
        // Lord Howe, first 01:55 on 2026-04-05 (14:55 UTC): ten minutes later
        // the clock has gone back half an hour to 01:35.
        assert_eq!(
            wall_clock_after(at_utc(2026, 4, 4, 14, 55).and_utc(), 600, &LordHowe2026),
            Some(at_utc(2026, 4, 5, 1, 35))
        );
        // Lord Howe, 01:55 on 2026-10-04 (15:25 UTC): ten minutes later the
        // clock has jumped half an hour to 02:35.
        assert_eq!(
            wall_clock_after(at_utc(2026, 10, 3, 15, 25).and_utc(), 600, &LordHowe2026),
            Some(at_utc(2026, 10, 4, 2, 35))
        );
        // Apia, Thursday 2011-12-29 23:55 (09:55 UTC): ten minutes later it is
        // Saturday 00:05.
        assert_eq!(
            wall_clock_after(at_utc(2011, 12, 30, 9, 55).and_utc(), 600, &Apia2011),
            Some(at_utc(2011, 12, 31, 0, 5))
        );
    }

    #[test]
    fn a_route_change_across_the_rewind_reads_the_clock_and_counts_real_seconds() {
        // At the first 01:55 on 2026-11-01 a change at the rewind reads 01:00
        // and is 300 real seconds away; a difference of readings would be
        // negative.
        let rewind = Change {
            local: day(11, 1, 1, 0),
            after: TimeDelta::minutes(5),
        };
        let timing = DisplayTiming::new(
            Some(rewind),
            None,
            day(11, 1, 6, 55).and_utc(),
            &Chicago2026,
        );
        assert_eq!(
            timing.route_change_at.as_deref(),
            Some("2026-11-01T01:00:00")
        );
        assert_eq!(timing.route_change_in_s, Some(300));
        assert_eq!(timing.until.as_deref(), Some("01:00"));
    }

    fn at(hour: u32, minute: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(2026, 8, 3)
            .unwrap()
            .and_hms_opt(hour, minute, 0)
            .unwrap()
    }

    #[test]
    fn timing_rounds_up_to_whole_seconds_and_never_goes_negative() {
        let half = at(17, 59) + TimeDelta::milliseconds(30_500);
        assert_eq!(whole_seconds_up(at(18, 0) - half), 30);
        assert_eq!(whole_seconds_up(at(18, 0) - at(17, 0)), 3600);
        assert_eq!(whole_seconds_up(at(18, 0) - at(18, 1)), 0);

        let start = Instant::now();
        let later = start + Duration::from_millis(100_250);
        assert_eq!(
            cycle_remaining(start, 300, later),
            Duration::from_millis(199_750)
        );
        assert_eq!(
            cycle_remaining(start, 300, start + Duration::from_secs(900)),
            Duration::ZERO
        );
        let timing = utc_timing(at(9, 0), None, Some(Duration::from_millis(199_750)));
        assert_eq!(timing.next_cycle_in_s, Some(200));
        assert_eq!(timing.next_change_in_s, Some(200));
        assert_eq!(timing.next_cycle_at.as_deref(), Some("2026-08-03T09:03:20"));
        let due = utc_timing(at(9, 0), None, Some(Duration::ZERO));
        assert_eq!(due.next_cycle_in_s, Some(0));
        assert_eq!(due.next_cycle_at.as_deref(), Some("2026-08-03T09:00:00"));
    }

    #[test]
    fn until_is_given_only_for_a_change_less_than_a_day_away() {
        let soon = utc_timing(at(9, 0), Some(at(18, 0)), None);
        assert_eq!(soon.until.as_deref(), Some("18:00"));
        assert_eq!(soon.route_change_at.as_deref(), Some("2026-08-03T18:00:00"));
        assert_eq!(soon.route_change_in_s, Some(9 * 3600));
        assert_eq!(soon.next_change_in_s, None);
        let almost_a_day = utc_timing(at(9, 1), Some(at(9, 0) + TimeDelta::days(1)), None);
        assert_eq!(almost_a_day.until.as_deref(), Some("09:00"));
        let a_day = utc_timing(at(9, 0), Some(at(9, 0) + TimeDelta::days(1)), None);
        assert_eq!(a_day.until, None);
        assert_eq!(a_day.route_change_in_s, Some(86_400));
        assert_eq!(utc_timing(at(9, 0), None, None).until, None);
    }

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

    #[test]
    fn a_count_is_as_many_characters_as_it_has_digits() {
        for (count, digits) in [
            (0, 1),
            (9, 1),
            (10, 2),
            (64, 2),
            (99, 2),
            (100, 3),
            (usize::MAX, 20),
        ] {
            assert_eq!(decimal_digits(count), digits, "{count}");
            assert_eq!(decimal_digits(count), count.to_string().len(), "{count}");
        }
    }
}
