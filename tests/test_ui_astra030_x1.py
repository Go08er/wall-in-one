"""Astra's 0.3.0 review, X-1, in the real application: a copy can't stop publication.

The new window's Playlists page over a library of one video with no still,
as Astra replayed it: "Duplicate to edit" on "All wallpapers" goes through the
real application's authoring lane and store. On 4018098 the copy was saved
with one unresolved entry and the runtime document no longer built (the
compiler: "playlist 'All wallpapers (copy)' has no playable entries"); now
the copy is refused at the head of the lane, nothing is saved, and the
document still builds. ``test_astra030_x1`` covers the same through the
adapter alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.test_ui_next_settings import Opened, _close, _open  # noqa: E402
from tests.test_ui_next_window import sandboxed_runtime  # noqa: E402
from tests.whole_document import builds_whole  # noqa: E402
from wall_in_one import config, runtime_compatibility  # noqa: E402
from wall_in_one.library import playlists  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.ui.next.playlists import PlaylistPage  # noqa: E402
from wall_in_one.ui.next.real_state import RealAppState  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Opened]:
    """The new window over a library holding one video whose still isn't made yet."""
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    videos = tmp_path / "videos"
    videos.mkdir()
    rain = videos / "rain.mp4"
    rain.write_bytes(b"a video ffmpeg has not captured a still from")
    config.update({"roots": (videos,)})
    shown = _open(monkeypatch)
    application = shown.application
    application.session.adopt_library(
        Library((videos,), (MediaItem(rain, Kind.VIDEO, rain.stat().st_size, 1),)),
        reconcile_workshop=False,
    )
    settings = application.settings
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    state = shown.window.state
    assert isinstance(state, RealAppState)
    state.reload()
    try:
        yield shown
    finally:
        _close(shown)


def _builds(opened: Opened) -> bool:
    """Whether the runtime document is built whole: nothing left out, no playlist dropped.

    What "it builds" meant while one playlist that can't play refused the
    whole compile (`tests.whole_document`).
    """
    application = opened.application
    return builds_whole(application.settings, application.session)


def test_x1_duplicate_to_edit_of_unresolved_videos_saves_nothing_and_keeps_publishing(
    opened: Opened,
) -> None:
    application = opened.application
    assert _builds(opened), "the built-in fallback with nothing resolved builds"
    opened.window.navigate("playlist:all-media")
    page = opened.window.pages["playlist"]
    assert isinstance(page, PlaylistPage) and page.pid == "all-media"

    page._duplicate_auto.emit("clicked")  # "Duplicate to edit"
    # The lane was idle, so its checks ran at once; wait for whatever followed.
    opened.wait("the copy's task")

    assert application.session.playlists.all() == ()
    assert playlists.Store.open().all() == (), "no bad copy saved"
    assert opened.reports[-1] == (
        "The copy was not made; nothing changed: "
        "\u201cAll wallpapers (copy)\u201d would hold only wallpapers playback can\u2019t use"
    )
    assert _builds(opened), "every later publication still goes through"
    opened.wait("the page back on a playlist that exists", lambda: page.pid == "all-media")
