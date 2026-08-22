"""Small, validated view of the Rust runtime's atomic status snapshot."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from wall_in_one import runtime_config
from wall_in_one.library.model import MediaItem
from wall_in_one.library.playlists import Playlist


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

    @property
    def follows_schedule(self) -> bool:
        return self.source == "schedule"

    @property
    def is_manual(self) -> bool:
        return self.source == "manual"

    @property
    def is_multi_display(self) -> bool:
        """Whether connector assignments currently select several playlists."""
        return not self.playlist_id and len(self.active_playlist_ids) > 1

    def playlist_is_active(self, playlist_id: str) -> bool:
        return playlist_id in self.active_playlist_ids


@dataclass(frozen=True)
class MediaPlayback:
    """What Media can say and highlight from one runtime snapshot."""

    playlist: str
    current: tuple[Path, ...]


def from_status(status: Mapping[str, object] | None) -> RuntimeTruth | None:
    """Return one coherent playback view, or ``None`` for an invalid snapshot."""
    if status is None:
        return None
    playlist_id = status.get("playlist_id")
    playlist = status.get("playlist")
    source = status.get("source")
    if (
        not isinstance(playlist_id, str)
        or not isinstance(playlist, str)
        or not playlist
        or source not in ("manual", "schedule")
    ):
        return None

    active_playlist_ids: list[str] = []
    reported_playlists = status.get("playlists")
    if isinstance(reported_playlists, list):
        for record in reported_playlists:
            if not isinstance(record, Mapping) or record.get("active") is not True:
                continue
            identifier = record.get("id")
            if isinstance(identifier, str) and identifier and identifier not in active_playlist_ids:
                active_playlist_ids.append(identifier)
    if playlist_id:
        if playlist_id not in active_playlist_ids:
            active_playlist_ids.append(playlist_id)
    elif source == "manual" or playlist != "Multiple displays" or len(active_playlist_ids) < 2:
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
    )


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
        records = tuple(record for record in reported_displays if isinstance(record, Mapping))
    if not records:
        records = (status,)

    resolved: list[Path] = []
    display_playlist_ids: set[str] = set()
    for record in records:
        playlist_id = record.get("playlist_id")
        entry_id = record.get("entry_id")
        if isinstance(playlist_id, str) and playlist_id:
            display_playlist_ids.add(playlist_id)

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

    label = "Multiple displays" if len(display_playlist_ids) > 1 else truth.playlist
    return MediaPlayback(playlist=label, current=tuple(resolved))
