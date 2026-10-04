"""After Codex 0.3.0 round 1: every path keeps the runtime configuration buildable (no display).

The rule is the compiler's (`runtime_config.change_problem`): a playlist in
use keeps something to play, and any playlist that holds entries keeps one
the compiler can use, used or not, because one holding only entries it
drops stops every publication (`runtime_config._compile`). Here are the rule
itself and the new window's adapters and Library view, over a real
`Session` and the recording applications of ``test_next_state`` and
``test_next_editing``. The application's own boundary, the classic window
and ``ctl`` are in ``test_ui_codex030_round1b``. Each test fails on f442d1e,
the merge this was made on.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_next_editing import EditingApplication, editing_of, wid
from tests.test_next_state import _library, _session
from wall_in_one import runtime_config
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.runtime_config import Playability
from wall_in_one.ui.next import real_schedule
from wall_in_one.ui.next.prefs import UiPrefsKeeper
from wall_in_one.ui.next.real_playlists import RealPlaylists
from wall_in_one.ui.next.real_state import RealAppState


@pytest.fixture(autouse=True)
def no_monitors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connected displays come from the runtime alone here (see test_next_schedule_mapping)."""
    monkeypatch.setattr(real_schedule, "_monitor_displays", list)


@pytest.fixture
def library(tmp_path: Path) -> tuple[MediaItem, ...]:
    # alpine.png, city-dusk.png, lily-pond.png and rain.mp4: a video with no still.
    return _library(tmp_path / "home" / "Pictures" / "Wallpapers")


@pytest.fixture
def backend(library: tuple[MediaItem, ...]) -> Iterator[EditingApplication]:
    session = _session(library)
    try:
        yield EditingApplication(session)
    finally:
        session.shutdown()


@pytest.fixture
def adapter(backend: EditingApplication) -> Iterator[RealAppState]:
    keeper = UiPrefsKeeper(backend.window_report)
    state = RealAppState(backend, keeper, authoring=backend)
    backend.after_favourites = state.reload
    backend.after_playlists = state.reload_playlists
    try:
        yield state
    finally:
        state.close()


def _video(library: tuple[MediaItem, ...]) -> MediaItem:
    video = next(item for item in library if item.kind is Kind.VIDEO)
    assert video.paired_still is None, "the fixture's video has no still"
    return video


def _builds(backend: EditingApplication) -> bool:
    try:
        runtime_config.render(backend.session.settings, backend.session)
    except runtime_config.RuntimeConfigError:
        return False
    return True


# -- the rule ------------------------------------------------------------------------------


def test_the_rule_counts_an_unused_playlist_that_would_hold_only_what_the_compiler_drops() -> None:
    change_problem = runtime_config.change_problem
    nothing_to_play = runtime_config.LEAVES_NOTHING_TO_PLAY
    only_unusable = runtime_config.LEAVES_ONLY_UNUSABLE
    plays = Playability(True, True)
    skipped = Playability(True, False, "codec missing")
    dropped = Playability(False, False, "it has no still for playback to show yet")
    # Used: something must stay that plays (unchanged from round 1).
    assert change_problem([plays, dropped], [dropped], True) == nothing_to_play
    assert change_problem([plays, plays], [plays], True) == ""
    assert change_problem([plays], [], True) == nothing_to_play
    # Unused: what holds entries keeps one the compiler can use.
    assert change_problem([plays, dropped], [dropped], False) == only_unusable
    assert change_problem([], [dropped], False) == only_unusable, "added to an empty one"
    assert change_problem([plays, dropped], [], False) == "", "an empty one compiles"
    assert change_problem([plays, skipped], [skipped], False) == "", "a skipped one compiles"
    assert change_problem([dropped, dropped], [dropped], False) == "", "already broken: tidy it"


def test_an_unused_playlist_left_with_only_what_the_compiler_drops_stops_publication(
    backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    """Why the rule covers unused playlists: the compiler refuses the whole document."""
    session = backend.session
    made = session.playlists.create("Rain only")
    session.playlists.add(made.id, _video(library).path)
    assert not _builds(backend), "nothing uses it, and still nothing can be published"


# -- the new window's Playlists page ---------------------------------------------------------


def test_removing_an_unused_playlists_last_usable_entry_is_refused(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    session = backend.session
    made = session.playlists.create("Mixed")
    session.playlists.add(made.id, library[0].path, entry_id="still")
    session.playlists.add(made.id, _video(library).path, entry_id="video")
    editing = adapter.playlist_editing
    assert isinstance(editing, RealPlaylists)

    editing.remove_entries(made.id, ["still"])
    backend.run_authoring()

    kept = session.playlists.get(made.id)
    assert kept is not None and [entry.id for entry in kept.entries] == ["still", "video"]
    assert "would hold only wallpapers playback can\u2019t use" in backend.reports[-1]
    assert _builds(backend)

    # Taking the video out too is fine: an empty playlist compiles.
    editing.remove_entries(made.id, ["video", "still"])
    backend.run_authoring()
    emptied = session.playlists.get(made.id)
    assert emptied is not None and emptied.entries == ()
    assert _builds(backend)


def test_library_removal_of_an_unused_playlists_last_usable_entry_is_refused(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    session = backend.session
    made = session.playlists.create("Mixed")
    session.playlists.add(made.id, library[1].path)
    session.playlists.add(made.id, _video(library).path)

    editing_of(adapter).remove_wallpapers([wid(library, 1)])

    assert backend.removals == []
    assert backend.reports == [
        "city-dusk was not removed: \u201cMixed\u201d would hold only wallpapers playback "
        "can\u2019t use. Add another wallpaper there first."
    ]
