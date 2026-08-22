"""App-owned persistence seam for Rust runtime compatibility findings.

Rust owns the live skip set and reports stable playlist/entry identities in
one atomic status snapshot. This module maps those identities back to Python
authoring without importing GTK. Both the GUI and the headless health sync use
it, so persistence cannot quietly depend on a window being open.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from wall_in_one import runtime_config
from wall_in_one.library.model import MediaItem
from wall_in_one.library.playlists import Playlist


class RuntimeHealthError(Exception):
    """A runtime status reply is not a trustworthy atomic snapshot."""


@dataclass(frozen=True)
class TabooReport:
    """One stable runtime entry mapped back to its library wallpaper."""

    item: MediaItem
    playlist_id: str
    entry_id: str
    reason: str
    source: str


@dataclass(frozen=True)
class TabooInventory:
    reports: tuple[TabooReport, ...]
    omitted: int


def parse_status(message: str) -> dict[str, object]:
    """Decode one Rust snapshot strictly before any authoring write occurs."""
    try:
        decoded: object = json.loads(message)
    except ValueError as error:
        raise RuntimeHealthError("runtime status was not valid JSON") from error
    if not isinstance(decoded, dict):
        raise RuntimeHealthError("runtime status was not a JSON object")
    if not isinstance(decoded.get("playlist"), str) or decoded.get("source") not in (
        "manual",
        "schedule",
    ):
        raise RuntimeHealthError("runtime status has no valid playlist state")

    raw_omitted = decoded.get("taboo_entries_omitted", 0)
    if type(raw_omitted) is not int or raw_omitted < 0:
        raise RuntimeHealthError("runtime status has an invalid taboo omitted count")
    raw_reports = decoded.get("taboo_entries", [])
    if not isinstance(raw_reports, list):
        raise RuntimeHealthError("runtime status has an invalid taboo inventory")
    for raw in raw_reports:
        if not isinstance(raw, Mapping) or not all(
            isinstance(raw.get(field), str) and raw.get(field)
            for field in ("playlist_id", "entry_id", "reason", "source")
        ):
            raise RuntimeHealthError("runtime status has a malformed taboo entry")
    return decoded


def taboo_inventory(
    status: Mapping[str, object] | None,
    authored_playlists: Iterable[Playlist],
    media: Iterable[MediaItem],
) -> TabooInventory:
    """Map Rust's bounded taboo inventory to app-owned media identities.

    Named entries use their stable authored id. The generated All-media list
    uses the same stable source hash as playback truth. Missing and malformed
    reports are ignored here so the GUI can retain its last coherent view; the
    headless writer calls :func:`parse_status` first and therefore fails closed.
    Absence is never interpreted as recovery because Rust may have omitted
    older records to keep its atomic status bounded.
    """
    omitted = 0
    if status is None:
        return TabooInventory((), omitted)
    raw_omitted = status.get("taboo_entries_omitted")
    if type(raw_omitted) is int and raw_omitted > 0:
        omitted = raw_omitted

    items = tuple(media)
    by_path = {item.path: item for item in items}
    authored = {playlist.id: playlist for playlist in authored_playlists}
    generated: dict[str, MediaItem | None] = {}
    for item in items:
        identifier = runtime_config.entry_id_for_source(item.path)
        if identifier not in generated:
            generated[identifier] = item
        elif generated[identifier] != item:
            generated[identifier] = None

    raw_reports = status.get("taboo_entries")
    if not isinstance(raw_reports, list):
        return TabooInventory((), omitted)
    reports: list[TabooReport] = []
    seen: set[Path] = set()
    for raw in raw_reports:
        if not isinstance(raw, Mapping):
            continue
        playlist_id = raw.get("playlist_id")
        entry_id = raw.get("entry_id")
        reason = raw.get("reason")
        source = raw.get("source")
        if not all(
            isinstance(value, str) and value for value in (playlist_id, entry_id, reason, source)
        ):
            continue
        assert isinstance(playlist_id, str)
        assert isinstance(entry_id, str)
        assert isinstance(reason, str)
        assert isinstance(source, str)

        mapped: MediaItem | None = None
        if playlist_id == runtime_config.FALLBACK_PLAYLIST_ID:
            mapped = generated.get(entry_id)
        else:
            playlist = authored.get(playlist_id)
            if playlist is not None:
                entry = next((entry for entry in playlist.entries if entry.id == entry_id), None)
                if entry is not None:
                    mapped = by_path.get(entry.path)
        if mapped is None or mapped.path in seen:
            continue
        seen.add(mapped.path)
        reports.append(TabooReport(mapped, playlist_id, entry_id, reason, source))
    return TabooInventory(tuple(reports), omitted)
