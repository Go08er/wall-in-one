"""No store writes a file its own reader would refuse as too large.

0.2.1b review H-3: playlists.json is read with an 8 MiB ceiling, but its
writer never checked the final serialization. A valid near-limit profile plus
one ordinary add produced a file the next open refused ("too large"); later
edits failed and recovery moved it aside as ``.broken``. Each store now
renders its document first and refuses (kind ``full``) a UTF-8 serialization
past its reader's budget, before any backup, move or write, leaving the file
and the Store's memory as they were.

The tests use a reduced byte budget, the module constant both the reader and
the writer consult, set just above a real file's size.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from wall_in_one import config
from wall_in_one.library import displays, favourites, pairings, playlists, schedules
from wall_in_one.library.model import Kind, MediaItem

#: Room for the next small edit to succeed, not for the big one.
HEADROOM = 40


def _no_temporaries(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir() if entry.name.endswith(".tmp"))


def _picture(directory: Path, index: int) -> Path:
    return directory / f"wallpaper-{index:04d}-with-a-longer-name-than-most.png"


def test_a_playlist_add_past_the_readers_budget_is_refused_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex's case through public Store operations, at a reduced budget."""
    target = tmp_path / "playlists.json"
    store = playlists.Store.open(target)
    evening = store.create("Evening")
    for index in range(20):
        store.add(evening.id, _picture(tmp_path, index))
    monkeypatch.setattr(playlists, "MAX_STATE_BYTES", target.stat().st_size + HEADROOM)
    assert playlists.Store.open(target).fault is None, "the near-limit file is valid"
    before = target.read_bytes()
    in_memory = store.get(evening.id)
    assert in_memory is not None

    with pytest.raises(playlists.PlaylistError) as caught:
        store.add(evening.id, _picture(tmp_path, 9999))

    assert caught.value.kind == "full"
    message = str(caught.value)
    assert message.startswith("full: playlists.json would be "), message
    assert "more than the" in message and "bytes this version can read back" in message
    assert message.endswith("remove some playlist entries first. Nothing was changed.")
    assert target.read_bytes() == before, "the valid generation stays"
    assert store.get(evening.id) == in_memory and store.fault is None
    assert _no_temporaries(tmp_path) == []
    reopened = playlists.Store.open(target)
    assert reopened.fault is None and reopened.get(evening.id) == in_memory
    assert not list(tmp_path.glob("playlists.json.broken*"))

    # An ordinary smaller edit still works, and frees the room for the add.
    shorter = reopened.remove_entry(evening.id, in_memory.entries[0].id)
    assert len(shorter) == len(in_memory) - 1
    assert len(store.add(evening.id, _picture(tmp_path, 9999))) == len(in_memory)
    assert playlists.Store.open(target).fault is None


def test_a_refused_bump_keeps_no_backup_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The document is measured before the one-time backup that precedes a
    version bump, so a refused edit leaves the directory exactly as it was."""
    target = tmp_path / "playlists.json"
    store = playlists.Store.open(target)
    evening = store.create("Evening")
    store.add(evening.id, _picture(tmp_path, 0))
    monkeypatch.setattr(playlists, "MAX_STATE_BYTES", target.stat().st_size + 5)
    listing = sorted(entry.name for entry in tmp_path.iterdir())

    with pytest.raises(playlists.PlaylistError) as caught:
        store.set_rotation(evening.id, cycle_interval=60)  # needs version 2

    assert caught.value.kind == "full"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == listing, "no .v1-backup"
    assert store.get(evening.id) == playlists.Store.open(target).get(evening.id)


def _schedules(directory: Path) -> tuple[Path, Callable[[int], object], Callable[[], object]]:
    target = directory / "schedules.json"
    store = schedules.Store.open(target)
    return (
        target,
        lambda index: store.add(f"playlist-{index:04d}", start="06:00", end="07:00"),
        lambda: store.rules,
    )


def _displays(directory: Path) -> tuple[Path, Callable[[int], object], Callable[[], object]]:
    target = directory / "displays.json"
    store = displays.Store.open(target)
    return (
        target,
        lambda index: store.assign(f"HDMI-A-{index}", f"playlist-{index:04d}"),
        store.all,
    )


def _favourites(directory: Path) -> tuple[Path, Callable[[int], object], Callable[[], object]]:
    target = directory / "favourites.json"
    store = favourites.Store.open(target)
    return target, lambda index: store.add(_picture(directory, index)), lambda: store.paths


def _pairings(directory: Path) -> tuple[Path, Callable[[int], object], Callable[[], object]]:
    target = directory / "pairings.json"
    store = pairings.Store.open(target)

    def choose(index: int) -> object:
        item = MediaItem(_picture(directory, index), Kind.STILL, 1, 1)
        return store.choose_palette(item, pairings.PalettePolicy(kind="builtin", name="Nord"))

    return target, choose, lambda: dict(store.records)


STORES: dict[str, tuple[ModuleType, Any, str]] = {
    "schedules": (schedules, _schedules, "remove some schedule rules"),
    "displays": (displays, _displays, "clear some display assignments"),
    "favourites": (favourites, _favourites, "unstar some wallpapers"),
    "pairings": (pairings, _pairings, "reset some pairings"),
}


@pytest.mark.parametrize("name", STORES)
def test_every_store_refuses_a_document_its_reader_would_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    module, make, remedy = STORES[name]
    target, edit, memory = make(tmp_path)
    for index in range(3):
        edit(index)
    monkeypatch.setattr(module, "MAX_STATE_BYTES", target.stat().st_size + 5)
    before, remembered = target.read_bytes(), memory()

    with pytest.raises(Exception) as caught:
        edit(99)

    assert getattr(caught.value, "kind", None) == "full", caught.value
    assert str(caught.value).endswith(f"{remedy} first. Nothing was changed.")
    assert target.read_bytes() == before
    assert memory() == remembered
    assert module.Store.open(target).fault is None
    assert _no_temporaries(tmp_path) == []


def test_settings_past_the_readers_budget_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "settings.toml"
    settings = config.Settings(roots=(tmp_path / "one",))
    config.save(settings, target)
    monkeypatch.setattr(config, "MAX_SETTINGS_BYTES", target.stat().st_size + 5)
    before = target.read_bytes()

    with pytest.raises(config.ConfigError) as caught:
        config.save(replace(settings, roots=(tmp_path / "one", tmp_path / "two")), target)

    assert str(caught.value).endswith("remove some library folders first. Nothing was changed.")
    assert target.read_bytes() == before
    assert config.load_strict(target).roots == settings.roots
