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
from wall_in_one import runtime_config
from wall_in_one.library import playlists
from wall_in_one.library.model import Kind, MediaItem
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


# -- P-3: one playability rule, the compiler's -------------------------------------------------


def _video(items: tuple[MediaItem, ...]) -> MediaItem:
    video = next(item for item in items if item.kind is Kind.VIDEO)
    assert video.paired_still is None, "the fixture's video has no still"
    return video


def test_p3_an_unresolved_video_does_not_count_as_playable_anywhere(
    backend: FakeApplication, items: tuple[MediaItem, ...]
) -> None:
    """A playlist with a still and a video that has no still: the video can't play.

    The review: removing the still was allowed (Playlists counted the video),
    Schedule said nothing could play, and the compiler then refused the
    saved rule's playlist.
    """
    session = backend.session
    still, video = items[0], _video(items)
    made = session.playlists.create("Evening", entry_id="evening")
    session.playlists.add(made.id, still.path, entry_id="e-still")
    session.playlists.add(made.id, video.path, entry_id="e-video")
    session.schedules.add(made.id)
    runtime_config.render(session.settings, session)  # the still compiles: fine
    _state, editing, scheduling = _parts(backend)

    # Removing the still would leave the rule's playlist with nothing that plays.
    editing.remove_entries(made.id, ["e-still"])
    backend.run_authoring()
    kept = playlists.Store.open().get(made.id)
    assert kept is not None and [entry.id for entry in kept.entries] == ["e-still", "e-video"]
    assert "would have nothing left to play" in backend.reports[-1]
    runtime_config.render(session.settings, session)  # still buildable

    # Every view gives the compiler's answer: the video doesn't play.
    detail = editing.detail(made.id)
    assert detail is not None and detail.usable == 1
    video_row = next(entry for entry in detail.entries if entry.id == "e-video")
    assert not video_row.usable and "no still" in video_row.problem
    assert runtime_config.entry_playability(video, session).compiles is False
    assert runtime_config.entry_playability(still, session).plays is True

    # The Schedule and the Playlists page refuse a video-only playlist alike.
    only = session.playlists.create("Video only")
    session.playlists.add(only.id, video.path)
    assert "nothing to play" in scheduling.playlist_blocked(only.id)
    only_detail = editing.detail(only.id)
    assert only_detail is not None and only_detail.default_blocked

    # And adding the video is refused at the gesture, saying why.
    editing.add_entries(made.id, [str(video.path)])
    assert not backend.authoring
    assert "no still" in backend.reports[-1]


def test_p3_the_rule_is_the_compilers_entry_resolution(
    backend: FakeApplication, items: tuple[MediaItem, ...]
) -> None:
    """For every item, "compiles" is exactly whether the compiled playlist keeps it."""
    session = backend.session
    for item in items:
        made = session.playlists.create(
            f"Only {item.name}", entries=[playlists.Entry("x", str(item.path))]
        )
        compiles = runtime_config.entry_playability(item, session).compiles
        session.schedules.add(made.id)
        try:
            runtime_config.render(session.settings, session)
        except runtime_config.RuntimeConfigError:
            built = False
        else:
            built = True
        assert built == compiles, item.name
        for rule in session.schedules.rules:
            session.schedules.remove(rule.id)


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
