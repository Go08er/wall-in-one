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
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from wall_in_one.browse import Downloaded  # noqa: E402
from wall_in_one.library.model import Kind  # noqa: E402
from wall_in_one.providers import registry, wallhaven  # noqa: E402
from wall_in_one.providers.base import (  # noqa: E402
    CandidateDetail,
    DownloadResult,
    Fact,
    ProviderError,
    SearchResult,
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


# -- loading more as the grid is scrolled --------------------------------


@pytest.mark.parametrize(
    ("value", "upper", "page_size", "expected"),
    [
        # Fresh grid, one screen of results: nothing scrolled yet, nothing more.
        (0.0, 400.0, 400.0, True),
        # Three screens of results, sitting at the top: plenty left.
        (0.0, 1200.0, 400.0, False),
        # Scrolled to within one screen of the bottom.
        (400.0, 1200.0, 400.0, True),
        # One pixel outside that.
        (399.0, 1200.0, 400.0, False),
        # All the way down.
        (800.0, 1200.0, 400.0, True),
    ],
)
def test_the_load_more_threshold_leaves_a_screen_of_slack(
    value: float, upper: float, page_size: float, expected: bool
) -> None:
    assert browse_dialog.near_the_end(value=value, upper=upper, page_size=page_size) is expected


class _StubApp:
    """The two things `BrowseDialog` asks of its application."""

    def __init__(self, root: Path) -> None:
        self.settings = SimpleNamespace(roots=(root,))
        self.refreshes = 0

    def refresh_library(self) -> None:
        self.refreshes += 1


def _result(*identifiers: str, page: int = 1, has_next: bool = False) -> SearchResult:
    return SearchResult(
        provider="wallhaven",
        query_url="https://wallhaven.cc/api/v1/search",
        items=tuple(
            WallpaperCandidate(
                provider="wallhaven",
                identifier=name,
                title=name,
                kind=Kind.STILL,
                page_url=f"https://wallhaven.cc/w/{name}",
            )
            for name in identifiers
        ),
        page=page,
        has_next=has_next,
        total_hint=99,
    )


@pytest.fixture
def dialog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> browse_dialog.BrowseDialog:
    """A real dialog whose searches are answered from a script.

    `registry.describe` is stubbed so the test cannot read the developer's own
    Wallhaven credentials on its way past.
    """
    monkeypatch.setattr(
        registry,
        "describe",
        lambda: (
            SimpleNamespace(name="wallhaven", title="Wallhaven", usable=True, limitations=()),
            SimpleNamespace(name="motionbgs", title="MotionBGS", usable=True, limitations=()),
        ),
    )
    built = browse_dialog.BrowseDialog(_StubApp(tmp_path))  # type: ignore[arg-type]
    # No previews: they would open sockets, and nothing here is about pictures.
    monkeypatch.setattr(built._loader, "request", lambda *_arguments: None)
    return built


def _answer(
    dialog: browse_dialog.BrowseDialog,
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[int, SearchResult],
) -> list[int]:
    """Serve ``pages`` from the fake browser, recording which were asked for."""
    asked: list[int] = []

    def search(_name: str, query: object) -> SearchResult:
        page = getattr(query, "page", 1)
        asked.append(page)
        return pages[page]

    monkeypatch.setattr(dialog._browser, "search", search)
    return asked


def test_a_first_page_that_says_there_is_more_asks_for_it(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grid is not realised, so it never fills the window and never scrolls.

    That is exactly the short-first-page case: without the idle check after
    the first result, the second page would never be requested at all.
    """
    asked = _answer(
        dialog,
        monkeypatch,
        {
            1: _result("aaa111", "bbb222", page=1, has_next=True),
            2: _result("ccc333", page=2, has_next=False),
        },
    )

    dialog.start_search(page=1)
    assert _settle(lambda: len(dialog._cards) == 3)
    assert asked == [1, 2]
    assert not dialog._has_next


def test_an_overlapping_page_does_not_show_a_wallpaper_twice(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wallhaven does this whenever a random search runs unseeded."""
    _answer(
        dialog,
        monkeypatch,
        {
            1: _result("aaa111", "bbb222", page=1, has_next=True),
            2: _result("bbb222", "ccc333", page=2, has_next=False),
        },
    )

    dialog.start_search(page=1)
    assert _settle(lambda: not dialog._has_next and not dialog._searching)
    assert [card.candidate.identifier for card in dialog._cards] == ["aaa111", "bbb222", "ccc333"]


def test_a_page_that_is_entirely_repeats_stops_the_loading(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the grid spins through the catalogue at scroll speed.

    A provider that keeps answering "here is more" with results already shown
    would be asked for page after page, none of which changes anything.
    """
    asked = _answer(
        dialog,
        monkeypatch,
        {
            1: _result("aaa111", page=1, has_next=True),
            2: _result("aaa111", page=2, has_next=True),
        },
    )

    dialog.start_search(page=1)
    assert _settle(lambda: len(asked) >= 2 and not dialog._searching)
    assert len(dialog._cards) == 1
    assert not dialog._has_next
    # Page three is never asked for.
    assert _settle(lambda: False, seconds=0.2) is False
    assert asked == [1, 2]


def test_a_new_search_replaces_the_grid_rather_than_growing_it(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(dialog, monkeypatch, {1: _result("aaa111", "bbb222", page=1)})
    dialog.start_search(page=1)
    assert _settle(lambda: len(dialog._cards) == 2)

    _answer(dialog, monkeypatch, {1: _result("zzz999", page=1)})
    dialog.start_search(page=1)

    assert _settle(lambda: [c.candidate.identifier for c in dialog._cards] == ["zzz999"])


def test_failing_to_load_more_keeps_what_is_already_shown(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The results already on screen are still results."""
    calls: list[int] = []

    def search(_name: str, query: object) -> SearchResult:
        page = getattr(query, "page", 1)
        calls.append(page)
        if page > 1:
            raise ProviderError("network", "the site is down")
        return _result("aaa111", "bbb222", page=1, has_next=True)

    monkeypatch.setattr(dialog._browser, "search", search)

    dialog.start_search(page=1)
    assert _settle(lambda: len(calls) >= 2 and not dialog._searching)

    assert len(dialog._cards) == 2
    assert dialog._stack.get_visible_child_name() == "results"
    assert not dialog._has_next


def test_the_summary_counts_everything_on_screen_not_the_last_page(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With paging gone, "3 results" has to mean the grid, not the last request."""
    _answer(
        dialog,
        monkeypatch,
        {
            1: _result("aaa111", "bbb222", page=1, has_next=True),
            2: _result("ccc333", page=2, has_next=False),
        },
    )

    dialog.start_search(page=1)
    assert _settle(lambda: len(dialog._cards) == 3)
    assert dialog._summary.get_label().startswith("3 results")


# -- picking several, and the queue --------------------------------------


def _downloaded(tmp_path: Path, name: str) -> Downloaded:
    return Downloaded(
        result=DownloadResult(
            provider="wallhaven",
            identifier=name,
            path=tmp_path / f"{name}.jpg",
            sidecar=tmp_path / f"{name}.jpg.wallhaven.json",
            marker=tmp_path / "marker.json",
            kind=Kind.STILL,
            size=1024,
            source_url=f"https://wallhaven.cc/w/{name}",
            download_url=f"https://w.wallhaven.cc/full/aa/wallhaven-{name}.jpg",
            sha256="0" * 64,
            downloaded_at="2026-01-01T00:00:00Z",
        ),
        root=tmp_path,
    )


def _three(dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(dialog, monkeypatch, {1: _result("aaa111", "bbb222", "ccc333", page=1)})
    dialog.start_search(page=1)
    assert _settle(lambda: len(dialog._cards) == 3)


def test_nothing_picked_means_no_batch_controls(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The footer stays a footer until there is a batch to act on."""
    _three(dialog, monkeypatch)
    assert not dialog._download_picked.get_visible()
    assert not dialog._picked.get_visible()


def test_picking_cards_reveals_the_batch_controls(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three(dialog, monkeypatch)

    dialog._cards[0]._check.set_active(True)
    dialog._cards[2]._check.set_active(True)

    assert dialog._picked.get_label() == "2 selected"
    assert dialog._download_picked.get_visible()


def test_downloading_a_batch_queues_each_and_drops_the_selection(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The selection is released as soon as the request is made.

    Leaving the boxes ticked would invite pressing Download again and queueing
    the whole batch a second time.
    """
    _three(dialog, monkeypatch)
    asked: list[str] = []

    def download(candidate: WallpaperCandidate, *, variant: str = "") -> Downloaded:
        asked.append(candidate.identifier)
        return _downloaded(tmp_path, candidate.identifier)

    monkeypatch.setattr(dialog._browser, "download", download)

    dialog._cards[0]._check.set_active(True)
    dialog._cards[1]._check.set_active(True)
    dialog._download_all_picked()

    assert not any(card.picked for card in dialog._cards)
    assert not dialog._download_picked.get_visible()
    assert _settle(lambda: sorted(asked) == ["aaa111", "bbb222"])


def test_the_queue_says_where_it_has_got_to_and_then_stops(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _three(dialog, monkeypatch)
    monkeypatch.setattr(
        dialog._browser,
        "download",
        lambda candidate, variant="": _downloaded(tmp_path, candidate.identifier),
    )

    for card in dialog._cards:
        card._check.set_active(True)
    dialog._download_all_picked()
    assert dialog._queue.get_label() == "downloading 1 of 3"

    assert _settle(lambda: dialog._queue.get_label() == "")


def test_a_second_batch_counts_from_one_again(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Otherwise the next batch would continue the last one's numbering."""
    _three(dialog, monkeypatch)
    monkeypatch.setattr(
        dialog._browser,
        "download",
        lambda candidate, variant="": _downloaded(tmp_path, candidate.identifier),
    )

    dialog._cards[0]._check.set_active(True)
    dialog._download_all_picked()
    assert _settle(lambda: dialog._queue.get_label() == "")

    dialog._cards[1]._check.set_active(True)
    dialog._download_all_picked()
    assert dialog._queue.get_label() == "downloading 1 of 1"


def test_a_failed_download_still_advances_the_queue(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch of two with one failure has still finished.

    A progress line that never reaches the end is worse than one that stops.
    """
    _three(dialog, monkeypatch)

    def download(candidate: WallpaperCandidate, *, variant: str = "") -> Downloaded:
        raise ProviderError("network", "the site is down")

    monkeypatch.setattr(dialog._browser, "download", download)

    dialog._cards[0]._check.set_active(True)
    dialog._cards[1]._check.set_active(True)
    dialog._download_all_picked()

    assert _settle(lambda: dialog._queue.get_label() == "")


def test_a_wallpaper_that_lands_cannot_be_picked_again(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It must not still be counted in "3 selected", nor queued twice."""
    _three(dialog, monkeypatch)
    card = dialog._cards[0]
    card._check.set_active(True)

    card.mark_downloaded()

    assert not card.picked
    assert not card._check.get_sensitive()
    assert not dialog._download_picked.get_visible()


def test_a_new_search_forgets_the_selection(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Download button over an empty grid has nothing behind it."""
    _three(dialog, monkeypatch)
    dialog._cards[0]._check.set_active(True)
    assert dialog._download_picked.get_visible()

    _answer(dialog, monkeypatch, {1: _result("zzz999", page=1)})
    dialog.start_search(page=1)

    assert _settle(lambda: len(dialog._cards) == 1)
    assert not dialog._download_picked.get_visible()


# -- keyboard ------------------------------------------------------------


def _press(card: browse_dialog._CandidateCard, keyval: int, ctrl: bool = False) -> bool:
    state = Gdk.ModifierType.CONTROL_MASK if ctrl else Gdk.ModifierType(0)
    handled = card._on_key(Gtk.EventControllerKey(), keyval, 0, state)
    return bool(handled)


def test_cards_are_in_the_focus_chain(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrow keys can only walk the grid if its cards can take focus."""
    _three(dialog, monkeypatch)
    assert all(card.get_focusable() for card in dialog._cards)


def test_enter_opens_the_detail_view(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three(dialog, monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(dialog._cards[0], "_on_open", lambda c: opened.append(c.identifier))

    assert _press(dialog._cards[0], Gdk.KEY_Return)
    assert opened == ["aaa111"]


def test_space_picks_rather_than_downloads(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking is the reversible one.

    Pressing space by accident should tick a box, not start pulling a 40 MB
    video off somebody's server.
    """
    _three(dialog, monkeypatch)
    started: list[str] = []
    monkeypatch.setattr(dialog._cards[0], "_on_download", lambda c: started.append(c.identifier))

    assert _press(dialog._cards[0], Gdk.KEY_space)

    assert dialog._cards[0].picked
    assert started == []


def test_space_again_unpicks(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three(dialog, monkeypatch)
    _press(dialog._cards[0], Gdk.KEY_space)
    _press(dialog._cards[0], Gdk.KEY_space)
    assert not dialog._cards[0].picked


def test_control_enter_downloads(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three(dialog, monkeypatch)
    started: list[str] = []
    monkeypatch.setattr(dialog._cards[0], "_on_download", lambda c: started.append(c.identifier))

    assert _press(dialog._cards[0], Gdk.KEY_Return, ctrl=True)
    assert started == ["aaa111"]


def test_the_keyboard_will_not_requeue_something_already_held(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three(dialog, monkeypatch)
    card = dialog._cards[0]
    started: list[str] = []
    monkeypatch.setattr(card, "_on_download", lambda c: started.append(c.identifier))
    card.mark_downloaded()

    _press(card, Gdk.KEY_Return, ctrl=True)
    _press(card, Gdk.KEY_space)

    assert started == []
    assert not card.picked


def test_an_unclaimed_key_is_left_alone(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrow keys must reach GTK's focus handling, not be swallowed here."""
    _three(dialog, monkeypatch)
    assert not _press(dialog._cards[0], Gdk.KEY_Down)
    assert not _press(dialog._cards[0], Gdk.KEY_Right)


def test_select_all_takes_everything_on_screen(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a grid that grows as it scrolls, "all" can only mean what is shown."""
    _three(dialog, monkeypatch)

    dialog._pick_all()

    assert all(card.picked for card in dialog._cards)
    assert dialog._picked.get_label() == "3 selected"


def test_select_all_skips_what_is_already_in_the_library(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three(dialog, monkeypatch)
    dialog._cards[1].mark_downloaded()

    dialog._pick_all()

    assert [card.picked for card in dialog._cards] == [True, False, True]
    assert dialog._picked.get_label() == "2 selected"


def test_control_f_reaches_the_search_box(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    focused: list[bool] = []

    def grab() -> bool:
        focused.append(True)
        return True

    monkeypatch.setattr(dialog._entry, "grab_focus", grab)

    dialog._focus_search()

    assert focused == [True]
