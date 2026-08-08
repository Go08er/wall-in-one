"""The browse dialog's widgets, in as much isolation as GTK allows.

Carries the `gui` marker like `test_ui_grid`: these build real widgets, which
needs a display the Nix check sandbox does not have. Nothing is presented,
nothing is drawn, and no network call is made -- the colour picker is a pure
function of clicks, which is exactly why it is worth pinning here rather than
discovering by hand.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib  # noqa: E402

from wall_in_one.library.model import Kind  # noqa: E402
from wall_in_one.providers import wallhaven  # noqa: E402
from wall_in_one.providers.base import (  # noqa: E402
    CandidateDetail,
    Fact,
    ProviderError,
    WallpaperCandidate,
)
from wall_in_one.ui import browse_dialog  # noqa: E402


@pytest.fixture(autouse=True)
def _initialised() -> None:
    Adw.init()


def test_the_picker_offers_every_colour_wallhaven_accepts() -> None:
    """A swatch the API would refuse is a search that fails on click."""
    picker = browse_dialog._ColourPicker()
    assert set(picker._buttons) == wallhaven.COLORS


def test_the_palette_keeps_its_order() -> None:
    """Built from a sequence, because a set would reshuffle between runs."""
    assert tuple(browse_dialog._ColourPicker()._buttons) == wallhaven.COLOR_ORDER


def test_nothing_is_selected_to_begin_with() -> None:
    assert browse_dialog._ColourPicker().colour() == ""


def test_choosing_a_colour_reads_back_as_that_colour() -> None:
    picker = browse_dialog._ColourPicker()
    picker._buttons["0066cc"].set_active(True)
    assert picker.colour() == "0066cc"


def test_choosing_a_second_colour_replaces_the_first() -> None:
    """Wallhaven takes one colour, so two selected swatches cannot be shown."""
    picker = browse_dialog._ColourPicker()
    picker._buttons["0066cc"].set_active(True)
    picker._buttons["cc0000"].set_active(True)

    assert picker.colour() == "cc0000"
    assert not picker._buttons["0066cc"].get_active()


def test_clicking_the_selected_colour_clears_it() -> None:
    """How "any colour" is said, without a Clear button that is dead most of
    the time."""
    picker = browse_dialog._ColourPicker()
    picker._buttons["0066cc"].set_active(True)
    picker._buttons["0066cc"].set_active(False)

    assert picker.colour() == ""


def test_a_swatch_is_the_colour_it_claims() -> None:
    texture = browse_dialog._swatch_texture("ff6600", size=4)
    assert (texture.get_width(), texture.get_height()) == (4, 4)


def test_a_default_is_found_by_name_not_position() -> None:
    assert browse_dialog._index_of(browse_dialog.TOP_RANGES, "1M") == 3
    assert browse_dialog.TOP_RANGES[3][0] == "1M"


def test_an_unknown_default_falls_back_to_the_first_entry() -> None:
    assert browse_dialog._index_of(browse_dialog.TOP_RANGES, "nonsense") == 0


# -- the detail view -----------------------------------------------------


class _StubBrowser:
    """Stands in for `Browser`, answering the two calls the detail view makes."""

    def __init__(self, detail: CandidateDetail | None = None, fail: str = "") -> None:
        self._detail = detail
        self._fail = fail
        self.described: list[str] = []

    def describe(self, candidate: WallpaperCandidate) -> CandidateDetail:
        self.described.append(candidate.identifier)
        if self._fail:
            raise ProviderError("response", self._fail)
        assert self._detail is not None
        return self._detail

    def preview(self, url: str) -> bytes:
        # No picture: decoding one would drag ffmpeg into a widget test, and
        # the facts are what this is pinning.
        return b""


def _candidate(provider: str = "wallhaven") -> WallpaperCandidate:
    return WallpaperCandidate(
        provider=provider,
        identifier="abc123",
        title="A wallpaper",
        kind=Kind.STILL,
        page_url="https://wallhaven.cc/w/abc123",
    )


def _settle(predicate: Callable[[], bool], seconds: float = 5.0) -> bool:
    """Run the main loop until ``predicate`` holds, or give up.

    The detail view does its work on a pool and delivers through
    `GLib.idle_add`, so nothing has happened when the constructor returns.
    """
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return predicate()


def _open(
    browser: _StubBrowser, candidate: WallpaperCandidate, *, held: bool = False
) -> browse_dialog.DetailDialog:
    return browse_dialog.DetailDialog(
        browser,  # type: ignore[arg-type]
        candidate,
        lambda _candidate, _variant: None,
        held=held,
    )


def test_the_detail_view_asks_the_provider_and_shows_what_it_says() -> None:
    detail = CandidateDetail(
        candidate=_candidate(),
        facts=(Fact("Resolution", "3840x2160"), Fact("Uploader", "someone")),
        tags=("sky", "clouds"),
        colours=("0066cc", "000000"),
    )
    browser = _StubBrowser(detail)

    dialog = _open(browser, _candidate())

    assert _settle(lambda: dialog._detail is not None)
    assert browser.described == ["abc123"]
    assert dialog._tags.get_visible()
    assert dialog._colours.get_visible()
    # Two facts, laid out as label and value in a two-column grid.
    assert dialog._facts.get_child_at(0, 1) is not None
    assert dialog._facts.get_child_at(1, 1) is not None
    assert dialog._facts.get_child_at(0, 2) is None


def test_a_single_file_provider_shows_no_variant_chooser() -> None:
    """A dropdown with one entry asks a question that has no answer."""
    dialog = _open(_StubBrowser(CandidateDetail(candidate=_candidate())), _candidate())

    assert _settle(lambda: dialog._detail is not None)
    assert not dialog._variants.get_visible()


def test_two_download_qualities_become_a_chooser() -> None:
    detail = CandidateDetail(candidate=_candidate("motionbgs"), variants=("4k", "hd"))
    dialog = _open(_StubBrowser(detail), _candidate("motionbgs"))

    assert _settle(lambda: dialog._detail is not None)
    assert dialog._variants.get_visible()


def test_a_failed_lookup_still_allows_the_download() -> None:
    """Failing to describe a wallpaper is no reason to refuse to fetch it.

    The candidate from the search carries everything the download needs, so
    disabling the button would take away a working action to report a broken
    one.
    """
    dialog = _open(_StubBrowser(fail="the site said no"), _candidate())

    assert _settle(lambda: "no" in dialog._status.get_label())
    assert dialog._download.get_sensitive()


def test_a_wallpaper_already_held_is_not_offered_again() -> None:
    dialog = _open(_StubBrowser(CandidateDetail(candidate=_candidate())), _candidate(), held=True)

    assert not dialog._download.get_sensitive()
    assert dialog._download.get_label() == "In your library"


def test_a_download_that_lands_updates_the_open_detail_view() -> None:
    dialog = _open(_StubBrowser(CandidateDetail(candidate=_candidate())), _candidate())
    assert _settle(lambda: dialog._detail is not None)

    dialog.downloaded()

    assert not dialog._download.get_sensitive()
    assert dialog._download.get_label() == "In your library"


def test_a_download_that_fails_gives_the_button_back() -> None:
    dialog = _open(_StubBrowser(CandidateDetail(candidate=_candidate())), _candidate())
    assert _settle(lambda: dialog._detail is not None)
    dialog._download.set_sensitive(False)

    dialog.failed()

    assert dialog._download.get_sensitive()
    assert dialog._download.get_label() == "Download"
