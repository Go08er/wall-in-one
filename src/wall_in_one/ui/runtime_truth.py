"""Small, validated view of the Rust runtime's atomic status snapshot."""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from wall_in_one import runtime_config
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.library.playlists import Playlist


@dataclass(frozen=True)
class RefusedTruth:
    """One Wallpaper Engine item the service won't start in its loaded document.

    Each entry of it is skipped, the way an empty playlist's display plays
    nothing: never a taboo and never saved. The service asks again after a
    reload.
    """

    #: The Workshop id of the item an entry renders: a preset's base.
    scene_id: str
    #: Refused because the document doesn't allow web wallpapers. Otherwise
    #: it is an item the service can't find, read or play.
    web_page: bool
    #: What the service says, naming the item.
    reason: str


def stand_in_sentence(asked: str, playing: str) -> str:
    """Say that ``asked`` can't play and ``playing`` stands in for it."""
    return f"\u201c{asked}\u201d can\u2019t play here, so \u201c{playing}\u201d is playing"


def pick_stand_in_sentence(playing: str) -> str:
    """Say that the pick can't play and ``playing`` stands in, when the pick isn't named."""
    return f"Your pick can\u2019t play here, so \u201c{playing}\u201d is playing"


def stand_ins_sentence(asked: Iterable[str], playing: str) -> str:
    """`stand_in_sentence` for a playing playlist that stands in for several.

    Each asked-for playlist is quoted on its own; one is the plain sentence.
    """
    names = list(dict.fromkeys(asked))
    if len(names) <= 1:
        return stand_in_sentence(names[0] if names else "", playing)
    quoted = " and ".join(f"\u201c{name}\u201d" for name in names)
    return f"{quoted} can\u2019t play here, so \u201c{playing}\u201d is playing"


#: What a pick may be when the reply left out every ``stand_in_for``: it can't be
#: told from the playlist that stands in for it.
PICK_OR_STAND_IN = "your pick, or what stands in for it"
#: The same for a rule whose id the reply left out (`schedule_rule_id_omitted`).
SCHEDULE_OR_STAND_IN = "from schedule, or what stands in for it"

#: What is said in place of "no error" when the reply left its diagnostics out.
DIAGNOSTICS_LEFT_OUT = "details left out: status too large"


def diagnostics_left_out(status: Mapping[str, object] | None) -> bool:
    """Whether the reply's size dropped ``last_error`` and ``output_discovery_error``.

    They then read as empty strings, which is not "no error".
    """
    return status is not None and status.get("diagnostics_omitted") is True


def nothing_to_play_sentence(reason: str) -> str:
    """Say that a display plays nothing, and the service's reason."""
    return f"Nothing to play: {reason}"


@dataclass(frozen=True)
class AutomaticRetryTruth:
    attempt: int
    maximum_attempts: int
    reason: str


@dataclass(frozen=True)
class DisplayRuntimeTruth:
    """One live connector route from status version 2."""

    connector: str
    connected: bool
    route_source: str
    manual_override: bool
    playlist_id: str
    playlist: str
    entry_id: str
    entry_taboo: bool
    motion_active: bool
    schedule_rule_id: str | None
    playback_state: str
    assignment_source: str
    assigned_playlist_id: str
    assigned_playlist: str
    shuffle: bool
    shuffle_default: bool
    shuffle_source: str
    cycle_enabled: bool
    cycle_default: bool
    cycle_source: str
    renderer_failed: bool
    last_error: str
    automatic_retry: AutomaticRetryTruth | None
    #: When the schedule next changes this route's playlist or reason (local
    #: time), and whole seconds until then. None from a service that does not
    #: say, while a pick holds, or with no change within eight days.
    route_change_at: datetime.datetime | None = None
    route_change_in_s: int | None = None
    #: When the rotation next advances, and whole seconds until then. None
    #: from a service that does not say, or when the route is paused or does
    #: not cycle.
    next_cycle_at: datetime.datetime | None = None
    next_cycle_in_s: int | None = None
    #: ``route_change_at`` as local ``HH:MM``, only when less than a day away.
    until: str | None = None
    #: The playlist this display asked for (its pick, rule, assignment or the
    #: default) when ``playlist_id`` is a stand-in for it: the service refused
    #: every entry of it. None while it plays its own, or from a service that
    #: doesn't say.
    stand_in_for: str | None = None
    #: That playlist's name, from the snapshot's own playlist inventory (its
    #: id when the inventory doesn't name it). Empty with no ``stand_in_for``.
    stand_in_for_name: str = ""
    #: Why this display plays nothing (``nothing_to_play``): the playlist asked
    #: for has nothing that can play and nothing can stand in. Else empty.
    nothing_to_play: str = ""
    #: A pick (or a rule whose id was left out) on a reply that dropped its
    #: ``stand_in_for``: what plays may be what the route asked for, or what stands
    #: in for it. The app can't tell.
    route_unsure: bool = False
    #: A pick that certainly can't play, on a reply that dropped its ``stand_in_for``:
    #: mirrored, every row shares the pick's decision and the flag says one had a
    #: stand-in, so this row plays one. Which playlist the pick was isn't said.
    pick_stood_in: bool = False

    @property
    def asked_playlist_id(self) -> str:
        """The playlist this display asked for: its own, or the one stood in for.

        Empty for a pick stood in for, which no field names.
        """
        if self.pick_stood_in:
            return ""
        return self.stand_in_for or self.playlist_id


@dataclass(frozen=True)
class ThemeSourceTruth:
    """The configured and effective source of Noctalia's global palette."""

    configured: str
    effective: str | None
    fallback: bool


@dataclass(frozen=True)
class RuntimeScheduleTruth:
    """One read-only authored rule from the atomic runtime snapshot."""

    id: str
    playlist_id: str
    playlist: str
    connector: str | None
    enabled: bool
    selected: bool
    in_force: bool


@dataclass(frozen=True)
class PowerRuntimeTruth:
    """Automatic animation policy, separate from manual playback intent."""

    source: str
    available: bool
    enabled: bool
    inhibited: bool
    reason: str

    @property
    def message(self) -> str:
        if self.inhibited:
            if self.reason == "power-unavailable":
                return "Power information unavailable · battery restriction retained"
            return "Animations stopped on battery"
        if self.enabled and not self.available:
            return "Power information unavailable"
        return ""


_POWER_FIELDS = (
    "power_source",
    "power_available",
    "stop_animations_on_battery",
    "animations_inhibited",
    "animation_inhibition_reason",
)


def power_from_status(status: Mapping[str, object] | None) -> PowerRuntimeTruth | None:
    """Read additive power fields; older runtimes have no power-policy view."""
    if status is None or not all(key in status for key in _POWER_FIELDS):
        return None
    source = status["power_source"]
    available = status["power_available"]
    enabled = status["stop_animations_on_battery"]
    inhibited = status["animations_inhibited"]
    reason = status["animation_inhibition_reason"]
    if (
        not isinstance(source, str)
        or source not in ("ac", "battery", "unknown")
        or type(available) is not bool
        or type(enabled) is not bool
        or type(inhibited) is not bool
        or not isinstance(reason, str)
        or reason not in ("", "battery", "power-unavailable")
        or available != (source != "unknown")
        or (enabled and source == "battery" and not inhibited)
        or (inhibited and (not enabled or source == "ac" or not reason))
        or (not inhibited and reason != "")
        or (reason == "battery" and source != "battery")
        or (reason == "power-unavailable" and available)
    ):
        return None
    return PowerRuntimeTruth(source, available, enabled, inhibited, reason)


@dataclass(frozen=True)
class RuntimeTruth:
    """The playback fields authoring pages need from one status response."""

    playlist_id: str
    playlist: str
    source: str
    scheduled_playlist_id: str
    scheduled_playlist: str
    schedule_rule_id: str | None
    active_playlist_ids: tuple[str, ...]
    status_version: int = 1
    display_mode: str = "mirrored"
    displays: tuple[DisplayRuntimeTruth, ...] = ()
    theme_source: ThemeSourceTruth | None = None
    schedules: tuple[RuntimeScheduleTruth, ...] = ()
    power: PowerRuntimeTruth | None = None
    #: The summary's playlist asked for, when every connected display plays
    #: the same stand-in (``playlist_id``) for it; and its name.
    stand_in_for: str | None = None
    stand_in_for_name: str = ""
    #: What the service refused in its loaded document (``refused``), and how
    #: many more it left out of this snapshot.
    refused: tuple[RefusedTruth, ...] = ()
    refused_omitted: int = 0
    #: Why every display plays nothing (``nothing_to_play``), or empty.
    nothing_to_play: str = ""
    #: The summary's pick, on a reply that dropped its ``stand_in_for``.
    route_unsure: bool = False
    #: The same, when every connected display's pick is certainly stood in for.
    pick_stood_in: bool = False
    #: The reply's size emptied ``last_error`` and ``output_discovery_error`` (also the
    #: rows'): an empty one is then not "no error".
    diagnostics_omitted: bool = False

    @property
    def asked_playlist_id(self) -> str:
        """The summary's playlist asked for: its own, or the one stood in for."""
        if self.pick_stood_in:
            return ""
        return self.stand_in_for or self.playlist_id

    def refusal(self, scene_id: str) -> RefusedTruth | None:
        """Why the service refused the Workshop item ``scene_id``, if it said so."""
        return next((one for one in self.refused if one.scene_id == scene_id), None)

    @property
    def follows_schedule(self) -> bool:
        return self.source == "schedule"

    @property
    def is_manual(self) -> bool:
        return self.source == "manual"

    @property
    def is_multi_display(self) -> bool:
        """Whether connector assignments currently select several playlists."""
        if self.status_version == 2:
            # Detached routes remain in the atomic inventory so Schedules can
            # explain what will happen after a reconnect. They are not current
            # playback, even if the service retains different route state for
            # them and the compatibility top-level source says ``mixed``.
            return len(self.active_playlist_ids) > 1
        return self.source == "mixed" or (
            not self.playlist_id and len(self.active_playlist_ids) > 1
        )

    def playlist_is_active(self, playlist_id: str) -> bool:
        return playlist_id in self.active_playlist_ids

    def nothing_to_play_for(self, playlist_id: str) -> str:
        """Why ``playlist_id`` plays nothing, when it does so on every display it is on."""
        rows = [d for d in self.displays if d.connected and d.playlist_id == playlist_id]
        if rows:
            return rows[0].nothing_to_play if all(d.nothing_to_play for d in rows) else ""
        return self.nothing_to_play if self.playlist_id == playlist_id else ""

    def display(self, connector: str) -> DisplayRuntimeTruth | None:
        return next((record for record in self.displays if record.connector == connector), None)

    def schedule_rule(self, rule_id: str | None) -> RuntimeScheduleTruth | None:
        if rule_id is None:
            return None
        return next((record for record in self.schedules if record.id == rule_id), None)


@dataclass(frozen=True)
class MediaPlayback:
    """What Media can say and highlight from one runtime snapshot."""

    playlist: str
    current: tuple[Path, ...]


def from_status(status: Mapping[str, object] | None) -> RuntimeTruth | None:
    """Return one coherent playback view, or ``None`` for an invalid snapshot."""
    if status is None:
        return None
    raw_version = status.get("status_version", 1)
    if type(raw_version) is not int or raw_version not in (1, 2):
        return None
    status_version = raw_version
    power = power_from_status(status)
    if any(key in status for key in _POWER_FIELDS) and power is None:
        return None
    playlist_id = status.get("playlist_id")
    playlist = status.get("playlist")
    source = status.get("source")
    allowed_sources = (
        ("manual", "schedule", "mixed")
        if status_version == 2
        else (
            "manual",
            "schedule",
        )
    )
    if (
        not isinstance(playlist_id, str)
        or not isinstance(playlist, str)
        or not playlist
        or source not in allowed_sources
    ):
        return None
    assert isinstance(source, str)

    display_mode = status.get("display_mode", "mirrored")
    if status_version == 2 and display_mode not in ("mirrored", "independent"):
        return None
    if not isinstance(display_mode, str):
        return None

    names = _playlist_names(status.get("playlists"))
    parsed_displays = _display_truth(
        status.get("displays"), required=status_version == 2, names=names
    )
    if parsed_displays is None:
        return None
    theme_source = _theme_source_truth(status.get("theme_source"), required=status_version == 2)
    if status_version == 2 and theme_source is None:
        return None
    parsed_schedules = _schedule_truth(status.get("schedules"))
    if parsed_schedules is None:
        return None

    active_playlist_ids: list[str] = []
    if status_version == 2:
        # The playlist inventory also describes retained detached routes. The
        # connector rows are the only authoritative answer to what is live.
        for display in parsed_displays:
            if (
                display.connected
                and display.playlist_id
                and display.playlist_id not in active_playlist_ids
            ):
                active_playlist_ids.append(display.playlist_id)
    else:
        reported_playlists = status.get("playlists")
        if isinstance(reported_playlists, list):
            for record in reported_playlists:
                if not isinstance(record, Mapping) or record.get("active") is not True:
                    continue
                identifier = record.get("id")
                if (
                    isinstance(identifier, str)
                    and identifier
                    and identifier not in active_playlist_ids
                ):
                    active_playlist_ids.append(identifier)
        if playlist_id and playlist_id not in active_playlist_ids:
            active_playlist_ids.append(playlist_id)
        for display in parsed_displays:
            if display.playlist_id and display.playlist_id not in active_playlist_ids:
                active_playlist_ids.append(display.playlist_id)
    if (
        not playlist_id
        and status_version == 1
        and (source == "manual" or playlist != "Multiple displays" or len(active_playlist_ids) < 2)
    ):
        # Rust uses an empty top-level id only when independent connector
        # assignments make a single global answer impossible. A malformed or
        # incomplete snapshot must not displace the last coherent one.
        return None

    scheduled_playlist_id = ""
    scheduled_playlist = ""
    schedule_rule_id: str | None = None
    schedule = status.get("schedule")
    if isinstance(schedule, Mapping):
        reported_id = schedule.get("playlist_id")
        reported_name = schedule.get("playlist")
        reported_rule = schedule.get("rule_id")
        if isinstance(reported_id, str):
            scheduled_playlist_id = reported_id
        if isinstance(reported_name, str):
            scheduled_playlist = reported_name
        if isinstance(reported_rule, str) and reported_rule:
            schedule_rule_id = reported_rule

    stand_in_for = _stand_in_for(status.get("stand_in_for"), playlist_id)
    refused, refused_omitted = refused_from_status(status)
    nothing_to_play = status.get("nothing_to_play")
    route_unsure = False
    pick_stood_in = False
    if status_version == 2 and status.get("stand_in_for_omitted") is True:
        # Reach (r13f N17): the service drops `stand_in_for` only at the reply ceiling.
        # A mirrored document gets there with about 64 connected outputs or a
        # hand-written `[[displays]]` document; the app's own (one row) leaves over
        # 600 KB to spare (docs/runtime-config.md). The mirrored paths below are for
        # those few.
        # The service says the reply's size dropped some `stand_in_for`. A row
        # that kept its own is left; the others still say what they asked for,
        # except for a pick (named by no field).
        parsed_displays = _inferred_stand_ins(
            parsed_displays,
            parsed_schedules,
            independent=display_mode == "independent",
            names=names,
        )
        heard = {
            (display.stand_in_for, display.playlist_id)
            for display in parsed_displays
            if display.connected and not display.nothing_to_play
        }
        if len(heard) == 1:
            [(asked, playing)] = heard
            if asked and playing == playlist_id:
                stand_in_for = asked
        unnamed = source == "manual" and stand_in_for is None
        # Mirrored, every row's pick is certainly stood in for: say so, don't hedge.
        here = [d for d in parsed_displays if d.connected and not d.nothing_to_play]
        pick_stood_in = (
            unnamed
            and bool(here)
            and all(d.pick_stood_in for d in here)
            and {d.playlist_id for d in here} == {playlist_id}
        )
        route_unsure = unnamed and not pick_stood_in
    return RuntimeTruth(
        playlist_id=playlist_id,
        playlist=playlist,
        source=source,
        scheduled_playlist_id=scheduled_playlist_id,
        scheduled_playlist=scheduled_playlist,
        schedule_rule_id=schedule_rule_id,
        active_playlist_ids=tuple(active_playlist_ids),
        status_version=status_version,
        display_mode=display_mode,
        displays=parsed_displays,
        theme_source=theme_source,
        schedules=parsed_schedules,
        power=power,
        stand_in_for=stand_in_for,
        stand_in_for_name=names.get(stand_in_for, stand_in_for) if stand_in_for else "",
        refused=refused,
        refused_omitted=refused_omitted,
        nothing_to_play=nothing_to_play if isinstance(nothing_to_play, str) else "",
        route_unsure=route_unsure,
        pick_stood_in=pick_stood_in,
        diagnostics_omitted=diagnostics_left_out(status),
    )


def pick_holders(
    truth: RuntimeTruth | None, record: str | None, playlist_id: str
) -> tuple[bool, tuple[str, ...]]:
    """Where a manual pick of ``playlist_id`` holds: (the whole screen, these connectors).

    The app's own record of a pick (``session.manual_playlist``) is written only by its
    own paths, not by a pick made through the service (``ctl``, the companion), so the
    service's status is read too, where it names the pick: the playlist asked for by a
    manual route (its ``stand_in_for`` when a stand-in plays). A pick no field names
    (the reply ceiling) is named by neither, and so not here.
    """
    whole = record == playlist_id
    connectors: list[str] = []
    if truth is not None:
        independent = truth.display_mode == "independent"
        if not independent and truth.is_manual and truth.asked_playlist_id == playlist_id:
            whole = True
        if independent:
            connectors = [
                row.connector
                for row in truth.displays
                if row.connected and row.manual_override and row.asked_playlist_id == playlist_id
            ]
    return whole, tuple(connectors)


def sibling_rule_id(
    displays: Iterable[DisplayRuntimeTruth], display: DisplayRuntimeTruth, *, independent: bool
) -> str | None:
    """A rule id for ``display``, which the reply left out, from a row that kept its own.

    Mirrored, every row shares one decision (`runtime.rs`, the mirrored rows are built
    from one reference), so any connected schedule row that kept its id names the
    decision of all of them. Independent rows decide apart: nothing is borrowed. Rows
    that disagree (which the service doesn't send) name nothing.

    Reach: the service leaves rule ids out only at the reply ceiling (step 6 of the
    fitting, `docs/runtime-config.md`), which a mirrored document reaches only with
    about 64 connected outputs or a hand-written mirrored ``[[displays]]`` document.
    The app's own one-row mirrored document never gets here, so this and its callers
    are rare paths, kept honest rather than common.
    """
    if independent or display.route_source != "schedule":
        return None
    ids = {
        one.schedule_rule_id
        for one in displays
        if one.connected and one.route_source == "schedule" and one.schedule_rule_id
    }
    return next(iter(ids)) if len(ids) == 1 else None


def _inferred_stand_ins(
    displays: tuple[DisplayRuntimeTruth, ...],
    schedules: tuple[RuntimeScheduleTruth, ...],
    *,
    independent: bool,
    names: Mapping[str, str],
) -> tuple[DisplayRuntimeTruth, ...]:
    """Each row's ``stand_in_for``, worked out from what the row still says.

    A default, an assignment and a rule play what they ask for unless the
    service refused it, so a row that plays another playlist stands in for it.
    The playlist asked for is the row's own decision, never the top-level
    ``schedule``, which is what the schedule chooses now: a default or an
    assignment is the row's ``assigned_playlist_id``, a rule is its
    ``schedule_rule_id``'s playlist. A pick is on no field. Mirrored, every row
    shares the pick's decision and the flag says one had a stand-in, so the pick
    is certainly stood in for (though not which playlist it was); independent, the
    row is marked unsure instead.

    With the rule's id left out (``schedule_rule_id_omitted``), or naming no rule
    the inventory lists, the playlist can't be looked up, so the row is marked
    unsure like a pick rather than guessed. The service leaves ids out per row, so
    a row that kept its id is trusted whatever the flag says. A null one takes the id of
    a mirrored sibling that kept its own (`sibling_rule_id`); only with none to take is
    it hedged.
    """
    found: list[DisplayRuntimeTruth] = []
    for display in displays:
        if not display.connected or display.nothing_to_play:
            found.append(display)
            continue
        if display.stand_in_for:
            found.append(display)
            continue
        if display.route_source == "manual":
            # Certain without asking the app's own record of the pick (which can lead
            # or lag the service): mirrored rows share one decision, and the flag says
            # a row of it had a stand-in.
            found.append(
                replace(display, route_unsure=True)
                if independent
                else replace(display, pick_stood_in=True)
            )
            continue
        if display.route_source == "schedule":
            rule_id = display.schedule_rule_id or sibling_rule_id(
                displays, display, independent=independent
            )
            rule = next((one for one in schedules if one.id == rule_id), None)
            if rule is None:
                found.append(replace(display, route_unsure=True))
                continue
            asked = rule.playlist_id
        else:
            asked = display.assigned_playlist_id
        stand_in_for = _stand_in_for(asked, display.playlist_id)
        found.append(
            replace(
                display,
                stand_in_for=stand_in_for,
                stand_in_for_name=names.get(stand_in_for, stand_in_for) if stand_in_for else "",
            )
        )
    return tuple(found)


def _playlist_names(raw: object) -> dict[str, str]:
    """The snapshot's playlist inventory as id -> name; malformed rows are left out."""
    if not isinstance(raw, list):
        return {}
    names: dict[str, str] = {}
    for record in raw:
        if not isinstance(record, Mapping):
            continue
        identifier = record.get("id")
        name = record.get("name")
        if isinstance(identifier, str) and identifier and isinstance(name, str) and name:
            names.setdefault(identifier, name)
    return names


def _stand_in_for(raw: object, playlist_id: str) -> str | None:
    """An additive ``stand_in_for``: a playlist id other than the one playing.

    Advisory, so a malformed value is dropped rather than rejecting the
    snapshot; a service that predates it never sends it.
    """
    if not isinstance(raw, str) or not raw or raw == playlist_id:
        return None
    return raw


#: What one snapshot carries of ``refused`` at most: the service's own cap is
#: 16 rows. A longer list is malformed, and read no further.
_MAX_REFUSED_ROWS = 64


def refused_from_status(
    status: Mapping[str, object] | None,
) -> tuple[tuple[RefusedTruth, ...], int]:
    """The additive ``refused`` list and ``refused_omitted`` count, or none.

    Advisory, like the timing fields: a malformed row is left out, and a
    service that predates them reports neither.
    """
    if status is None:
        return (), 0
    raw = status.get("refused")
    rows: list[RefusedTruth] = []
    if isinstance(raw, list):
        for record in raw[:_MAX_REFUSED_ROWS]:
            if not isinstance(record, Mapping):
                continue
            scene_id = record.get("scene_id")
            web_page = record.get("web_page")
            reason = record.get("reason")
            if (
                isinstance(scene_id, str)
                and scene_id.isascii()
                and scene_id.isdigit()
                and type(web_page) is bool
                and isinstance(reason, str)
            ):
                rows.append(RefusedTruth(scene_id, web_page, reason))
    omitted = status.get("refused_omitted", 0)
    return tuple(rows), omitted if type(omitted) is int and omitted > 0 else 0


def refused_paths(refused: Iterable[RefusedTruth], items: Iterable[MediaItem]) -> dict[Path, str]:
    """The library items the service refused, by path, with its reason.

    A Wallpaper Engine item is refused by the item its entries render: a
    preset's base, else its own Workshop id.
    """
    reasons = {one.scene_id: one.reason for one in refused}
    if not reasons:
        return {}
    return {
        item.path: reasons[item.scene_base or item.scene]
        for item in items
        if item.kind is Kind.SCENE and (item.scene_base or item.scene) in reasons
    }


def _theme_source_truth(raw: object, *, required: bool) -> ThemeSourceTruth | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return None
    configured = raw.get("configured")
    effective = raw.get("effective")
    fallback = raw.get("fallback")
    if (
        not isinstance(configured, str)
        or (effective is not None and not isinstance(effective, str))
        or type(fallback) is not bool
    ):
        return None
    return ThemeSourceTruth(configured, effective, fallback)


def _schedule_truth(raw: object) -> tuple[RuntimeScheduleTruth, ...] | None:
    """Keep connector attribution from the read-only schedule inventory."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return None
    parsed: list[RuntimeScheduleTruth] = []
    seen: set[str] = set()
    for record in raw:
        if not isinstance(record, Mapping):
            return None
        identifier = record.get("id")
        playlist_id = record.get("playlist_id")
        playlist = record.get("playlist")
        connector = record.get("connector")
        enabled = record.get("enabled")
        selected = record.get("selected")
        in_force = record.get("in_force")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in seen
            or not isinstance(playlist_id, str)
            or not playlist_id
            or not isinstance(playlist, str)
            or not playlist
            or (connector is not None and (not isinstance(connector, str) or not connector))
            or type(enabled) is not bool
            or type(selected) is not bool
            or type(in_force) is not bool
        ):
            return None
        seen.add(identifier)
        parsed.append(
            RuntimeScheduleTruth(
                identifier,
                playlist_id,
                playlist,
                connector,
                enabled,
                selected,
                in_force,
            )
        )
    return tuple(parsed)


def _display_truth(
    raw: object,
    *,
    required: bool,
    names: Mapping[str, str] | None = None,
) -> tuple[DisplayRuntimeTruth, ...] | None:
    if raw is None:
        return None if required else ()
    if not isinstance(raw, list):
        return None
    parsed: list[DisplayRuntimeTruth] = []
    seen: set[str] = set()
    for record in raw:
        if not isinstance(record, Mapping):
            return None if required else ()
        if required and not {
            "connector",
            "connected",
            "route_source",
            "manual_override",
            "playlist_id",
            "playlist",
            "entry_id",
            "motion_active",
            "schedule_rule_id",
            "playback_state",
            "assignment_source",
            "assigned_playlist_id",
            "assigned_playlist",
            "shuffle",
            "shuffle_default",
            "shuffle_source",
            "cycle_enabled",
            "cycle_default",
            "cycle_source",
            "renderer_failed",
            "last_error",
            "automatic_retry",
        }.issubset(record):
            return None
        connector = record.get("connector")
        playlist_id = record.get("playlist_id")
        playlist = record.get("playlist")
        entry_id = record.get("entry_id", "")
        if not all(
            isinstance(value, str) for value in (connector, playlist_id, playlist, entry_id)
        ):
            return None if required else ()
        assert isinstance(connector, str)
        assert isinstance(playlist_id, str)
        assert isinstance(playlist, str)
        assert isinstance(entry_id, str)
        if not connector or connector in seen or not playlist:
            return None if required else ()
        seen.add(connector)

        connected = record.get("connected", connector == "ALL")
        route_source = record.get("route_source", "default")
        manual_override = record.get("manual_override", False)
        motion_active = record.get("motion_active", False)
        entry_taboo = record.get("entry_taboo", False)
        schedule_rule = record.get("schedule_rule_id")
        playback_state = record.get("playback_state", "playing")
        assignment_source = record.get("assignment_source", "default")
        assigned_id = record.get("assigned_playlist_id", playlist_id)
        assigned_name = record.get("assigned_playlist", playlist)
        shuffle = record.get("shuffle", False)
        shuffle_default = record.get("shuffle_default", shuffle)
        shuffle_source = record.get("shuffle_source", "config")
        cycle_enabled = record.get("cycle_enabled", False)
        cycle_default = record.get("cycle_default", cycle_enabled)
        cycle_source = record.get("cycle_source", "config")
        renderer_failed = record.get("renderer_failed", False)
        last_error = record.get("last_error", "")
        automatic_retry = _automatic_retry_truth(record.get("automatic_retry"))
        if (
            type(connected) is not bool
            or route_source not in ("manual", "schedule", "assignment", "default")
            or type(manual_override) is not bool
            or type(motion_active) is not bool
            or type(entry_taboo) is not bool
            or (schedule_rule is not None and not isinstance(schedule_rule, str))
            or playback_state not in ("playing", "paused", "stopped")
            or assignment_source not in ("explicit", "default")
            or not isinstance(assigned_id, str)
            or not isinstance(assigned_name, str)
            or type(shuffle) is not bool
            or type(shuffle_default) is not bool
            or shuffle_source not in ("manual", "config")
            or type(cycle_enabled) is not bool
            or type(cycle_default) is not bool
            or cycle_source not in ("manual", "config")
            or type(renderer_failed) is not bool
            or not isinstance(last_error, str)
            or (record.get("automatic_retry") is not None and automatic_retry is None)
        ):
            return None if required else ()
        route_change_at, route_change_in_s = _timing_pair(
            record.get("route_change_at"), record.get("route_change_in_s")
        )
        next_cycle_at, next_cycle_in_s = _timing_pair(
            record.get("next_cycle_at"), record.get("next_cycle_in_s")
        )
        until = record.get("until")
        stand_in_for = _stand_in_for(record.get("stand_in_for"), playlist_id)
        nothing = record.get("nothing_to_play")
        parsed.append(
            DisplayRuntimeTruth(
                connector=connector,
                connected=connected,
                route_source=route_source,
                manual_override=manual_override,
                playlist_id=playlist_id,
                playlist=playlist,
                entry_id=entry_id,
                entry_taboo=entry_taboo,
                motion_active=motion_active,
                schedule_rule_id=schedule_rule or None,
                playback_state=playback_state,
                assignment_source=assignment_source,
                assigned_playlist_id=assigned_id,
                assigned_playlist=assigned_name,
                shuffle=shuffle,
                shuffle_default=shuffle_default,
                shuffle_source=shuffle_source,
                cycle_enabled=cycle_enabled,
                cycle_default=cycle_default,
                cycle_source=cycle_source,
                renderer_failed=renderer_failed,
                last_error=last_error,
                automatic_retry=automatic_retry,
                route_change_at=route_change_at,
                route_change_in_s=route_change_in_s,
                next_cycle_at=next_cycle_at,
                next_cycle_in_s=next_cycle_in_s,
                until=(
                    until
                    if isinstance(until, str)
                    and _CLOCK_TIME.fullmatch(until)
                    and route_change_at is not None
                    else None
                ),
                stand_in_for=stand_in_for,
                stand_in_for_name=(
                    (names or {}).get(stand_in_for, stand_in_for) if stand_in_for else ""
                ),
                nothing_to_play=nothing if isinstance(nothing, str) else "",
            )
        )
    return tuple(parsed)


_CLOCK_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def _timing_pair(
    when: object, seconds: object
) -> tuple[datetime.datetime, int] | tuple[None, None]:
    """One additive timing field pair: a local wall-clock time and its seconds.

    Advisory, so a malformed pair is dropped rather than rejecting the whole
    snapshot; a service that predates these fields reports neither.
    """
    if not isinstance(when, str) or type(seconds) is not int or seconds < 0:
        return None, None
    try:
        parsed = datetime.datetime.fromisoformat(when)
    except ValueError:
        return None, None
    if parsed.tzinfo is not None:
        return None, None
    return parsed, seconds


def _automatic_retry_truth(raw: object) -> AutomaticRetryTruth | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return None
    attempt = raw.get("attempt")
    maximum = raw.get("maximum_attempts")
    reason = raw.get("reason")
    if (
        type(attempt) is not int
        or type(maximum) is not int
        or not 1 <= attempt <= maximum
        or not isinstance(reason, str)
        or not reason
    ):
        return None
    return AutomaticRetryTruth(attempt, maximum, reason)


def _unique_still_source(still: object, items: tuple[MediaItem, ...]) -> Path | None:
    """Resolve a representative only when it identifies one media item.

    A pairing still can legitimately be shared. Highlighting every wallpaper
    that happens to use it would claim they are all playing, so ambiguity
    deliberately produces no highlight.
    """
    if not isinstance(still, str):
        return None
    still_path = Path(still)
    if not still_path.is_absolute():
        return None
    candidates = {
        item.path
        for item in items
        if item.path == still_path or item.paired_still == still_path or item.preview == still_path
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def media_playback(
    status: Mapping[str, object] | None,
    authored_playlists: Iterable[Playlist],
    media: Iterable[MediaItem],
) -> MediaPlayback | None:
    """Resolve current runtime entries to Media paths without inventing state.

    Named playlists use their stable authored entry ids. ``All media`` is a
    generated playlist, so its stable path hash is the only authoring identity
    available. The resolved still is a final, conservative fallback for an
    old or just-edited playlist snapshot.
    """
    truth = from_status(status)
    if truth is None or status is None:
        return None

    items = tuple(media)
    known_paths = {item.path for item in items}
    authored = {playlist.id: playlist for playlist in authored_playlists}

    # Detect the astronomically unlikely truncated-hash collision instead of
    # choosing whichever path happened to be scanned last.
    generated: dict[str, Path | None] = {}
    for item in items:
        identifier = runtime_config.entry_id_for_source(item.path)
        if identifier not in generated:
            generated[identifier] = item.path
        elif generated[identifier] != item.path:
            generated[identifier] = None

    reported_displays = status.get("displays")
    records: tuple[Mapping[str, object], ...] = ()
    if isinstance(reported_displays, list):
        records = tuple(
            record
            for record in reported_displays
            if isinstance(record, Mapping) and record.get("connected") is not False
        )
    if not records:
        records = (status,)

    resolved: list[Path] = []
    display_playlists: dict[str, str] = {}
    for record in records:
        playlist_id = record.get("playlist_id")
        entry_id = record.get("entry_id")
        if isinstance(playlist_id, str) and playlist_id:
            playlist_name = record.get("playlist")
            display_playlists.setdefault(
                playlist_id,
                playlist_name if isinstance(playlist_name, str) and playlist_name else playlist_id,
            )

        source: Path | None = None
        if isinstance(entry_id, str) and entry_id:
            if playlist_id == runtime_config.FALLBACK_PLAYLIST_ID:
                source = generated.get(entry_id)
            elif isinstance(playlist_id, str):
                playlist = authored.get(playlist_id)
                if playlist is not None:
                    authored_entry = next(
                        (entry for entry in playlist.entries if entry.id == entry_id),
                        None,
                    )
                    if authored_entry is not None and authored_entry.path in known_paths:
                        source = authored_entry.path
        if source is None:
            source = _unique_still_source(record.get("still"), items)
        if source is not None and source not in resolved:
            resolved.append(source)

    if len(display_playlists) > 1:
        label = "Multiple displays"
    elif display_playlists:
        label = next(iter(display_playlists.values()))
    else:
        label = truth.playlist
    return MediaPlayback(playlist=label, current=tuple(resolved))


def entry_is_taboo(
    status: Mapping[str, object] | None,
    playlist_id: str,
    entry_id: str,
) -> bool:
    """Whether one exact resolved runtime entry is in the atomic taboo inventory."""
    if status is None or not playlist_id or not entry_id:
        return False
    raw = status.get("taboo_entries")
    if not isinstance(raw, list):
        return False
    return any(
        isinstance(record, Mapping)
        and record.get("playlist_id") == playlist_id
        and record.get("entry_id") == entry_id
        for record in raw
    )


def current_renderer_failure_is_taboo(status: Mapping[str, object] | None) -> bool:
    """Tell controls when Play cannot start any current connected route.

    The explicit bits are complete even when the diagnostic inventory is
    capped, and do not depend on the renderer on this particular display being
    the process which discovered an equivalent media failure. Inventory lookup
    remains as backwards compatibility for older version-two services.
    """
    if status is None:
        return False
    if status.get("entry_taboo") is True:
        return True
    displays = status.get("displays")
    if isinstance(displays, list) and any(
        isinstance(record, Mapping)
        and record.get("connected") is not False
        and record.get("entry_taboo") is True
        for record in displays
    ):
        return True
    playlist_id = status.get("playlist_id")
    entry_id = status.get("entry_id")
    if (
        isinstance(playlist_id, str)
        and isinstance(entry_id, str)
        and entry_is_taboo(status, playlist_id, entry_id)
    ):
        return True
    if not isinstance(displays, list):
        return False
    return any(
        isinstance(record, Mapping)
        and record.get("connected") is not False
        and isinstance(record.get("playlist_id"), str)
        and isinstance(record.get("entry_id"), str)
        and entry_is_taboo(
            status,
            str(record.get("playlist_id")),
            str(record.get("entry_id")),
        )
        for record in displays
    )
