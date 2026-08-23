"""Small, validated view of the Rust runtime's atomic status snapshot."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from wall_in_one import runtime_config
from wall_in_one.library.model import MediaItem
from wall_in_one.library.playlists import Playlist


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

    parsed_displays = _display_truth(status.get("displays"), required=status_version == 2)
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
    )


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
            )
        )
    return tuple(parsed)


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
