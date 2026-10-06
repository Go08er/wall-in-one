"""Edits and additions on a store file that is damaged, or can't be read at all.

Two different faults, on disposable files:

* Content this build can't use (bytes that aren't JSON). An addition keeps
  the designed recovery: the bytes are moved to ``.broken`` and a new file
  holds what was added. An edit of a record that isn't among what could be
  read is refused with ``unreadable``: whether the file holds it isn't known.
* A file that couldn't be read at all (mode 000: a native ``EACCES``). Nothing
  says its bytes are damaged, so no change moves it aside or writes over it:
  every edit and every addition is refused with ``unreadable`` and the file is
  left as it is.

`state_file.read_object` tells the two apart (`state_file.ReadFailure`).
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.test_store_unreadable_removals import (
    BROKEN,
    FAULTS,
    Made,
    _displays,
    _favourites,
    _pairings,
    _playlists,
    _schedule,
    corrupt,
    no_permissions,
)
from wall_in_one.library import displays, favourites, pairings, playlists, schedules, state_file
from wall_in_one.library.model import Kind, MediaItem


def _other(tmp_path: Path) -> Path:
    path = tmp_path / "wallpapers" / "other.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"y")
    return path


def _other_item(tmp_path: Path) -> MediaItem:
    return MediaItem(path=_other(tmp_path), kind=Kind.STILL, size=1, mtime=1)


def _entry(tmp_path: Path) -> playlists.Entry:
    return playlists.Entry("e2", str(_other(tmp_path)))


Call = Callable[[Made, Path], object]
Case = tuple[str, Callable[[Path], Made], Call, Callable[[], Path]]

#: Edits of a record that is there before the fault.
EDITS: list[Case] = [
    (
        "schedules.set_enabled",
        _schedule,
        lambda m, _t: m.store.set_enabled(m.record.id, False),
        schedules.state_path,
    ),
    (
        "schedules.set_name",
        _schedule,
        lambda m, _t: m.store.set_name(m.record.id, "x"),
        schedules.state_path,
    ),
    (
        "schedules.update",
        _schedule,
        lambda m, _t: m.store.update(m.record.id, "q"),
        schedules.state_path,
    ),
    ("schedules.copy", _schedule, lambda m, _t: m.store.copy(m.record.id), schedules.state_path),
    ("schedules.move", _schedule, lambda m, _t: m.store.move(m.record.id, 0), schedules.state_path),
    (
        "schedules.move_relative",
        _schedule,
        lambda m, _t: m.store.move_relative(m.record.id, 1),
        schedules.state_path,
    ),
    (
        "playlists.add",
        _playlists,
        lambda m, t: m.store.add(m.record, _other(t)),
        playlists.state_path,
    ),
    (
        "playlists.set_rotation",
        _playlists,
        lambda m, _t: m.store.set_rotation(m.record, cycle_interval=60),
        playlists.state_path,
    ),
    (
        "playlists.move_entry",
        _playlists,
        lambda m, _t: m.store.move_entry(m.record, "e1", 0),
        playlists.state_path,
    ),
    (
        "playlists.move_entry_relative",
        _playlists,
        lambda m, _t: m.store.move_entry_relative(m.record, "e1", 1),
        playlists.state_path,
    ),
    (
        "playlists.insert_entries",
        _playlists,
        lambda m, t: m.store.insert_entries(m.record, [(0, _entry(t))]),
        playlists.state_path,
    ),
]

#: Additions and upserts, with how to see that a recovered file holds them.
Holds = Callable[[Path], bool]
ADDITIONS: list[tuple[str, Callable[[Path], Made], Call, Callable[[], Path], Holds]] = [
    (
        "schedules.add",
        _schedule,
        lambda m, _t: m.store.add("q"),
        schedules.state_path,
        lambda _t: [rule.playlist for rule in schedules.Store.open().rules] == ["q"],
    ),
    (
        "displays.assign",
        _displays,
        lambda m, _t: m.store.assign("DP-3", "q"),
        displays.state_path,
        lambda _t: dict(displays.Store.open().all()) == {"DP-3": "q"},
    ),
    (
        "playlists.create",
        _playlists,
        lambda m, _t: m.store.create("New"),
        playlists.state_path,
        lambda _t: [found.name for found in playlists.Store.open().all()] == ["New"],
    ),
    (
        "playlists.set_singleton",
        _playlists,
        lambda m, t: m.store.set_singleton("quick-choice", "Quick choice", _other(t)),
        playlists.state_path,
        lambda _t: playlists.Store.open().get("quick-choice") is not None,
    ),
    (
        "playlists.set_display_singleton",
        _playlists,
        lambda m, t: m.store.set_display_singleton("DP-2", _other(t)),
        playlists.state_path,
        lambda _t: len(playlists.Store.open().all()) == 1,
    ),
    (
        "favourites.add",
        _favourites,
        lambda m, t: m.store.add(_other(t)),
        favourites.state_path,
        lambda t: favourites.Store.open().paths == frozenset({_other(t)}),
    ),
    (
        "favourites.add_many",
        _favourites,
        lambda m, t: m.store.add_many([_other(t)]),
        favourites.state_path,
        lambda t: favourites.Store.open().paths == frozenset({_other(t)}),
    ),
    (
        "pairings.choose_still",
        _pairings,
        lambda m, t: m.store.choose_still(_other_item(t), None),
        pairings.state_path,
        lambda t: pairings.Store.open().get(pairings.Identity.of(_other_item(t))) is not None,
    ),
    (
        "pairings.choose_palette",
        _pairings,
        lambda m, t: m.store.choose_palette(_other_item(t), pairings.PalettePolicy()),
        pairings.state_path,
        lambda t: pairings.Store.open().get(pairings.Identity.of(_other_item(t))) is not None,
    ),
    (
        "pairings.choose_mode",
        _pairings,
        lambda m, t: m.store.choose_mode(_other_item(t), pairings.Mode.LIGHT),
        pairings.state_path,
        lambda t: pairings.Store.open().get(pairings.Identity.of(_other_item(t))) is not None,
    ),
]


def _siblings(target: Path) -> list[str]:
    return sorted(entry.name for entry in target.parent.iterdir())


def _refused(call: Callable[[], object]) -> Exception:
    with pytest.raises(Exception) as refused:
        call()
    error = refused.value
    assert getattr(error, "kind", None) == "unreadable", error
    assert "can't be read" in str(error) and "Nothing was changed" in str(error), error
    return error


# -- read_object tells a read it couldn't make from content it can't use ---------------------


def test_a_file_that_cannot_be_opened_is_a_read_failure(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}", encoding="utf-8")
    with no_permissions(target):
        _document, fault = state_file.read_object(target, maximum_bytes=64, description="state")
    assert isinstance(fault, state_file.ReadFailure) and state_file.read_failed(fault)
    assert fault.startswith("could not read state.json")


def test_a_file_that_cannot_be_inspected_is_a_read_failure(tmp_path: Path) -> None:
    folder = tmp_path / "locked"
    folder.mkdir()
    target = folder / "state.json"
    target.write_text("{}", encoding="utf-8")
    folder.chmod(0o600)  # no search permission: lstat itself fails
    try:
        _document, fault = state_file.read_object(target, maximum_bytes=64, description="state")
    finally:
        folder.chmod(0o700)
    assert state_file.read_failed(fault) and fault is not None
    assert fault.startswith("could not inspect state.json")


def test_a_file_replaced_while_it_is_read_is_a_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}", encoding="utf-8")
    replacement = tmp_path / "replacement.json"
    replacement.write_text("{}", encoding="utf-8")
    real_open = os.open

    def opening(path: Any, flags: int, *rest: Any, **keywords: Any) -> int:
        if Path(path) == target and replacement.exists():
            os.replace(replacement, target)  # another writer, between lstat and open
        return real_open(path, flags, *rest, **keywords)

    monkeypatch.setattr(os, "open", opening)
    _document, fault = state_file.read_object(target, maximum_bytes=64, description="state")
    assert state_file.read_failed(fault) and fault == "state.json changed while it was being read"


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (lambda target: target.write_bytes(BROKEN), "is not readable JSON"),
        (lambda target: target.write_text("[]", encoding="utf-8"), "is not a state file"),
        (lambda target: target.write_bytes(b" " * 100), "is too large"),
        (lambda target: target.symlink_to(target.with_name("elsewhere.json")), "symbolic link"),
        (lambda target: target.mkdir(), "is not a regular state file"),
    ],
    ids=["not-json", "not-an-object", "too-large", "symlink", "directory"],
)
def test_content_it_cannot_use_is_not_a_read_failure(
    tmp_path: Path, make: Callable[[Path], object], says: str
) -> None:
    target = tmp_path / "state.json"
    target.with_name("elsewhere.json").write_text("{}", encoding="utf-8")
    make(target)
    _document, fault = state_file.read_object(target, maximum_bytes=64, description="state")
    assert fault is not None and says in fault
    assert not state_file.read_failed(fault)


# -- edits -----------------------------------------------------------------------------------


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize(("name", "setup", "call", "where"), EDITS, ids=[case[0] for case in EDITS])
def test_an_edit_in_a_file_that_cannot_be_read_says_so(
    name: str,
    setup: Callable[[Path], Made],
    call: Call,
    where: Callable[[], Path],
    fault: Callable[[Path], contextlib.AbstractContextManager[bytes]],
    tmp_path: Path,
) -> None:
    """Before: ``no-such-rule`` / ``no-such-playlist``, as if the record were gone."""
    made = setup(tmp_path)
    target = where()
    with fault(target) as held:
        before = _siblings(target)
        _refused(lambda: call(made, tmp_path))
        after = _siblings(target)
    assert target.read_bytes() == held
    assert after == before, "nothing moved aside, nothing written next to it"


def _partly_damaged() -> tuple[schedules.Store, schedules.Rule]:
    """A schedule file with one rule that reads and one that doesn't."""
    store = schedules.Store.open()
    kept = store.add("p", weekdays=("mon",))
    document = json.loads(schedules.state_path().read_text(encoding="utf-8"))
    document["rules"].append({"id": "lost", "playlist": 7})
    schedules.state_path().write_text(json.dumps(document), encoding="utf-8")
    reopened = schedules.Store.open()
    assert reopened.fault is not None and [rule.id for rule in reopened.rules] == [kept.id]
    return reopened, kept


def test_an_edit_of_a_record_that_did_not_read_says_the_file_is_damaged(tmp_path: Path) -> None:
    store, _kept = _partly_damaged()
    damaged = schedules.state_path().read_bytes()
    _refused(lambda: store.set_enabled("lost", False))
    assert schedules.state_path().read_bytes() == damaged


def test_an_edit_of_a_record_that_did_read_still_repairs_a_partly_damaged_file(
    tmp_path: Path,
) -> None:
    """A guard, unchanged: the designed repair, through any edit of what can be seen.

    A removal is refused instead (it must see every record); an edit of a rule
    that was read moves the damaged bytes aside and saves what could be read.
    """
    store, kept = _partly_damaged()
    damaged = schedules.state_path().read_bytes()
    _refused(lambda: store.remove(kept.id))
    store.set_enabled(kept.id, False)
    assert schedules.state_path().with_name("schedules.json.broken").read_bytes() == damaged
    repaired = schedules.Store.open()
    assert repaired.fault is None and [rule.enabled for rule in repaired.rules] == [False]


# -- additions -------------------------------------------------------------------------------

ADDITION_IDS = [case[0] for case in ADDITIONS]


@pytest.mark.parametrize(("name", "setup", "call", "where", "holds"), ADDITIONS, ids=ADDITION_IDS)
def test_an_addition_to_a_file_that_cannot_be_read_is_refused_and_moves_nothing(
    name: str,
    setup: Callable[[Path], Made],
    call: Call,
    where: Callable[[], Path],
    holds: Holds,
    tmp_path: Path,
) -> None:
    """Before: the unreadable file moved to ``.broken`` and a one-record file written."""
    made = setup(tmp_path)
    target = where()
    with no_permissions(target) as held:
        before = _siblings(target)
        _refused(lambda: call(made, tmp_path))
        after = _siblings(target)
    assert target.read_bytes() == held
    assert after == before, "nothing moved aside, nothing written next to it"


@pytest.mark.parametrize(("name", "setup", "call", "where", "holds"), ADDITIONS, ids=ADDITION_IDS)
def test_an_addition_to_damaged_content_still_recovers_it(
    name: str,
    setup: Callable[[Path], Made],
    call: Call,
    where: Callable[[], Path],
    holds: Holds,
    tmp_path: Path,
) -> None:
    """A guard, unchanged: the bytes moved to ``.broken``, a new file holding the addition."""
    made = setup(tmp_path)
    target = where()
    with corrupt(target):
        call(made, tmp_path)
    assert target.with_name(target.name + ".broken").read_bytes() == BROKEN
    assert holds(tmp_path)


@pytest.mark.parametrize("fault", FAULTS)
def test_runtime_health_keeps_refusing_any_fault(
    fault: Callable[[Path], contextlib.AbstractContextManager[bytes]], tmp_path: Path
) -> None:
    """A guard, unchanged: an unattended finding never rewrites a faulted file."""
    made = _pairings(tmp_path)
    target = pairings.state_path()
    with fault(target) as held, pytest.raises(pairings.PairingError) as refused:
        made.store.mark_borked(_other_item(tmp_path), "renderer exited", "test")
    assert refused.value.kind == "invalid-state"
    assert target.read_bytes() == held
