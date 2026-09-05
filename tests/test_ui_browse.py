"""The browse dialog's widgets, in as much isolation as GTK allows.

Carries the `gui` marker like `test_ui_grid`: these build real widgets, which
needs a display the Nix check sandbox does not have. Nothing is presented,
nothing is drawn, and no network call is made -- the colour picker is a pure
function of clicks, which is exactly why it is worth pinning here rather than
discovering by hand.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from wall_in_one import thumbnails  # noqa: E402
from wall_in_one.browse import Browser, Downloaded  # noqa: E402
from wall_in_one.library import owned  # noqa: E402
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


def _candidate(provider: str = "wallhaven", identifier: str = "abc123") -> WallpaperCandidate:
    return WallpaperCandidate(
        provider=provider,
        identifier=identifier,
        title="A wallpaper",
        kind=Kind.STILL,
        page_url=f"https://wallhaven.cc/w/{identifier}",
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


@pytest.mark.parametrize("failure", ["preview", "lookup", "conversion", "cache-write"])
def test_preview_failures_preserve_metadata_and_download_quality(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate("motionbgs")
    detail = CandidateDetail(candidate=candidate, variants=("4k", "hd"))
    browser = _StubBrowser(detail)
    monkeypatch.setattr(browser, "preview", lambda _url: b"image")
    monkeypatch.setattr(thumbnails, "lookup_preview", lambda _url: b"")
    monkeypatch.setattr(thumbnails, "to_displayable", lambda *_args, **_kw: b"")
    monkeypatch.setattr(thumbnails, "store_preview", lambda *_args: None)

    def fail(*_args: object, **_kwargs: object) -> bytes:
        raise ProviderError("timeout", "only the preview failed")

    if failure == "preview":
        monkeypatch.setattr(browser, "preview", fail)
    else:
        name = {
            "lookup": "lookup_preview",
            "conversion": "to_displayable",
            "cache-write": "store_preview",
        }[failure]
        monkeypatch.setattr(thumbnails, name, fail)
    dialog = _open(browser, candidate)
    try:
        assert _settle(lambda: dialog._detail is detail)
        assert dialog._variants.get_visible()
        requested: list[str] = []
        dialog._on_download = lambda _candidate, variant: requested.append(variant)
        dialog._variants.set_selected(1)
        dialog._download.emit("clicked")
        assert requested == ["hd"]
    finally:
        dialog.cancel_pending()


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


class _StubApp:
    """The two things `BrowseDialog` asks of its application."""

    def __init__(self, root: Path) -> None:
        self.settings = SimpleNamespace(roots=(root,))
        self.refreshes = 0

    def refresh_library(self) -> None:
        self.refreshes += 1

    def require_authoring_ready(self, *, report: bool = True) -> bool:
        del report
        return True


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
    monkeypatch.setattr(built._loader, "prioritize", lambda *_arguments: None)
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


# -- filters -------------------------------------------------------------


def test_store_titles_keep_existing_browse_entry_points(
    dialog: browse_dialog.BrowseDialog,
) -> None:
    assert dialog.get_title() == "Store"
    title = dialog._header.get_title_widget()
    assert isinstance(title, Adw.WindowTitle)
    assert title.get_title() == "Store"


def test_filters_expand_inline_without_a_popover(dialog: browse_dialog.BrowseDialog) -> None:
    assert isinstance(dialog._filters, Gtk.ToggleButton)
    assert isinstance(dialog._filter_revealer.get_child(), Gtk.Box)
    assert not isinstance(dialog._filter_revealer.get_child(), Gtk.Popover)

    dialog._filters.set_active(True)

    assert dialog._filter_revealer.get_reveal_child()


def test_search_and_batch_controls_wrap_without_clipping_at_large_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registry,
        "describe",
        lambda: (
            SimpleNamespace(name="wallhaven", title="Wallhaven", usable=True, limitations=()),
            SimpleNamespace(name="motionbgs", title="MotionBGS", usable=True, limitations=()),
        ),
    )
    settings = Gtk.Settings.get_default()
    if settings is None:  # pragma: no cover - Gtk is initialised under Xvfb
        pytest.skip("no GTK settings")
    old_font = settings.get_property("gtk-font-name")
    settings.set_property("gtk-font-name", "Sans 24")
    page = browse_dialog.BrowsePage(_StubApp(tmp_path))  # type: ignore[arg-type]
    surface = page._surface
    surface._summary.set_label("showing the first 10000 results of about 12000")
    surface._result_page_label.set_label("Page 1 of 250 · more online")
    surface._more.set_label("Loading next page…")
    surface._queue.set_label("downloading 10 of 12")
    surface._picked.set_label("40 selected")
    for widget in (surface._picked, surface._clear_picked, surface._download_picked):
        widget.set_visible(True)
    window = Gtk.Window(default_width=800, default_height=600)
    window.set_child(page)
    window.present()

    try:
        assert isinstance(surface._search_controls, Adw.WrapBox)
        assert isinstance(surface._pager, Adw.WrapBox)
        assert _settle(
            lambda: surface._search_controls.get_width() > 0 and surface._pager.get_width() > 0
        )
        for bar in (surface._search_controls, surface._pager):
            bounds = bar.get_allocation()
            child = bar.get_first_child()
            while child is not None:
                if child.get_visible():
                    allocation = child.get_allocation()
                    assert allocation.width > 0, (
                        type(child).__name__,
                        child.get_mapped(),
                        child.get_child_visible(),
                        getattr(child, "get_label", lambda: "")(),
                    )
                    assert allocation.height > 0
                    assert allocation.x >= 0
                    assert allocation.x + allocation.width <= bounds.width
                    assert allocation.y >= 0
                    assert allocation.y + allocation.height <= bounds.height
                child = child.get_next_sibling()
    finally:
        window.set_child(None)
        window.destroy()
        page.shutdown()
        settings.set_property("gtk-font-name", old_font)


def test_opening_inline_filters_refreshes_nsfw_availability(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert dialog._purity._checks[2].get_sensitive()
    monkeypatch.setattr(
        registry,
        "describe",
        lambda: (
            SimpleNamespace(
                name="wallhaven",
                title="Wallhaven",
                usable=True,
                limitations=("save an API key to include NSFW results",),
            ),
            SimpleNamespace(name="motionbgs", title="MotionBGS", usable=True, limitations=()),
        ),
    )

    dialog._filters.set_active(True)

    assert not dialog._purity._checks[2].get_sensitive()


def test_motionbgs_browse_mode_is_disabled_while_searching(
    dialog: browse_dialog.BrowseDialog,
) -> None:
    dialog._providers.set_selected(1)
    dialog._entry.set_text("naruto")

    assert not dialog._mode.get_sensitive()
    assert not dialog._genre.get_sensitive()
    assert dialog._motionbgs_hint.get_visible()
    assert "clear the search box" in dialog._motionbgs_hint.get_label()


def test_clearing_motionbgs_search_immediately_restores_browse_mode(
    dialog: browse_dialog.BrowseDialog,
) -> None:
    dialog._providers.set_selected(1)
    dialog._entry.set_text("naruto")
    dialog._entry.set_text("")

    assert dialog._mode.get_sensitive()
    assert dialog._genre.get_sensitive()
    assert not dialog._motionbgs_hint.get_visible()


def test_a_first_page_waits_for_explicit_next_before_asking_for_more(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scrolling stays local; a clearly labelled Next owns remote paging."""
    asked = _answer(
        dialog,
        monkeypatch,
        {
            1: _result("aaa111", "bbb222", page=1, has_next=True),
            2: _result("ccc333", page=2, has_next=False),
        },
    )

    dialog.start_search(page=1)
    assert _settle(lambda: len(dialog._cards) == 2)
    assert asked == [1]
    assert dialog._next_results.get_sensitive()

    dialog._show_next_page()
    assert _settle(lambda: [card.candidate.identifier for card in dialog._cards] == ["ccc333"])
    assert asked == [1, 2]
    assert not dialog._has_next
    assert dialog._previous_results.get_sensitive()


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
    assert _settle(lambda: not dialog._searching)
    dialog._show_next_page()
    assert _settle(lambda: not dialog._has_next and not dialog._searching)
    assert tuple(candidate.identifier for candidate in dialog._candidates) == (
        "aaa111",
        "bbb222",
        "ccc333",
    )
    assert [card.candidate.identifier for card in dialog._cards] == ["ccc333"]


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
    assert _settle(lambda: not dialog._searching)
    dialog._show_next_page()
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


def test_switching_provider_discards_an_in_flight_old_provider_result(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow Wallhaven answer must never appear under MotionBGS controls."""
    started = threading.Event()
    release = threading.Event()
    asked: list[str] = []

    def search(name: str, _query: object) -> SearchResult:
        asked.append(name)
        if name == "wallhaven":
            started.set()
            assert release.wait(2)
            return _result("old-wallhaven", page=1)
        return _result("new-motionbgs", page=1)

    monkeypatch.setattr(dialog._browser, "search", search)
    dialog.start_search(page=1)
    assert started.wait(2)

    dialog._providers.set_selected(1)
    assert dialog.provider_name == "motionbgs"
    assert dialog._stack.get_visible_child_name() == "empty"
    assert dialog._cards == []

    # A new-provider request may be queued while the old worker finishes. Its
    # generation wins; the old completion is ignored when it reaches GTK.
    dialog.start_search(page=1)
    release.set()
    assert _settle(
        lambda: [card.candidate.identifier for card in dialog._cards] == ["new-motionbgs"]
    )
    assert asked == ["wallhaven", "motionbgs"]


def test_editing_the_query_discards_the_in_flight_old_answer(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grid must never describe controls that no longer made its request."""
    started = threading.Event()
    release = threading.Event()

    def search(_name: str, _query: object) -> SearchResult:
        started.set()
        assert release.wait(2)
        return _result("old-query", page=1)

    monkeypatch.setattr(dialog._browser, "search", search)
    dialog._entry.set_text("first")
    dialog.start_search(page=1)
    assert started.wait(2)

    dialog._entry.set_text("second")
    release.set()

    assert _settle(lambda: not dialog._searching)
    assert dialog._cards == []
    assert dialog._stack.get_visible_child_name() == "empty"
    assert dialog._status.get_title() == "Search changed"


def test_changed_query_can_queue_behind_an_in_flight_search(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale worker must not force a second click after it eventually returns."""
    started = threading.Event()
    release = threading.Event()
    asked: list[str] = []

    def search(_name: str, query: object) -> SearchResult:
        text = str(getattr(query, "text", ""))
        asked.append(text)
        if text == "first":
            started.set()
            assert release.wait(2)
            return _result("old-query", page=1)
        return _result("new-query", page=1)

    monkeypatch.setattr(dialog._browser, "search", search)
    dialog._entry.set_text("first")
    dialog.start_search(page=1)
    assert started.wait(2)

    dialog._entry.set_text("second")
    dialog.start_search(page=1)
    release.set()

    assert _settle(lambda: [card.candidate.identifier for card in dialog._cards] == ["new-query"])
    assert asked == ["first", "second"]


def test_newest_query_supersedes_a_queued_intermediate_search(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    asked: list[str] = []

    def search(_name: str, query: object) -> SearchResult:
        text = str(getattr(query, "text", ""))
        asked.append(text)
        if text == "first":
            started.set()
            assert release.wait(2)
        return _result(text, page=1)

    monkeypatch.setattr(dialog._browser, "search", search)
    try:
        dialog._entry.set_text("first")
        dialog.start_search(page=1)
        assert started.wait(2)
        for text in ("obsolete", "latest"):
            dialog._entry.set_text(text)
            dialog.start_search(page=1)
    finally:
        release.set()
    assert _settle(lambda: not dialog._searching)
    assert asked == ["first", "latest"]
    assert [card.candidate.identifier for card in dialog._cards] == ["latest"]


def test_cached_pages_and_details_do_not_wait_for_ownership_after_refresh(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_thread = threading.get_ident()
    started = threading.Event()
    release = threading.Event()
    read_threads: list[int] = []
    candidate = _candidate(identifier="aaa111")
    index = owned.Index()
    index.add(candidate, Path("/offline/wallpaper.png"))

    def read(_roots: object) -> owned.Index:
        read_threads.append(threading.get_ident())
        started.set()
        assert release.wait(10)
        return index

    # Start from a searched page with a ready, empty ownership snapshot.
    _ = dialog._browser.owned
    dialog._show_result(_result("aaa111", page=1), page=1)
    monkeypatch.setattr(owned, "read", read)
    monkeypatch.setattr(dialog._browser, "describe", lambda item: CandidateDetail(candidate=item))
    monkeypatch.setattr(dialog._browser, "preview", lambda _url: b"")
    try:
        dialog.library_refreshed()
        assert started.wait(2)
        dialog._materialize_page(0)
        dialog._open_detail(dialog._cards[0].candidate)
        assert read_threads and main_thread not in read_threads
        initially_pickable = dialog._cards[0].can_pick
        assert initially_pickable
    finally:
        release.set()
    assert _settle(lambda: dialog._ownership_future is None)
    assert not dialog._cards[0].can_pick
    detail = next(iter(dialog._detail_dialogs.values()))
    assert detail._download.get_label() == "In your library"


def test_changing_library_roots_retargets_browse_without_losing_the_query(
    dialog: browse_dialog.BrowseDialog,
    tmp_path: Path,
) -> None:
    dialog._entry.set_text("mountains")
    dialog._show_result(_result("aaa111", page=1), page=1)
    replacement = tmp_path / "replacement"
    replacement.mkdir()

    dialog.update_library_roots((replacement,))

    assert dialog._browser.download_root() == replacement
    assert dialog._entry.get_text() == "mountains"
    assert dialog._cards == []
    assert dialog._stack.get_visible_child_name() == "empty"
    assert dialog._status.get_title() == "Library folders changed"


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
    assert _settle(lambda: not dialog._searching)
    dialog._show_next_page()
    assert _settle(lambda: len(calls) >= 2 and not dialog._searching)

    assert len(dialog._cards) == 2
    assert dialog._stack.get_visible_child_name() == "results"
    assert not dialog._has_next


def test_failed_fresh_search_clears_invisible_previous_batch_actions(
    dialog: browse_dialog.BrowseDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error page must not download checked cards hidden behind it."""
    _answer(dialog, monkeypatch, {1: _result("aaa111", "bbb222", page=1)})
    dialog.start_search(page=1)
    assert _settle(lambda: len(dialog._cards) == 2)
    dialog._cards[0]._check.set_active(True)
    assert dialog._download_picked.get_visible()

    def fail(_name: str, _query: object) -> SearchResult:
        raise ProviderError("network", "the site is unavailable")

    monkeypatch.setattr(dialog._browser, "search", fail)
    dialog._entry.set_text("a different query")
    dialog.start_search(page=1)

    assert not dialog._picked_keys
    assert not dialog._candidates
    assert not dialog._cards
    assert not dialog._download_picked.get_visible()
    assert _settle(lambda: not dialog._searching)
    assert dialog._stack.get_visible_child_name() == "empty"
    assert dialog._status.get_title() == "That search did not work"


def test_the_summary_counts_everything_on_screen_not_the_last_page(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count covers retained metadata, not only forty materialised cards."""
    _answer(
        dialog,
        monkeypatch,
        {
            1: _result("aaa111", "bbb222", page=1, has_next=True),
            2: _result("ccc333", page=2, has_next=False),
        },
    )

    dialog.start_search(page=1)
    assert _settle(lambda: not dialog._searching)
    dialog._show_next_page()
    assert _settle(lambda: len(dialog._candidates) == 3)
    assert dialog._summary.get_label().startswith("3 results")


def test_six_hundred_results_keep_only_one_bounded_widget_page_and_the_last_is_reachable(
    dialog: browse_dialog.BrowseDialog,
) -> None:
    identifiers = tuple(f"item-{index}" for index in range(600))
    dialog._show_result(_result(*identifiers), page=1)

    assert len(dialog._candidates) == 600
    assert len(dialog._shown) == 600
    assert len(dialog._cards) == browse_dialog.MAX_MATERIALIZED_RESULTS
    assert len(dialog._result_pages) == 15
    for _page in range(14):
        dialog._show_next_page()

    assert dialog._result_page == 14
    assert len(dialog._cards) == browse_dialog.MAX_MATERIALIZED_RESULTS
    assert dialog._cards[-1].candidate.identifier == "item-599"
    assert dialog._result_page_label.get_label() == "Page 15 of 15"


def test_candidate_metadata_has_an_honest_independent_ceiling(
    dialog: browse_dialog.BrowseDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browse_dialog, "MAX_RETAINED_CANDIDATES", 50)
    identifiers = tuple(f"item-{index}" for index in range(51))

    dialog._show_result(_result(*identifiers, has_next=True), page=1)

    assert len(dialog._candidates) == 50
    assert len(dialog._cards) == browse_dialog.MAX_MATERIALIZED_RESULTS
    assert not dialog._has_next
    assert dialog._summary.get_label().startswith("showing the first 50 results")


def test_turning_pages_preserves_search_cursor_filter_state_selection_and_focus(
    dialog: browse_dialog.BrowseDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifiers = tuple(f"item-{index}" for index in range(80))
    dialog._entry.set_text("naruto wallpapers")
    dialog._entry.set_position(6)
    dialog._filters.set_active(True)
    dialog._show_result(_result(*identifiers), page=1)
    dialog._cards[3].set_picked(True)
    focused: list[str] = []

    def record_focus(card: browse_dialog._CandidateCard) -> bool:
        focused.append(card.candidate.identifier)
        return True

    monkeypatch.setattr(
        browse_dialog._CandidateCard,
        "grab_focus",
        record_focus,
    )

    dialog._show_next_page()
    assert _settle(lambda: "item-40" in focused)
    assert dialog._cards[0].candidate.identifier == "item-40"
    assert dialog._entry.get_text() == "naruto wallpapers"
    assert dialog._entry.get_position() == 6
    assert dialog._filters.get_active()
    assert dialog._picked.get_label() == "1 selected"

    focused.clear()
    dialog._show_previous_page()
    assert _settle(lambda: "item-0" in focused)
    assert dialog._cards[3].picked
    assert dialog._entry.get_position() == 6
    assert focused[-1] == "item-0"


def test_a_stale_preview_completion_cannot_mutate_a_recreated_card(
    dialog: browse_dialog.BrowseDialog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog._show_result(_result("same-id"), page=1)
    old_generation = dialog._preview_generation
    dialog._show_result(_result("same-id"), page=1)
    card = dialog._cards[0]
    delivered: list[bytes] = []
    monkeypatch.setattr(card, "set_preview", lambda data: delivered.append(data))

    dialog._on_preview(old_generation, card.candidate, b"stale")
    assert delivered == []

    dialog._on_preview(dialog._preview_generation, card.candidate, b"current")
    assert delivered == [b"current"]


def test_a_newly_materialized_page_rebuilds_owned_and_in_flight_badges(
    dialog: browse_dialog.BrowseDialog,
) -> None:
    identifiers = tuple(f"item-{index}" for index in range(80))
    dialog._show_result(_result(*identifiers), page=1)
    held = dialog._candidates[41]
    dialog._browser.owned.add(held, Path("/offline/held-item"))
    busy = dialog._candidates[42]
    dialog._downloads_in_flight.add(browse_dialog._candidate_key(busy))

    dialog._show_next_page()

    held_card = dialog._cards[1]
    busy_card = dialog._cards[2]
    assert held_card.candidate.identifier == "item-41"
    assert not held_card._button.get_sensitive()
    assert held_card._button.get_tooltip_text() == "In your library"
    assert not busy_card._button.get_sensitive()
    assert busy_card._button.get_tooltip_text() == "Downloading"


def test_an_unlaid_out_grid_only_queues_a_viewport_sized_preview_window(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = tuple(
        WallpaperCandidate(
            provider="wallhaven",
            identifier=f"item-{index}",
            title=f"item-{index}",
            kind=Kind.STILL,
            page_url=f"https://wallhaven.cc/w/item-{index}",
            thumbnail_url=f"https://example.invalid/{index}.jpg",
        )
        for index in range(browse_dialog.PREVIEW_FALLBACK_CARDS + 10)
    )
    result = SearchResult(
        provider="wallhaven",
        query_url="https://wallhaven.cc/api/v1/search",
        items=candidates,
        page=1,
        has_next=False,
    )
    queued: list[tuple[WallpaperCandidate, float]] = []
    monkeypatch.setattr(
        dialog._loader,
        "prioritize",
        lambda candidates, _callback: queued.extend(candidates),
    )

    dialog._show_result(result, page=1)
    dialog._refresh_previews()

    assert [candidate.identifier for candidate, _priority in queued][
        -browse_dialog.PREVIEW_FALLBACK_CARDS :
    ] == [f"item-{index}" for index in range(browse_dialog.PREVIEW_FALLBACK_CARDS)]


def test_preview_queue_prefers_nearby_cards_and_drops_stale_work() -> None:
    class RecordingPool:
        def __init__(self) -> None:
            self.submitted: list[str] = []

        def submit(
            self,
            _function: Callable[[WallpaperCandidate], bytes],
            candidate: WallpaperCandidate,
        ) -> Future[bytes]:
            self.submitted.append(candidate.identifier)
            return Future()

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            pass

    def preview_candidate(index: int) -> WallpaperCandidate:
        return WallpaperCandidate(
            provider="wallhaven",
            identifier=f"item-{index}",
            title=f"item-{index}",
            kind=Kind.STILL,
            page_url=f"https://wallhaven.cc/w/item-{index}",
            thumbnail_url=f"https://example.invalid/{index}.jpg",
        )

    loader = browse_dialog.PreviewLoader(cast(Browser, SimpleNamespace()), max_workers=2)
    loader._pool.shutdown(wait=False, cancel_futures=True)
    pool = RecordingPool()
    loader._pool = pool  # type: ignore[assignment]
    try:
        far, near, middle, current = (preview_candidate(index) for index in range(4))
        loader.prioritize(((far, 50.0), (near, 0.0), (middle, 20.0)), lambda *_args: None)

        assert pool.submitted == ["item-1", "item-2"]

        loader.prioritize(((current, 0.0),), lambda *_args: None)

        assert tuple(loader._waiting) == (current.thumbnail_url,)
        assert far.thumbnail_url not in loader._waiting
    finally:
        loader.shutdown()


def test_the_preview_memory_cache_evicts_the_least_recently_used() -> None:
    loader = browse_dialog.PreviewLoader(
        cast(Browser, SimpleNamespace()), max_workers=1, max_cache_entries=2
    )
    delivered: list[str] = []
    try:
        for index in range(3):
            candidate = WallpaperCandidate(
                provider="wallhaven",
                identifier=f"item-{index}",
                title=f"item-{index}",
                kind=Kind.STILL,
                page_url=f"https://wallhaven.cc/w/item-{index}",
                thumbnail_url=f"https://example.invalid/{index}.jpg",
            )
            future: Future[bytes] = Future()
            future.set_result(str(index).encode())
            loader._desired.add(candidate.thumbnail_url)
            loader._active.add(candidate.thumbnail_url)
            loader._finish(candidate, future, lambda item, _data: delivered.append(item.identifier))

        assert tuple(loader._cache) == (
            "https://example.invalid/1.jpg",
            "https://example.invalid/2.jpg",
        )
    finally:
        loader.shutdown()


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


def test_one_candidate_cannot_be_queued_twice_while_downloading(
    dialog: browse_dialog.BrowseDialog,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Card, detail and batch controls share one in-flight identity."""
    _three(dialog, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    asked: list[str] = []

    def download(candidate: WallpaperCandidate, *, variant: str = "") -> Downloaded:
        asked.append(candidate.identifier)
        started.set()
        assert release.wait(2)
        return _downloaded(tmp_path, candidate.identifier)

    monkeypatch.setattr(dialog._browser, "download", download)
    card = dialog._cards[0]
    card._check.set_active(True)
    dialog._on_download(card.candidate)
    assert started.wait(2)

    dialog._on_download(card.candidate, variant="4k")

    assert asked == ["aaa111"]
    assert not card._button.get_sensitive()
    assert not card._check.get_sensitive()
    assert not card.picked
    assert dialog._queued == 1

    release.set()
    assert _settle(lambda: not dialog._downloads_in_flight)


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


def test_many_detail_views_share_one_bounded_worker_lane(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening many candidates cannot create one provider thread per dialog."""
    started: list[str] = []
    release = threading.Event()

    def describe(candidate: WallpaperCandidate) -> CandidateDetail:
        started.append(candidate.identifier)
        release.wait(timeout=5)
        return CandidateDetail(candidate=candidate)

    monkeypatch.setattr(dialog._browser, "describe", describe)
    monkeypatch.setattr(dialog._browser, "preview", lambda _url: b"")

    for index in range(8):
        dialog._open_detail(_candidate(identifier=f"detail-{index}"))

    try:
        assert _settle(lambda: len(started) == 2)
        # The remaining six requests are queued behind the shared two-worker
        # lane; another scheduler turn cannot start a third one.
        time.sleep(0.05)
        assert len(started) == 2
        assert {detail._pool for detail in dialog._detail_dialogs.values()} == {dialog._details}
        assert not any(detail._owns_pool for detail in dialog._detail_dialogs.values())
    finally:
        dialog._on_closed(dialog)
        release.set()


def test_closing_browse_cancels_transport_and_refuses_late_work(
    dialog: browse_dialog.BrowseDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    submitted: list[bool] = []
    monkeypatch.setattr(dialog._browser, "shutdown", lambda: closed.append(True))
    monkeypatch.setattr(dialog._searches, "submit", lambda *_args: submitted.append(True))

    dialog._on_closed(dialog)
    dialog.start_search(page=1)
    dialog._on_download(_candidate())

    assert closed == [True]
    assert submitted == []
    assert not dialog._downloads_in_flight


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
    """On a paged photo grid, "all" means the current bounded page."""
    _three(dialog, monkeypatch)

    dialog._pick_all()

    assert all(card.picked for card in dialog._cards)
    assert dialog._picked.get_label() == "3 selected"


def test_select_all_is_page_bounded_and_keeps_earlier_page_picks(
    dialog: browse_dialog.BrowseDialog,
) -> None:
    identifiers = tuple(f"item-{index}" for index in range(80))
    dialog._show_result(_result(*identifiers), page=1)

    dialog._pick_all()
    assert len(dialog._picked_keys) == browse_dialog.MAX_MATERIALIZED_RESULTS

    dialog._show_next_page()
    assert not any(card.picked for card in dialog._cards)
    dialog._pick_all()
    assert len(dialog._picked_keys) == 80


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


def test_shortcuts_are_attached_to_the_reparented_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registry,
        "describe",
        lambda: (
            SimpleNamespace(name="wallhaven", title="Wallhaven", usable=True, limitations=()),
            SimpleNamespace(name="motionbgs", title="MotionBGS", usable=True, limitations=()),
        ),
    )
    page = browse_dialog.BrowsePage(_StubApp(tmp_path))  # type: ignore[arg-type]
    try:
        assert page._surface._shortcuts.get_widget() is page.get_first_child()
    finally:
        page.shutdown()
