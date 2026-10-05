"""Removing from a store file that can't be read is refused, never a silent no-op.

Every removal, unassign, rename and retarget across the five authoring stores
reads the file under its lock first. When that file can't be read, which
records it holds isn't known, so nothing can be said to be gone: the store
refuses with ``unreadable`` and leaves the file, byte for byte, where it is.
Two faults, both on disposable files: bytes that aren't JSON, and a file with
no permissions (a native ``EACCES``). Adding to such a file keeps its designed
recovery (the bytes moved aside, a new file written); see each store's tests.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from wall_in_one.library import displays, favourites, pairings, playlists, schedules
from wall_in_one.library.model import Kind, MediaItem

BROKEN = b"{ not json"


@contextlib.contextmanager
def corrupt(target: Path) -> Iterator[bytes]:
    """``target`` holding bytes that aren't JSON; yields what it must still hold."""
    target.write_bytes(BROKEN)
    yield BROKEN


@contextlib.contextmanager
def no_permissions(target: Path) -> Iterator[bytes]:
    """``target`` with mode 000, a native read fault; put back afterwards."""
    held = target.read_bytes()
    mode = target.stat().st_mode
    target.chmod(0)
    try:
        yield held
    finally:
        target.chmod(mode)


FAULTS = [pytest.param(corrupt, id="corrupt"), pytest.param(no_permissions, id="mode-000")]


@dataclass
class Made:
    store: Any
    record: Any = None
    path: Path = Path()


def _item(tmp_path: Path) -> MediaItem:
    path = tmp_path / "wallpapers" / "w.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return MediaItem(path=path, kind=Kind.STILL, size=1, mtime=1)


def _schedule(_tmp_path: Path) -> Made:
    store = schedules.Store.open()
    return Made(store, store.add("p", connector="DP-2"))


def _displays(_tmp_path: Path) -> Made:
    store = displays.Store.open()
    store.assign("DP-2", "p")
    store.set_beats_global_rules("DP-2", True)
    return Made(store)


def _playlists(tmp_path: Path) -> Made:
    store = playlists.Store.open()
    path = _item(tmp_path).path
    made = store.create("P", entries=[playlists.Entry("e1", str(path))])
    return Made(store, made.id, path)


def _favourites(tmp_path: Path) -> Made:
    store = favourites.Store.open()
    path = _item(tmp_path).path
    store.add(path)
    return Made(store, None, path)


def _pairings(tmp_path: Path) -> Made:
    store = pairings.Store.open()
    item = _item(tmp_path)
    store.choose_mode(item, pairings.Mode.DARK)
    store.mark_borked(item, "renderer exited", "test")
    return Made(store, item, item.path)


Case = tuple[str, Callable[[Path], Made], Callable[[Made], object], Callable[[], Path]]
CASES: list[Case] = [
    ("schedules.remove", _schedule, lambda m: m.store.remove(m.record.id), schedules.state_path),
    (
        "schedules.remove_for_display",
        _schedule,
        lambda m: m.store.remove_for_display("DP-2"),
        schedules.state_path,
    ),
    (
        "schedules.forget_playlist",
        _schedule,
        lambda m: m.store.forget_playlist("p"),
        schedules.state_path,
    ),
    ("schedules.retarget", _schedule, lambda m: m.store.retarget("p", "q"), schedules.state_path),
    ("displays.unassign", _displays, lambda m: m.store.unassign("DP-2"), displays.state_path),
    (
        "displays.forget_display",
        _displays,
        lambda m: m.store.forget_display("DP-2"),
        displays.state_path,
    ),
    (
        "displays.forget_playlist",
        _displays,
        lambda m: m.store.forget_playlist("p"),
        displays.state_path,
    ),
    ("displays.retarget", _displays, lambda m: m.store.retarget("p", "q"), displays.state_path),
    (
        "displays.set_beats_global_rules(off)",
        _displays,
        lambda m: m.store.set_beats_global_rules("DP-2", False),
        displays.state_path,
    ),
    ("playlists.delete", _playlists, lambda m: m.store.delete(m.record), playlists.state_path),
    (
        "playlists.remove_entry",
        _playlists,
        lambda m: m.store.remove_entry(m.record, "e1"),
        playlists.state_path,
    ),
    (
        "playlists.remove_entries",
        _playlists,
        lambda m: m.store.remove_entries(m.record, ["e1"]),
        playlists.state_path,
    ),
    (
        "playlists.forget_path",
        _playlists,
        lambda m: m.store.forget_path(m.path),
        playlists.state_path,
    ),
    ("playlists.rename", _playlists, lambda m: m.store.rename(m.record, "Q"), playlists.state_path),
    ("favourites.discard", _favourites, lambda m: m.store.discard(m.path), favourites.state_path),
    ("favourites.toggle", _favourites, lambda m: m.store.toggle(m.path), favourites.state_path),
    (
        "pairings.clear_borked",
        _pairings,
        lambda m: m.store.clear_borked(m.record),
        pairings.state_path,
    ),
    ("pairings.reset", _pairings, lambda m: m.store.reset(m.record), pairings.state_path),
    (
        "pairings.forget_identity",
        _pairings,
        lambda m: m.store.forget_identity(pairings.Identity.of(m.record)),
        pairings.state_path,
    ),
    (
        "pairings.forget_item",
        _pairings,
        lambda m: m.store.forget_item(m.record),
        pairings.state_path,
    ),
    (
        "pairings.forget_path",
        _pairings,
        lambda m: m.store.forget_path(m.path),
        pairings.state_path,
    ),
]


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize(("name", "setup", "call", "where"), CASES, ids=[case[0] for case in CASES])
def test_a_removal_from_an_unreadable_file_is_refused_and_leaves_it_alone(
    name: str,
    setup: Callable[[Path], Made],
    call: Callable[[Made], object],
    where: Callable[[], Path],
    fault: Callable[[Path], contextlib.AbstractContextManager[bytes]],
    tmp_path: Path,
) -> None:
    made = setup(tmp_path)
    target = where()
    with fault(target) as held:
        before = sorted(entry.name for entry in target.parent.iterdir())
        with pytest.raises(Exception) as refused:
            call(made)
        after = sorted(entry.name for entry in target.parent.iterdir())
    assert getattr(refused.value, "kind", None) == "unreadable", refused.value
    assert "can't be read" in str(refused.value), refused.value
    assert "Nothing was changed" in str(refused.value), refused.value
    assert target.read_bytes() == held, "the file as it was"
    assert after == before, "nothing moved aside, nothing written next to it"


def test_an_unstar_never_stars_or_moves_the_list_aside(tmp_path: Path) -> None:
    """Before: an unstar on an unparseable list starred, and moved the list aside."""
    made = _favourites(tmp_path)
    favourites.state_path().write_bytes(BROKEN)
    with pytest.raises(favourites.FavouritesError):
        made.store.toggle(made.path)
    assert favourites.state_path().read_bytes() == BROKEN
    assert [entry.name for entry in favourites.state_path().parent.iterdir()].count(
        "favourites.json"
    ) == 1
    assert not list(favourites.state_path().parent.glob("favourites.json.broken*"))


def test_adding_to_an_unreadable_file_still_recovers_it(tmp_path: Path) -> None:
    """A guard, unchanged by the refusals: an addition moves the bytes aside, writes anew."""
    _schedule(tmp_path)
    schedules.state_path().write_bytes(BROKEN)
    schedules.Store.open().add("q")
    assert [rule.playlist for rule in schedules.Store.open().rules] == ["q"]


def test_retargeting_several_references_is_one_save_per_store_and_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saves: list[str] = []
    for module in (schedules, displays):
        real = module.Store._mutate

        def counted(store: Any, change: Any, *, _real: Any = real, **keywords: Any) -> Any:
            saves.append(type(store).__module__.rsplit(".", 1)[-1])
            return _real(store, change, **keywords)

        monkeypatch.setattr(module.Store, "_mutate", counted)
    store = schedules.Store.open()
    first = store.add("Old name", weekdays=("mon",))
    second = store.add("old id", weekdays=("tue",))
    other = store.add("else", weekdays=("wed",))
    assigned = displays.Store.open()
    assigned.assign("DP-1", "Old name")
    assigned.assign("DP-2", "old id")
    saves.clear()
    assert store.retarget_many(("Old name", "old id"), "pid") == 2
    assert assigned.retarget_many(("Old name", "old id"), "pid") == 2
    assert saves == ["schedules", "displays"], "one transaction each"
    reopened = {rule.id: rule.playlist for rule in schedules.Store.open().rules}
    assert reopened == {first.id: "pid", second.id: "pid", other.id: "else"}
    assert dict(displays.Store.open().all()) == {"DP-1": "pid", "DP-2": "pid"}


def test_a_rename_problem_is_found_without_writing(tmp_path: Path) -> None:
    made = _playlists(tmp_path)
    made.store.create("Taken")
    target = playlists.state_path()
    before = target.read_bytes()
    assert made.store.rename_problem(made.record, "Fine") == ""
    assert "identity-conflict" in made.store.rename_problem(made.record, "taken")
    assert "invalid-name" in made.store.rename_problem(made.record, "   ")
    assert target.read_bytes() == before
    target.write_bytes(BROKEN)
    assert "unreadable" in made.store.rename_problem(made.record, "Fine")
    assert target.read_bytes() == BROKEN
