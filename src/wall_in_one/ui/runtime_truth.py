"""Small, validated view of the Rust runtime's atomic status snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


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
