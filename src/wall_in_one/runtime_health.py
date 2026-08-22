"""App-owned persistence seam for Rust runtime compatibility findings.

Rust owns the live skip set and reports stable playlist/entry identities in
one atomic status snapshot. This module maps those identities back to Python
authoring without importing GTK. Both the GUI and the headless health sync use
it, so persistence cannot quietly depend on a window being open.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from wall_in_one import runtime_config
from wall_in_one.library.model import MediaItem
from wall_in_one.library.playlists import Playlist

RUNTIME_INSTANCE_HEX_CHARS = 32
MAX_RUNTIME_EPOCH = (1 << 64) - 1


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
    durable: bool
    observed_config_epoch: int


@dataclass(frozen=True)
class TabooInventory:
    reports: tuple[TabooReport, ...]
    omitted: int
    unmapped: int
    stale: int


def _is_generation(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == runtime_config.CONFIG_GENERATION_HEX_CHARS
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_epoch(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_RUNTIME_EPOCH


def parse_status(message: str) -> dict[str, object]:
    """Decode one Rust snapshot strictly before any authoring write occurs."""
    try:
        decoded: object = json.loads(message)
    except ValueError as error:
        raise RuntimeHealthError("runtime status was not valid JSON") from error
    if not isinstance(decoded, dict):
        raise RuntimeHealthError("runtime status was not a JSON object")
    generation = decoded.get("config_generation")
    if not _is_generation(generation):
        raise RuntimeHealthError("runtime status has no valid configuration generation")
    runtime_instance = decoded.get("runtime_instance")
    if (
        not isinstance(runtime_instance, str)
        or len(runtime_instance) != RUNTIME_INSTANCE_HEX_CHARS
        or any(character not in "0123456789abcdef" for character in runtime_instance)
    ):
        raise RuntimeHealthError("runtime status has no valid service instance identity")
    if not _is_epoch(decoded.get("config_epoch")):
        raise RuntimeHealthError("runtime status has no valid configuration epoch")
    config_path = decoded.get("config_path")
    if (
        not isinstance(config_path, str)
        or not Path(config_path).is_absolute()
        or any(ord(character) < 32 or ord(character) == 127 for character in config_path)
    ):
        raise RuntimeHealthError("runtime status has no valid configuration path")
    # Playback source may be "mixed" when independent displays disagree. It
    # is deliberately not part of the health-persistence trust boundary.
    source = decoded.get("source")
    if source is not None and source not in ("manual", "schedule", "mixed"):
        raise RuntimeHealthError("runtime status has an invalid playback source")

    raw_omitted = decoded.get("taboo_entries_omitted", 0)
    if type(raw_omitted) is not int or raw_omitted < 0:
        raise RuntimeHealthError("runtime status has an invalid taboo omitted count")
    raw_reports = decoded.get("taboo_entries", [])
    if not isinstance(raw_reports, list):
        raise RuntimeHealthError("runtime status has an invalid taboo inventory")
    for raw in raw_reports:
        if (
            not isinstance(raw, Mapping)
            or not all(
                isinstance(raw.get(field), str) and raw.get(field)
                for field in (
                    "playlist_id",
                    "entry_id",
                    "reason",
                    "source",
                )
            )
            or not _is_epoch(raw.get("observed_config_epoch"))
            or type(raw.get("durable")) is not bool
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
    unmapped = 0
    stale = 0
    if status is None:
        return TabooInventory((), omitted, unmapped, stale)
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
        return TabooInventory((), omitted, unmapped, stale)
    status_epoch = status.get("config_epoch")
    reports: list[TabooReport] = []
    report_indexes: dict[Path, int] = {}
    for raw in raw_reports:
        if not isinstance(raw, Mapping):
            continue
        playlist_id = raw.get("playlist_id")
        entry_id = raw.get("entry_id")
        reason = raw.get("reason")
        source = raw.get("source")
        durable = raw.get("durable")
        observed_config_epoch = raw.get("observed_config_epoch")
        if (
            not all(
                isinstance(value, str) and value
                for value in (playlist_id, entry_id, reason, source)
            )
            or not _is_epoch(observed_config_epoch)
            or type(durable) is not bool
        ):
            continue
        assert isinstance(playlist_id, str)
        assert isinstance(entry_id, str)
        assert isinstance(reason, str)
        assert isinstance(source, str)
        assert isinstance(durable, bool)
        assert isinstance(observed_config_epoch, int)
        if observed_config_epoch != status_epoch:
            stale += 1
            continue

        mapped: MediaItem | None = None
        if playlist_id == runtime_config.FALLBACK_PLAYLIST_ID:
            mapped = generated.get(entry_id)
        else:
            playlist = authored.get(playlist_id)
            if playlist is not None:
                entry = next((entry for entry in playlist.entries if entry.id == entry_id), None)
                if entry is not None:
                    mapped = by_path.get(entry.path)
        if mapped is None:
            unmapped += 1
            continue
        previous_index = report_indexes.get(mapped.path)
        if previous_index is not None:
            # One wallpaper may occur several times. It is fully durable only
            # when every visible occurrence is; otherwise a reload is still
            # needed to apply the media-level authoring marker everywhere.
            if not durable and reports[previous_index].durable:
                reports[previous_index] = replace(reports[previous_index], durable=False)
            continue
        report_indexes[mapped.path] = len(reports)
        reports.append(
            TabooReport(
                mapped,
                playlist_id,
                entry_id,
                reason,
                source,
                durable,
                observed_config_epoch,
            )
        )
    return TabooInventory(tuple(reports), omitted, unmapped, stale)
