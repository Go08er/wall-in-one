"""Astra's 0.3.0 review, X-1: a copy holds something that can play. No display.

Copying "All wallpapers" copied every scanned item without asking whether the
copy could compile. A library of one video with no still yet (its capture
pending or failed, or no ffmpeg) gave a saved copy with one unresolved entry,
and the compiler then refused the whole runtime document, so every later
change stayed off the wallpaper service until the copy was repaired. (Since
r6 the compiler builds without such a playlist instead of refusing; the copy
is still refused, as a playlist that could never play.)

Here through the Playlists page's part (`RealPlaylists`) over a real `Session`
with its stores in the test's own XDG dirs and the queue-holding application
of ``test_next_state``; ``test_ui_astra030_x1`` drives the real application.
Each scenario test fails on 4018098 (the copy is saved, and the document is
no longer whole: `tests.whole_document`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_next_state import FakeApplication, _adapter, _library, _session
from tests.whole_document import builds_whole
from wall_in_one import runtime_config
from wall_in_one.library import playlists
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.session import Session
from wall_in_one.ui.next.real_playlists import RealPlaylists

#: The refusal, in the compiler's rule's own words (`runtime_config.change_refusal`).
ONLY_UNUSABLE = "would hold only wallpapers playback can\u2019t use"


@pytest.fixture
def items(tmp_path: Path) -> tuple[MediaItem, ...]:
    # alpine.png, city-dusk.png, lily-pond.png and rain.mp4: a video with no still.
    return _library(tmp_path / "home" / "Pictures" / "Wallpapers")


def _video(items: tuple[MediaItem, ...]) -> MediaItem:
    video = next(item for item in items if item.kind is Kind.VIDEO)
    assert video.paired_still is None, "the fixture's video has no still"
    return video


@pytest.fixture
def only_video(items: tuple[MediaItem, ...]) -> Iterator[FakeApplication]:
    """A library of one video with no still: All wallpapers compiles to nothing, harmlessly."""
    session = _session((_video(items),))
    try:
        yield FakeApplication(session)
    finally:
        session.shutdown()


@pytest.fixture
def everything(items: tuple[MediaItem, ...]) -> Iterator[FakeApplication]:
    session = _session(items)
    try:
        yield FakeApplication(session)
    finally:
        session.shutdown()


def _editing(backend: FakeApplication) -> RealPlaylists:
    adapter, _keeper = _adapter(backend)
    editing = adapter.playlist_editing
    assert isinstance(editing, RealPlaylists)
    return editing


def _builds(session: Session) -> bool:
    """Whether the runtime document is built whole: nothing left out, no playlist dropped.

    What "it builds" meant while one playlist that can't play refused the
    whole compile (`tests.whole_document`).
    """
    return builds_whole(session.settings, session)


def _saved_names() -> list[str]:
    """What is on disk, not what this process holds."""
    return [playlist.name for playlist in playlists.Store.open().all()]


def test_x1_copying_all_wallpapers_of_only_unresolved_videos_is_refused(
    only_video: FakeApplication,
) -> None:
    session = only_video.session
    assert _builds(session), "the built-in fallback with nothing resolved builds"
    editing = _editing(only_video)

    copy = editing.duplicate_playlist("all-media")
    assert copy and editing.creating(copy)
    only_video.run_authoring()

    assert not editing.creating(copy)
    assert session.playlists.get(copy) is None and session.playlists.all() == ()
    assert _saved_names() == [], "no bad copy saved"
    assert only_video.reports[-1] == (
        f"The copy was not made; nothing changed: \u201cAll wallpapers (copy)\u201d {ONLY_UNUSABLE}"
    )
    assert only_video.playlist_changes == 0, "nothing to publish"
    assert _builds(session), "nothing was made that the document would be built without"


def test_x1_a_copy_with_something_that_compiles_is_made(everything: FakeApplication) -> None:
    """The accepted limitation stands: a video waiting for its still may ride along."""
    session = everything.session
    editing = _editing(everything)
    copy = editing.duplicate_playlist("all-media")
    everything.run_authoring()
    made = session.playlists.find(copy)
    assert made.name == "All wallpapers (copy)"
    assert [entry.path for entry in made.entries] == [item.path for item in session.library.items]
    assert not everything.reports
    assert _builds(session)


def test_x1_a_copy_of_a_playlist_whose_files_no_longer_compile_is_refused(
    everything: FakeApplication, items: tuple[MediaItem, ...]
) -> None:
    """The same path for a playlist of one's own: no second playlist that can't play.

    "Videos" got that way from outside the app (only a removed file's entry
    is left, say); a copy of it would be one more playlist the document is
    built without, even once "Videos" is mended or deleted.
    """
    session = everything.session
    videos = session.playlists.create(
        "Videos", entries=[playlists.Entry("v0", str(_video(items).path))]
    )
    assert not _builds(session), "the original is already left out of the document"
    built = runtime_config.compiled(session.settings, session)
    assert "Videos" not in built.document and built.left_out == (), (
        "and stops nothing: the rest is published, with nothing referring to it to say"
    )
    editing = _editing(everything)
    copy = editing.duplicate_playlist(videos.id)
    everything.run_authoring()
    assert session.playlists.get(copy) is None
    assert _saved_names() == ["Videos"]
    assert f"\u201cVideos (copy)\u201d {ONLY_UNUSABLE}" in everything.reports[-1]
    session.playlists.delete(videos.id)
    assert _builds(session), "mending the original is enough"


def test_x1_empty_playlists_and_empty_copies_are_still_made(everything: FakeApplication) -> None:
    session = everything.session
    editing = _editing(everything)
    empty = editing.create_playlist("Empty")
    everything.run_authoring()
    assert session.playlists.find(empty).entries == ()
    copy = editing.duplicate_playlist(empty)
    everything.run_authoring()
    assert session.playlists.find(copy).name == "Empty (copy)"
    assert session.playlists.find(copy).entries == ()
    assert not everything.reports
    assert _builds(session)


def test_x1_the_shared_rule_for_a_new_playlist(everything: FakeApplication) -> None:
    session = everything.session
    still, video = session.library.items[0], _video(tuple(session.library.items))
    assert runtime_config.new_playlist_refusal("Empty", [], session) == ""
    assert runtime_config.new_playlist_refusal("Mixed", [video.path, still.path], session) == ""
    assert runtime_config.new_playlist_refusal("Gone", ["/not/mounted.png"], session) == (
        f"\u201cGone\u201d {ONLY_UNUSABLE}"
    )
    with pytest.raises(runtime_config.PlaybackGuardError) as refused:
        runtime_config.checked_new_playlist("Videos", [video.path], lambda: None, session)
    assert refused.value.kind == "nothing-to-play"
    assert str(refused.value) == f"\u201cVideos\u201d {ONLY_UNUSABLE}"
