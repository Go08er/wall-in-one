"""Codex's 0.3.0 round-1 findings that need no display, one scenario each.

Each test is named for its finding, reproduces the review's scenario through
the real adapters over a real `Session` (stores in the test's own XDG
directories) and the recording application of ``test_next_state``, and
asserts the fixed behavior: it fails on the code the review was made against.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_next_state import FakeApplication, _adapter, _library, _session
from wall_in_one.library import playlists
from wall_in_one.library.model import MediaItem
from wall_in_one.session import Session
from wall_in_one.ui.next import real_schedule
from wall_in_one.ui.next.real_playlists import RealPlaylists
from wall_in_one.ui.next.real_schedule import RealScheduling
from wall_in_one.ui.next.real_state import RealAppState


@pytest.fixture(autouse=True)
def no_monitors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connected displays come from the runtime alone here (see test_next_schedule_mapping)."""
    monkeypatch.setattr(real_schedule, "_monitor_displays", list)


@pytest.fixture
def items(tmp_path: Path) -> tuple[MediaItem, ...]:
    # alpine.png, city-dusk.png, lily-pond.png and rain.mp4: a video with no still.
    return _library(tmp_path / "home" / "Pictures" / "Wallpapers")


@pytest.fixture
def backend(items: tuple[MediaItem, ...]) -> Iterator[FakeApplication]:
    session = _session(items)
    try:
        yield FakeApplication(session)
    finally:
        session.shutdown()


def _parts(backend: FakeApplication) -> tuple[RealAppState, RealPlaylists, RealScheduling]:
    adapter, _keeper = _adapter(backend)
    editing = adapter.playlist_editing
    scheduling = adapter.scheduling
    assert isinstance(editing, RealPlaylists) and isinstance(scheduling, RealScheduling)
    return adapter, editing, scheduling


def _writes(monkeypatch: pytest.MonkeyPatch, module: Any, failing: set[int]) -> list[int]:
    """Count ``module.save`` calls; the ``n``th (1-based) raises while ``n`` is in ``failing``.

    Clearing the returned list starts the count again.
    """
    calls: list[int] = []
    real = module.save

    def save(*arguments: Any, **keywords: Any) -> Any:
        calls.append(len(calls) + 1)
        if len(calls) in failing:
            raise OSError(28, "No space left on device")
        return real(*arguments, **keywords)

    monkeypatch.setattr(module, "save", save)
    return calls


def _evening(session: Session, items: tuple[MediaItem, ...]) -> str:
    return session.playlists.create(
        "Evening", entries=[playlists.Entry("e0", str(items[0].path))]
    ).id


# -- P-5: a playlist made with its wallpapers is one write -------------------------------------


def test_p5_create_with_wallpapers_refused_by_the_entry_cap_leaves_nothing(
    backend: FakeApplication, items: tuple[MediaItem, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, editing, _scheduling_part = _parts(backend)
    monkeypatch.setattr(playlists, "MAX_ENTRIES", 1)
    editing.create_playlist("Morning", [str(items[0].path), str(items[1].path)])
    backend.run_authoring()
    assert playlists.Store.open().by_name("Morning") is None
    assert "was not created; nothing changed" in backend.reports[-1]

    # The same name is free: retrying with what fits works.
    editing.create_playlist("Morning", [str(items[0].path)])
    backend.run_authoring()
    made = playlists.Store.open().by_name("Morning")
    assert made is not None and [entry.source for entry in made.entries] == [str(items[0].path)]


def test_p5_create_with_wallpapers_whose_write_fails_leaves_nothing(
    backend: FakeApplication, items: tuple[MediaItem, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, editing, _scheduling_part = _parts(backend)
    writes = _writes(monkeypatch, playlists, {1})
    editing.create_playlist("Morning", [str(items[0].path), str(items[1].path)])
    backend.run_authoring()
    assert writes == [1], "the playlist and its wallpapers are one write"
    assert playlists.Store.open().by_name("Morning") is None
    assert "was not created; nothing changed" in backend.reports[-1]

    editing.create_playlist("Morning", [str(items[0].path), str(items[1].path)])
    backend.run_authoring()
    made = playlists.Store.open().by_name("Morning")
    assert made is not None and len(made.entries) == 2 and writes == [1, 2]


def test_p5_a_copy_with_its_rotation_is_one_write(
    backend: FakeApplication, items: tuple[MediaItem, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = backend.session
    pid = _evening(session, items)
    session.playlists.set_rotation(pid, cycle_interval=60, shuffle=True)
    _state, editing, _scheduling_part = _parts(backend)
    writes = _writes(monkeypatch, playlists, {1})
    editing.duplicate_playlist(pid)
    backend.run_authoring()
    assert writes == [1]
    assert [playlist.name for playlist in playlists.Store.open().all()] == ["Evening"]

    editing.duplicate_playlist(pid)
    backend.run_authoring()
    assert writes == [1, 2]
    copy = playlists.Store.open().by_name("Evening (copy)")
    assert copy is not None and len(copy.entries) == 1
    assert (copy.cycle_interval, copy.shuffle) == (60, True)
