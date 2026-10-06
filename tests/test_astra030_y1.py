"""Astra's Y-1, through the Playlists page's part alone (no display).

`RealPlaylists` asks the application, at the head of the authoring lane,
whether the library is current before taking wallpapers from it or judging a
change by it (`Application.require_current_library`); here the queue-holding
application of ``test_next_state`` says it is not (``library_refusal``).
``test_ui_astra030_y1`` drives Codex's ordering through the real application.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_next_state import FakeApplication, _adapter, _library, _session
from wall_in_one.library import playlists
from wall_in_one.library.model import MediaItem
from wall_in_one.ui.next.real_playlists import RealPlaylists

STALE = "the library is being scanned again after a removal; wait for that scan to finish"


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[FakeApplication]:
    session = _session(_library(tmp_path / "home" / "Pictures" / "Wallpapers"))
    try:
        yield FakeApplication(session)
    finally:
        session.shutdown()


def _editing(backend: FakeApplication) -> RealPlaylists:
    adapter, _keeper = _adapter(backend)
    editing = adapter.playlist_editing
    assert isinstance(editing, RealPlaylists)
    return editing


def _ids(backend: FakeApplication, pid: str) -> list[str]:
    return [entry.id for entry in backend.session.playlists.find(pid).entries]


def test_y1_nothing_is_taken_from_or_judged_by_a_stale_library(
    backend: FakeApplication,
) -> None:
    items: tuple[MediaItem, ...] = backend.session.library.items
    store = backend.session.playlists
    evening = store.create(
        "Evening",
        entries=[
            playlists.Entry("e0", str(items[0].path)),
            playlists.Entry("e1", str(items[1].path)),
        ],
    )
    editing = _editing(backend)
    editing.remove_entries(evening.id, ["e1"])
    backend.run_authoring()
    reports = backend.reports
    assert _ids(backend, evening.id) == ["e0"]
    backend.library_refusal = STALE

    editing.duplicate_playlist("all-media")
    editing.duplicate_playlist(evening.id)
    editing.create_playlist("With", [str(items[2].path)])
    editing.add_entries(evening.id, [str(items[2].path)])
    editing.remove_entries(evening.id, ["e0"])
    backend.run_authoring()

    assert [playlist.name for playlist in store.all()] == ["Evening"]
    assert _ids(backend, evening.id) == ["e0"]
    assert len(reports) == 5 and all(STALE in report for report in reports)

    # An empty playlist takes nothing from the library: still made.
    empty = editing.create_playlist("Empty")
    backend.run_authoring()
    assert store.find(empty).entries == ()

    backend.library_refusal = ""
    editing.add_entries(evening.id, [str(items[2].path)])
    backend.run_authoring()
    assert len(_ids(backend, evening.id)) == 2


def test_y1_undo_waits_for_a_current_library(backend: FakeApplication) -> None:
    items = backend.session.library.items
    store = backend.session.playlists
    lone = store.create("Lone", entries=[playlists.Entry("e0", str(items[0].path))])
    adapter, _keeper = _adapter(backend)
    editing = adapter.playlist_editing
    assert isinstance(editing, RealPlaylists)
    undos: list[object] = []
    adapter.connect("toast", lambda _state, _text, button: undos.append(button[1]))
    editing.remove_entries(lone.id, ["e0"])
    backend.run_authoring()
    assert _ids(backend, lone.id) == [] and undos

    backend.library_refusal = STALE
    undo = undos[-1]
    assert callable(undo)
    undo()
    backend.run_authoring()
    assert _ids(backend, lone.id) == []
    assert STALE in backend.reports[-1]

    backend.library_refusal = ""
    undo()
    backend.run_authoring()
    assert _ids(backend, lone.id) == ["e0"]
