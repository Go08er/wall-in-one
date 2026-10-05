"""A write that published, then couldn't sync its folder, is a save (audit B-2's rule).

`state_file.write_atomic_bytes` renames the new bytes into place before it
syncs the folder; a failure of that sync raises
`state_file.PublishedNotDurableError` with the new bytes already live. Each
store wrapped it in its own local-io error inside its transaction, so memory
kept the old state and the caller said nothing changed while every reader,
the next process included, saw the new file. B-2 fixed this for
``settings.toml``; these pin it for every other writer: the store adopts
what it published, the caller gets the new state, and the uncertainty is
reported (here, to the log), never raised as a refusal.

The sync is failed only for the one file under test, so a backup or claim
that happens to share the folder can't make a test pass for the wrong reason.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.test_tidy import _noctalia_settings
from wall_in_one import paths, tidy, ui_prefs
from wall_in_one.library import (
    displays,
    favourites,
    pairings,
    playlists,
    removals,
    schedules,
    state_file,
)
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.providers import credentials, registry
from wall_in_one.theme import noctalia, palettes

A = Path("/w/a.png")
B = Path("/w/b.png")
SAVED_BUT = "was saved, but syncing its folder failed"


def _item(path: Path) -> MediaItem:
    return MediaItem(path, Kind.STILL, 7, 1)


def _sync_fails_for(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Path]:
    """Fail the folder sync after ``name`` is renamed into place, and only that one."""
    real = state_file.fsync_parent
    failed: list[Path] = []

    def sync(path: Path) -> None:
        if Path(path).name == name:
            failed.append(Path(path))
            raise OSError(errno.EIO, "injected post-replace directory sync failure")
        real(path)

    monkeypatch.setattr(state_file, "fsync_parent", sync)
    # Uncollected warnings go to the log unless an application installed a sink.
    monkeypatch.setattr(state_file, "_durability_sink", None, raising=False)
    return failed


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if SAVED_BUT in record.getMessage()]


@dataclass(frozen=True)
class Case:
    name: str
    filename: str
    open: Callable[[Path], Any]
    current: dict[str, Any]
    mutate: Callable[[Any], object]
    #: What the store holds, comparable between memory and a fresh open.
    held: Callable[[Any], object]


CASES = (
    Case(
        "playlists",
        "playlists.json",
        playlists.Store.open,
        {"version": 1, "playlists": [{"id": "x", "name": "X", "entries": []}]},
        lambda store: store.create("Evening"),
        lambda store: tuple((playlist.id, playlist.name) for playlist in store.all()),
    ),
    Case(
        "schedules",
        "schedules.json",
        schedules.Store.open,
        {"version": 2, "rules": [{"id": "a", "playlist": "X"}]},
        lambda store: store.add("Y", rule_id="b"),
        lambda store: tuple((rule.id, rule.playlist) for rule in store.rules),
    ),
    Case(
        "displays",
        "displays.json",
        displays.Store.open,
        {"version": 1, "displays": {"DP-1": "X"}},
        lambda store: store.assign("DP-2", "Y"),
        lambda store: store.all(),
    ),
    Case(
        "pairings",
        "pairings.json",
        pairings.Store.open,
        {"version": 2, "pairings": [{"identity": f"still:{A}", "palette": "builtin:Ocean"}]},
        lambda store: store.choose_still(_item(A), B),
        lambda store: dict(store.records),
    ),
    Case(
        "favourites",
        "favourites.json",
        favourites.Store.open,
        {"version": 1, "paths": [str(A)]},
        lambda store: store.add(B),
        lambda store: store.paths,
    ),
    Case(
        "pending removals",
        "pending-removals.json",
        removals.Store.open,
        {"version": 1, "removals": []},
        lambda store: store.record_external(_item(A), (Path("/w"),)),
        lambda store: store.records,
    ),
)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_store_keeps_what_it_published_when_the_folder_sync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: Case,
) -> None:
    target = tmp_path / "state" / case.filename
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(case.current, indent=2) + "\n", encoding="utf-8")
    store = case.open(target)
    before = case.held(store)
    failed = _sync_fails_for(monkeypatch, case.filename)
    caplog.set_level(logging.WARNING)

    case.mutate(store)  # before: "could not write ...", with memory left behind

    assert failed == [target], "the injected failure was the store's own publication"
    on_disk = case.held(case.open(target))
    assert on_disk != before, "the new bytes are live"
    assert case.held(store) == on_disk, "memory is what every reader sees"
    assert store.fault is None
    assert len(_warnings(caplog)) == 1 and case.filename in _warnings(caplog)[0]


def test_window_preferences_published_before_the_sync_failed_are_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "config" / "ui.toml"
    ui_prefs.update({"tidy_offer_dismissed": False}, target)
    failed = _sync_fails_for(monkeypatch, "ui.toml")
    caplog.set_level(logging.WARNING)

    saved = ui_prefs.update({"tidy_offer_dismissed": True}, target)  # before: UiPrefsError

    assert failed == [target]
    assert saved.prefs.tidy_offer_dismissed is True
    assert ui_prefs.load(target).prefs.tidy_offer_dismissed is True
    assert len(_warnings(caplog)) == 1 and "ui.toml" in _warnings(caplog)[0]


_VARIANT = {key: "#203040" for key, _label in palettes.EDITABLE_KEYS}


def _palette() -> dict[str, object]:
    return {"dark": dict(_VARIANT), "light": dict(_VARIANT)}


@pytest.mark.parametrize("tmpfile", [True, False], ids=["linked", "renamed"])
def test_a_new_palette_published_before_the_sync_failed_is_saved(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmpfile: bool
) -> None:
    """Both publications: the ``O_TMPFILE`` link, and the rename used without it."""
    if not tmpfile:
        real_open = os.open

        def no_tmpfile(path: Any, flags: int, *arguments: Any, **keywords: Any) -> int:
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                raise OSError(errno.EOPNOTSUPP, "no O_TMPFILE here")
            return real_open(path, flags, *arguments, **keywords)

        monkeypatch.setattr(os, "open", no_tmpfile)
    failed = _sync_fails_for(monkeypatch, "Mine.json")
    caplog.set_level(logging.WARNING)

    entry = palettes.write_custom("Mine", _palette())  # before: PaletteWriteError

    assert failed == [palettes.custom_path("Mine")]
    assert entry.path is not None and entry.path.is_file()
    assert palettes.parse_document(entry.path.read_bytes()) == entry.colours
    assert len(_warnings(caplog)) == 1 and "Mine.json" in _warnings(caplog)[0]


def test_an_edited_palette_published_before_the_sync_failed_is_saved(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    entry = palettes.write_custom("Mine", _palette())
    edited = _palette()
    edited["dark"] = {**_VARIANT, "mPrimary": "#ff8800"}
    failed = _sync_fails_for(monkeypatch, "Mine.json")
    caplog.set_level(logging.WARNING)

    saved = palettes.save_edits(entry, edited)  # before: PaletteWriteError

    assert failed == [entry.path]
    assert saved.path is not None
    assert palettes.parse_document(saved.path.read_bytes()) == saved.colours
    assert saved.colours != entry.colours
    assert len(_warnings(caplog)) == 1


def test_a_tidy_journal_published_before_the_sync_failed_carries_the_action_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The manifest is Undo's record: live, it is the record, so the edit goes on."""
    settings = _noctalia_settings()
    original = settings.read_bytes()
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)
    plan = tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS)
    assert plan.ready, plan.blocked
    failed = _sync_fails_for(monkeypatch, tidy.MANIFEST)
    caplog.set_level(logging.WARNING)

    result = tidy.apply(tidy.PLUGIN_SETTINGS, plan)  # before: the first journal write raised

    assert failed, "the journal's own publication was the one that failed"
    assert result.changed and settings.read_bytes() != original
    (archive,) = tidy.archive_root().iterdir()
    assert json.loads((archive / tidy.MANIFEST).read_bytes())["state"] == "applied"
    assert _warnings(caplog) and all(tidy.MANIFEST in warning for warning in _warnings(caplog))
    undone = tidy.undo(tidy.PLUGIN_SETTINGS)
    assert undone.changed and settings.read_bytes() == original


def _key_folder_sync_fails(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    real = paths.fsync_directory
    failed: list[Path] = []
    folder = credentials.key_path().parent

    def sync(directory: Path) -> None:
        if Path(directory) == folder:
            failed.append(Path(directory))
            raise OSError(errno.EIO, "injected post-replace directory sync failure")
        real(directory)

    monkeypatch.setattr(paths, "fsync_directory", sync)
    monkeypatch.setattr(state_file, "_durability_sink", None, raising=False)
    return failed


def test_a_provider_key_published_before_the_sync_failed_is_saved(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(registry.API_KEY_VARIABLE, raising=False)
    credentials.save_key("Abc123_key-value")
    failed = _key_folder_sync_fails(monkeypatch)
    caplog.set_level(logging.WARNING)

    saved = credentials.save_key("Zzz999_other-key")  # before: ProviderError local-io

    assert failed and saved == credentials.key_path()
    assert registry.wallhaven_api_key() == "Zzz999_other-key"
    assert len(_warnings(caplog)) == 1
    assert "Zzz999" not in caplog.text, "a key never appears in a message"


def test_a_provider_key_removed_before_the_sync_failed_is_removed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(registry.API_KEY_VARIABLE, raising=False)
    credentials.save_key("Abc123_key-value")
    failed = _key_folder_sync_fails(monkeypatch)
    caplog.set_level(logging.WARNING)

    assert credentials.clear_key() is True  # before: ProviderError local-io

    assert failed and not credentials.key_path().exists()
    assert any("was removed, but syncing its folder failed" in m for m in caplog.messages)
