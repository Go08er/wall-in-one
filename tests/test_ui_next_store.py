"""The new interface's Store page over the classic `Browser`, with no network.

`RealStore` is built over a `Browser` whose provider answers are scripted
(`StubBrowser`): searching, describing and card pictures are stubbed, while
claims, ownership walks, roots and downloads into the library folder are the
real `Browser`'s, over a stub provider that writes a real file and sidecar.
The page is the shipped `StorePage`, over a `RealAppState` whose application
is the fake from `test_next_state` plus the two things the Store needs
(`refresh_library` with its completion, and the authoring gate). The
application's own completion hook is tested against a real `Application`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.shown_text import shown_texts  # noqa: E402
from tests.test_browse import _record_download  # noqa: E402
from tests.test_next_state import NAMES, FakeApplication, _library, _session  # noqa: E402
from tests.test_providers_fakes import FakeClient, png_bytes  # noqa: E402
from tests.test_ui_library_scan import (  # noqa: E402
    _ScanWindow,
    _stop_scan_application,
    _wire_scan_application,
)
from wall_in_one import browse, config  # noqa: E402
from wall_in_one.browse import Browser  # noqa: E402
from wall_in_one.library import owned  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.providers import credentials, registry  # noqa: E402
from wall_in_one.providers.base import (  # noqa: E402
    CandidateDetail,
    DownloadResult,
    Fact,
    ProviderError,
    SearchQuery,
    SearchResult,
    WallpaperCandidate,
)
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.prefs import UiPrefsKeeper  # noqa: E402
from wall_in_one.ui.next.real_state import RealAppState  # noqa: E402
from wall_in_one.ui.next.real_store import CandidateView, RealStore, StoreBackend  # noqa: E402
from wall_in_one.ui.next.store import StorePage  # noqa: E402
from wall_in_one.ui.next.store_state import (  # noqa: E402
    MOTIONBGS,
    WALLHAVEN,
    StoreAccess,
    StoreDetail,
    StoreItemView,
)

#: Tests run on GTK's thread; every worker answer must come from another.
GTK_THREAD = threading.get_ident()


def _conforms(store: RealStore, application: Application) -> tuple[StoreAccess, StoreBackend]:
    """mypy --strict checks the adapter and the application against the Protocols."""
    return store, application


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


# -- the scripted providers -----------------------------------------------------------------


def candidate(identifier: str, provider: str = WALLHAVEN) -> WallpaperCandidate:
    moving = provider == MOTIONBGS
    return WallpaperCandidate(
        provider=provider,
        identifier=identifier,
        title=f"Wallpaper {identifier}",
        kind=Kind.VIDEO if moving else Kind.STILL,
        page_url=f"https://wallhaven.cc/w/{identifier}"
        if not moving
        else f"https://motionbgs.com/{identifier}",
        thumbnail_url=f"https://th.wallhaven.cc/small/{identifier[:2]}/{identifier}.jpg",
        resolution="4K" if moving else "3840x2160",
    )


def result(*identifiers: str, provider: str = WALLHAVEN, has_next: bool = False) -> SearchResult:
    return SearchResult(
        provider=provider,
        query_url="https://example.invalid/search",
        items=tuple(candidate(name, provider) for name in identifiers),
        has_next=has_next,
        total_hint=500,
    )


def ids(count: int, prefix: str = "aa", start: int = 0) -> list[str]:
    return [f"{prefix}{index:04d}" for index in range(start, start + count)]


@dataclass
class Script:
    """What the providers answer, and every call that reached them (and on which thread)."""

    pages: dict[tuple[str, str, int], SearchResult] = field(default_factory=dict)
    queries: list[tuple[str, SearchQuery]] = field(default_factory=list)
    threads: list[tuple[str, int]] = field(default_factory=list)
    gates: dict[str, threading.Event] = field(default_factory=dict)
    downloads: list[tuple[str, str]] = field(default_factory=list)
    during_download: Callable[[], None] | None = None
    #: Raised by ``describe`` when set: a preview whose details fail.
    describe_error: ProviderError | None = None

    def answer(self, name: str, query: SearchQuery) -> SearchResult:
        found = self.pages.get((name, query.text, query.page))
        if found is not None:
            return found
        prefix = "mb" if name == MOTIONBGS else (query.text[:2] or "aa")
        return result(*ids(3, prefix), provider=name)


class StubProvider:
    def __init__(self, script: Script) -> None:
        self._script = script

    def download(
        self, selected: WallpaperCandidate, destination: Path, *, variant: str = ""
    ) -> DownloadResult:
        self._script.threads.append(("download", threading.get_ident()))
        self._script.downloads.append((selected.identifier, variant))
        if self._script.during_download is not None:
            self._script.during_download()
        return _record_download(destination, selected)


class StubBrowser(Browser):
    """The real Browser (claims, ownership, roots, downloads) over scripted providers."""

    def __init__(self, roots: tuple[Path, ...], script: Script) -> None:
        super().__init__(client=FakeClient(), root=roots[0] if roots else None, library_roots=roots)
        self.script = script

    def provider(self, name: str) -> StubProvider:  # type: ignore[override]
        return StubProvider(self.script)

    def search(self, name: str, query: SearchQuery) -> SearchResult:
        self.script.threads.append(("search", threading.get_ident()))
        self.script.queries.append((name, query))
        gate = self.script.gates.get(query.text)
        if gate is not None:
            assert gate.wait(5)
        _ = self.owned  # as Browser.search does: warm the index on the worker
        return self.script.answer(name, query)

    def describe(self, wanted: WallpaperCandidate) -> CandidateDetail:
        self.script.threads.append(("describe", threading.get_ident()))
        if self.script.describe_error is not None:
            raise self.script.describe_error
        return CandidateDetail(
            candidate=wanted,
            preview_url=f"https://w.wallhaven.cc/full/{wanted.identifier}.png",
            facts=(Fact("Resolution", "3840x2160"),),
            tags=("sky",),
            colours=("0066cc",),
            variants=("hd", "4k") if wanted.provider == MOTIONBGS else (),
        )

    def thumbnail(self, wanted: WallpaperCandidate) -> bytes:
        self.script.threads.append(("thumbnail", threading.get_ident()))
        return png_bytes()

    def preview(self, url: str) -> bytes:
        self.script.threads.append(("preview", threading.get_ident()))
        return png_bytes(8, 6)


class StoreApplication(FakeApplication):
    """`test_next_state`'s fake application, plus what the Store asks of `Application`."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.refreshes: list[Callable[[bool], None] | None] = []
        self.ready = True

    def refresh_library(self, *, finished: Callable[[bool], None] | None = None) -> None:
        self.refreshes.append(finished)

    def require_authoring_ready(self, *, report: bool = True) -> bool:
        if not self.ready and report:
            self.reports.append("authoring is still opening")
        return self.ready


@dataclass
class Store:
    backend: StoreApplication
    state: RealAppState
    store: RealStore
    script: Script
    root: Path
    toasts: list[str]
    navigations: list[str]

    def page(self) -> StorePage:
        page = StorePage(self.state, self.store)
        self.pages.append(page)
        return page

    pages: list[StorePage] = field(default_factory=list)

    def land(self, path: Path) -> None:
        """The application's rescan found ``path`` and the window adopted it."""
        session = self.backend.session
        item = MediaItem(path=path, kind=Kind.STILL, size=path.stat().st_size, mtime=1)
        library = Library(roots=session.library.roots, items=(*session.library.items, item))
        session.adopt_library(library, reconcile_workshop=False)
        self.state.reload()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    items = _library(tmp_path / "home" / "Pictures" / "Wallpapers")
    assert len(items) == len(NAMES)
    session = _session(items)
    backend = StoreApplication(session)
    state = RealAppState(backend, UiPrefsKeeper(backend.window_report))
    script = Script()
    real = RealStore(backend, state, browser=lambda roots: StubBrowser(roots, script))
    toasts: list[str] = []
    navigations: list[str] = []
    state.connect("toast", lambda _state, text, _button: toasts.append(text))
    state.connect("navigate", lambda _state, key: navigations.append(key))
    built = Store(backend, state, real, script, items[0].path.parent, toasts, navigations)
    try:
        yield built
    finally:
        real.close()
        state.close()
        session.shutdown()


def _shown(page: StorePage) -> list[str]:
    return [item.id.partition(":")[2] for item in page.pages.current]


def selecting(page: StorePage) -> bool:
    """Read afresh (mypy would keep an asserted attribute narrowed)."""
    return page.select_mode


def _searched(page: StorePage) -> None:
    spin_until(lambda: not page.loading and not page.fetching, 5)


def _last_options(store: Store) -> dict[str, str]:
    return dict(store.script.queries[-1][1].options)


# -- the application's completion hook ---------------------------------------------------------


def test_a_refresh_waiter_hears_after_the_library_is_shown_and_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    wallpaper = root / "fresh.png"
    wallpaper.write_bytes(png_bytes())
    fail = threading.Event()

    def scanner(roots: Sequence[Path] | None) -> Library:
        if fail.is_set():
            raise OSError("the drive went away")
        item = MediaItem(path=wallpaper, kind=Kind.STILL, size=1, mtime=1)
        return Library(roots=tuple(roots or ()), items=(item,))

    application = Application()
    session = Session(
        config.Settings(roots=(root,), scan_workshop=False).validated(), scanner=scanner
    )
    window = _ScanWindow()
    _wire_scan_application(application, session, window, monkeypatch)
    heard: list[tuple[bool, int, bool]] = []

    def finished(installed: bool) -> None:
        heard.append(
            (installed, len(window.libraries), session.library.find(wallpaper) is not None)
        )

    try:
        application.refresh_library(finished=finished)
        assert heard == [], "nothing is said before the scan lands"
        spin_until(lambda: bool(heard), 5)
        assert heard == [(True, 1, True)], "after show_library, with the file in the library"

        # A plain refresh is the classic one: nobody waits on it.
        application.refresh_library()
        spin_until(lambda: len(window.libraries) == 2, 5)
        assert len(heard) == 1

        # A waiter that fails does not turn a good install into a failed one.
        def broken(_installed: bool) -> None:
            raise RuntimeError("the waiter broke")

        application.refresh_library(finished=broken)
        spin_until(lambda: len(window.libraries) == 3, 5)
        assert "After the library refresh: the waiter broke" in window.reports
        assert not any("could not be installed" in report for report in window.reports)

        fail.set()
        application.refresh_library(finished=finished)
        spin_until(lambda: len(heard) == 2, 5)
        assert heard[1][0] is False, "a failed scan says so rather than leaving the waiter"
        assert any("the drive went away" in report for report in window.reports)
    finally:
        _stop_scan_application(application, session)


# -- searching ----------------------------------------------------------------------------------


def test_nothing_is_asked_until_the_page_is_shown_and_then_off_gtk(store: Store) -> None:
    page = store.page()
    assert store.script.queries == [], "building the window must not reach a website"
    page.activate(None)
    _searched(page)
    assert _shown(page) == ids(3)
    assert store.script.queries[0][0] == WALLHAVEN
    spin_until(lambda: all(card.picture.paintable is not None for card in page.cards.values()), 5)
    page.preview(page.pages.current[0])
    assert page.dialog is not None
    spin_until(lambda: page.dialog is not None and page.dialog.detail is not None, 5)
    assert page.dialog.detail is not None and page.dialog.detail.facts
    kinds = {kind for kind, _thread in store.script.threads}
    assert {"search", "thumbnail", "describe", "preview"} <= kinds
    assert all(thread != GTK_THREAD for _kind, thread in store.script.threads)
    page.dialog.force_close()


def test_an_ownership_walk_never_runs_on_gtk(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    walks: list[int] = []
    read = owned.read

    def recorded(roots: Sequence[Path]) -> owned.Index:
        walks.append(threading.get_ident())
        return read(roots)

    monkeypatch.setattr(owned, "read", recorded)
    page = store.page()
    page.activate(None)
    _searched(page)
    store.state.reload()  # the library changed: the index is read again
    spin_until(lambda: len(walks) >= 2, 5)
    assert all(thread != GTK_THREAD for thread in walks)


def test_the_filters_reach_wallhaven_as_its_own_options(store: Store) -> None:
    page = store.page()
    page.activate(None)
    _searched(page)
    page.filters_button.set_active(True)
    assert page.revealer.get_reveal_child(), "the filters are inline, in a revealer"
    page.size.set_selected(3)  # 4K or larger
    _searched(page)
    page.ratio.set_selected(1)  # 16:9
    _searched(page)
    page.categories.chips["People"].set_active(False)
    _searched(page)
    page.purity.chips["Sketchy"].set_active(True)
    _searched(page)
    page.palette.buttons["0066cc"].emit("clicked")
    _searched(page)
    options = _last_options(store)
    assert options["atleast"] == "3840x2160"
    assert options["ratios"] == "16x9"
    assert options["categories"] == "110"
    assert options["purity"] == "110"
    assert options["colors"] == "0066cc"
    assert options["top_range"] == "1w"
    assert page.top_range_row.get_visible(), "Top list covers, for the top list"

    page.sort.set_selected(next(i for i, row in enumerate(browse_sorts()) if row == "views"))
    _searched(page)
    assert "top_range" not in _last_options(store)
    assert not page.top_range_row.get_visible(), "and only for the top list"

    # Clicking the chosen colour again is "any colour".
    page.palette.buttons["0066cc"].emit("clicked")
    _searched(page)
    assert "colors" not in _last_options(store)


def browse_sorts() -> list[str]:
    from wall_in_one.ui.next.store_state import SORTS

    return [key for key, _label in SORTS]


def test_motionbgs_browses_by_genre_and_downloads_the_chosen_quality(store: Store) -> None:
    page = store.page()
    page.activate(MOTIONBGS)
    _searched(page)
    assert store.script.queries[-1] == (
        MOTIONBGS,
        SearchQuery(text="", page=1, options={"mode": "latest"}),
    )
    page.mode.set_selected(3)  # By genre
    _searched(page)
    assert page.problem is not None and "type a genre" in page.problem.message
    # The error page reads as a sentence: the kind is for code and toasts.
    description = page._error.get_description() or ""
    assert description.startswith("type a genre") and "validation:" not in description
    assert page.problem.reported.startswith("validation: type a genre")
    page.genre.set_text("anime")
    page.genre.emit("activate")
    _searched(page)
    assert _last_options(store) == {"mode": "genre", "genre": "anime"}

    page.quality.set_selected(2)  # HD
    assert not page.loading, "the quality is the download's, not the search's"
    item = page.pages.current[0]
    page.download(item)
    spin_until(lambda: bool(store.script.downloads), 5)
    assert store.script.downloads == [(item.id.partition(":")[2], "hd")]


def test_pages_hold_forty_cards_at_most_with_previous_and_next(store: Store) -> None:
    store.script.pages[(WALLHAVEN, "", 1)] = result(*ids(100), has_next=True)
    store.script.pages[(WALLHAVEN, "", 2)] = result(*ids(30, start=100))
    page = store.page()
    page.activate(None)
    _searched(page)
    assert page.pages.pages == 3 and len(page.cards) == 40
    assert page.page_label.get_label() == "Page 1 of 3 · more online"
    assert not page.previous.get_sensitive() and page.next.get_sensitive()
    page.show_next()
    page.show_next()
    assert _shown(page) == ids(20, start=80) and len(page.cards) == 20
    asked = len(store.script.queries)
    page.show_next()  # past every kept page: the provider's next page
    _searched(page)
    assert len(store.script.queries) == asked + 1
    assert store.script.queries[-1][1].page == 2
    assert _shown(page) == ids(30, start=100)
    assert page.pages.index == 3 and not page.next.get_sensitive()
    page.show_previous()
    assert _shown(page) == ids(20, start=80)
    assert all(len(cards) <= 40 for cards in [page.cards])


def test_a_superseded_search_never_reaches_the_grid(store: Store) -> None:
    gate = threading.Event()
    store.script.gates["old"] = gate
    store.script.pages[(WALLHAVEN, "old", 1)] = result(*ids(2, "ol"))
    store.script.pages[(WALLHAVEN, "new", 1)] = result(*ids(2, "nw"))
    page = store.page()
    page.activate(None)
    _searched(page)
    page.set_text("old")
    spin_until(lambda: any(query.text == "old" for _n, query in store.script.queries), 5)
    page.set_text("new")  # queued behind the running one on the single search lane
    gate.set()
    _searched(page)
    spin_until(lambda: any(query.text == "new" for _n, query in store.script.queries), 5)
    _searched(page)
    assert _shown(page) == ids(2, "nw")
    assert page._heading.get_label() == "Results for “new”"


def test_p7_a_reused_page_asks_again_for_the_search_its_closed_adapter_dropped(
    store: Store,
) -> None:
    """Codex P-7: the adapter closed while the opening search was out; the page is shown again."""
    gate = threading.Event()
    store.script.gates[""] = gate
    page = store.page()
    page.activate(None)
    spin_until(lambda: len(store.script.queries) == 1, 5)
    assert page.loading and store.store.searching()
    store.store.close()  # the window went away: its answer will never come
    assert not store.store.searching()
    gate.set()
    page.activate(None)  # the window is back, with the same page
    _searched(page)
    assert len(store.script.queries) == 2, "the dropped search was asked for again"
    assert _shown(page) == ids(3)
    page.activate(None)  # shown again with nothing outstanding: no new search
    assert len(store.script.queries) == 2


# -- what the library holds ------------------------------------------------------------------


def test_a_result_the_library_holds_says_in_library(store: Store) -> None:
    _record_download(store.root, candidate("aa0001"))
    page = store.page()
    page.activate(None)
    _searched(page)
    held = page.cards["wallhaven:aa0001"]
    spin_until(lambda: held.in_library, 5)
    assert held.apply_button.get_visible() and not held.download_button.get_visible()
    others = [card for key, card in page.cards.items() if key != "wallhaven:aa0001"]
    assert others and not any(card.in_library for card in others)
    assert not held.can_pick, "what is in the library can't be picked for download"


# -- downloading ------------------------------------------------------------------------------


def test_download_and_apply_waits_for_the_rescan_then_applies(store: Store) -> None:
    page = store.page()
    page.activate(None)
    _searched(page)
    item = page.pages.current[0]
    page.download(item, apply=True)
    card = page.cards[item.id]
    assert card.busy == "downloading"
    assert page.queue_label.get_label() == "Downloading 1 of 1"
    spin_until(lambda: bool(store.backend.refreshes), 5)
    assert store.backend.played == [], "not before the library holds it"
    finished = store.backend.refreshes[-1]
    assert finished is not None
    path = store.root / "Wall-in-One" / "Wallhaven" / f"wallhaven-{item.id.partition(':')[2]}.png"
    assert path.is_file()
    store.land(path)
    finished(True)
    assert store.backend.played == [(path, None)]
    assert card.busy == "" and page.queue_label.get_label() == ""
    spin_until(lambda: card.in_library, 5)


def test_show_in_library_after_a_download_opens_it_there(store: Store) -> None:
    page = store.page()
    page.activate(None)
    _searched(page)
    item = page.pages.current[1]
    store.store.download(item, then="show")
    spin_until(lambda: bool(store.backend.refreshes), 5)
    path = store.root / "Wall-in-One" / "Wallhaven" / f"wallhaven-{item.id.partition(':')[2]}.png"
    store.land(path)
    finished = store.backend.refreshes[-1]
    assert finished is not None
    finished(True)
    assert store.navigations == [f"library:{path}"]


def test_a_rescan_that_fails_says_the_download_was_not_applied(store: Store) -> None:
    page = store.page()
    page.activate(None)
    _searched(page)
    page.download(page.pages.current[0], apply=True)
    spin_until(lambda: bool(store.backend.refreshes), 5)
    finished = store.backend.refreshes[-1]
    assert finished is not None
    finished(False)
    assert store.backend.played == []
    assert any("could not be rescanned" in report for report in store.backend.reports)


def test_a_preview_whose_details_fail_says_so_in_the_sites_words(store: Store) -> None:
    """As on the error page: the kind is for code, the words are for the person."""
    page = store.page()
    page.activate(None)
    _searched(page)
    item = page.pages.current[0]
    store.script.describe_error = ProviderError("rate-limit", "slow down, please")
    details: list[StoreDetail] = []
    store.store.item_detail(item, details.append)
    spin_until(lambda: bool(details), 5)
    assert details[0].problem == "slow down, please"


def test_with_no_library_folder_download_is_off_and_says_where_to_add_one(
    store: Store,
) -> None:
    """The card's Download looks and is off, with the reason; the refusal opens Settings."""
    page = store.page()
    page.activate(None)
    _searched(page)
    item = page.pages.current[0]
    card = page.cards[item.id]
    assert card.download_button.get_sensitive()
    session = store.backend.session
    session.update_settings(replace(session.settings, roots=()), rescan_library=False)
    store.state.emit_changed("settings")
    reason = store.store.download_blocked()
    assert reason.endswith("Add one in Settings \u2192 Library")
    assert not card.download_button.get_sensitive()
    assert card.download_button.get_tooltip_text() == reason

    buttons: list[tuple[str, Callable[[], None]] | None] = []
    store.state.connect("toast", lambda _state, _text, button: buttons.append(button))
    store.store.download(item)  # a press that reaches it anyway (the menu, a key)
    assert store.toasts == [reason]
    [button] = buttons
    assert button is not None and button[0] == "Settings"
    button[1]()
    assert store.navigations[-1] == "settings:library"
    assert store.script.downloads == [], "nothing was downloaded"


def test_a_plain_download_offers_apply_in_its_toast(store: Store) -> None:
    page = store.page()
    page.activate(None)
    _searched(page)
    item = page.pages.current[2]
    page.download(item)
    spin_until(lambda: bool(store.toasts), 5)
    assert store.toasts == [f"“{item.title}” is in your library"]
    assert store.backend.refreshes == [None], "the library is rescanned, nobody waits"


def test_a_download_into_a_folder_replaced_meanwhile_is_not_applied(
    store: Store, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    page = store.page()
    page.activate(None)
    _searched(page)
    item = page.pages.current[0]
    lanes = store.store._lanes
    assert lanes is not None
    # Settings swaps the folders while the transfer runs.
    store.script.during_download = lambda: lanes.browser.configure_roots(
        root=other, library_roots=(other,)
    )
    page.download(item, apply=True)
    spin_until(lambda: bool(store.toasts), 5)
    assert "folders changed mid-download" in store.toasts[0]
    assert "It is not in your library" in store.toasts[0]
    assert store.backend.refreshes == [], "nothing to find: the library doesn't scan that folder"
    assert store.backend.played == []
    blocked = store.store.apply_item_blocked(item)
    assert "no longer a library folder" in blocked
    store.store.apply_item(item)
    assert store.backend.played == [] and store.backend.reports[-1] == blocked


def test_one_download_at_a_time_with_n_of_m(store: Store) -> None:
    release = threading.Event()
    store.script.during_download = lambda: (release.wait(5), None)[1]
    page = store.page()
    page.activate(None)
    _searched(page)
    first, second, third = page.pages.current[:3]
    page.toggle_pick(first)
    page.toggle_pick(second)
    page.toggle_pick(third)
    assert selecting(page) and len(page.picked) == 3
    page.download_picked()
    assert not selecting(page) and not page.picked
    assert [page.cards[i.id].busy for i in (first, second, third)] == [
        "downloading",
        "waiting",
        "waiting",
    ]
    assert page.queue_label.get_label() == "Downloading 1 of 3"
    release.set()
    spin_until(lambda: not store.store.downloads.busy, 5)
    assert store.toasts == ["3 wallpapers are in your library"]
    assert all(page.cards[i.id].in_library for i in (first, second, third))


def test_a_download_the_library_already_holds_is_a_conflict(store: Store) -> None:
    page = store.page()
    page.activate(None)
    _searched(page)
    item = page.pages.current[0]
    # Another process lands it after this page read the folders.
    _record_download(store.root, candidate(item.id.partition(":")[2]))
    store.store.download(item)
    spin_until(lambda: not store.store.downloads.busy, 5)
    assert store.script.downloads == [], "provenance is re-read under the claim"
    assert any(report.startswith("conflict:") for report in store.backend.reports)
    spin_until(lambda: page.cards[item.id].in_library, 5)


def test_a_download_whose_report_raised_stops_showing_as_downloading(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AA-3's sibling in the Store: the conflict's report came before the batch ended
    and the page heard, so a report that raised left the card "Downloading…"."""
    page = store.page()
    page.activate(None)
    _searched(page)
    item = page.pages.current[0]
    # Another process lands it after this page read the folders: a conflict, reported.
    _record_download(store.root, candidate(item.id.partition(":")[2]))
    real_report = store.backend.window_report
    raised: list[str] = []

    def report_raises_once(message: str) -> None:
        if not raised:
            raised.append(message)
            raise RuntimeError("injected: the report failed")
        real_report(message)

    monkeypatch.setattr(store.backend, "window_report", report_raises_once)
    store.store.download(item)
    spin_until(lambda: not store.store.downloads.busy and bool(raised), 5)
    # Before: the card kept "Downloading…", and the batch never ended.
    spin_until(lambda: not page.cards[item.id].busy, 5, what="the card to stop downloading")
    assert page.queue_label.get_label() == ""

    other = page.pages.current[1]
    store.store.download(other)
    spin_until(lambda: bool(store.toasts), 5, what="the next download")
    assert store.toasts == [f"\u201c{other.title}\u201d is in your library"]


def test_a_download_another_request_holds_is_busy(store: Store) -> None:
    page = store.page()
    page.activate(None)
    _searched(page)
    item = page.pages.current[0]
    with browse._download_claim(store.root, WALLHAVEN, item.id.partition(":")[2]):
        store.store.download(item)
        spin_until(lambda: not store.store.downloads.busy, 5)
    assert store.script.downloads == []
    assert any(report.startswith("busy:") for report in store.backend.reports)
    assert not page.cards[item.id].busy, "the card is downloadable again"


def test_a_download_waits_for_authoring_to_open(store: Store) -> None:
    store.backend.ready = False
    page = store.page()
    page.activate(None)
    _searched(page)
    page.download(page.pages.current[0])
    assert store.store.downloads.busy == ()
    assert store.backend.reports == ["authoring is still opening"]


# -- the key and the keyboard ---------------------------------------------------------------


def test_a_key_saved_after_the_store_opened_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4ba7b45: the Store keeps one Browser, so a key saved later must still be sent."""
    monkeypatch.delenv("WALLHAVEN_API_KEY", raising=False)
    items = _library(tmp_path / "home" / "Pictures" / "Wallpapers")
    session = _session(items)
    backend = StoreApplication(session)
    state = RealAppState(backend, UiPrefsKeeper(backend.window_report))
    client = FakeClient()  # no routes: every request is recorded, then refused
    real = RealStore(
        backend,
        state,
        browser=lambda roots: Browser(client=client, root=roots[0], library_roots=roots),
    )
    page = StorePage(state, real)
    try:
        page.activate(None)
        _searched(page)
        spin_until(lambda: real.nsfw_blocked == registry.NSFW_NEEDS_KEY, 5)
        # The refusal names the URL, "&" and all: shown as written.
        spin_until(lambda: any("&purity=" in text for text in shown_texts(page.widget)), 5)
        assert not page.purity.chips["NSFW"].get_sensitive()
        assert page.key_hint.get_visible()
        assert page.key_hint_label.get_label() == registry.NSFW_NEEDS_KEY
        assert client.requests and not dict(client.requests[-1].headers)

        credentials.save_key("a" * 32)
        page.filters_button.set_active(True)  # opening the filters asks again
        spin_until(lambda: real.nsfw_blocked == "", 5)
        assert page.purity.chips["NSFW"].get_sensitive() and not page.key_hint.get_visible()
        page.refresh()
        spin_until(lambda: dict(client.requests[-1].headers).get("X-API-Key") == "a" * 32, 5)
    finally:
        real.close()
        state.close()
        session.shutdown()


def _shortcut(page: StorePage, trigger: str) -> Gtk.Shortcut:
    controllers = page.widget.observe_controllers()
    for index in range(controllers.get_n_items()):
        controller = controllers.get_item(index)
        if isinstance(controller, Gtk.ShortcutController):
            for position in range(controller.get_n_items()):
                shortcut = controller.get_item(position)
                if not isinstance(shortcut, Gtk.Shortcut):
                    continue
                found = shortcut.get_trigger()
                if found is not None and found.to_string() == trigger:
                    return shortcut
    raise AssertionError(f"no {trigger} shortcut on the Store page")


def _press(page: StorePage, item: StoreItemView, keyval: int, ctrl: bool = False) -> bool:
    state = Gdk.ModifierType.CONTROL_MASK if ctrl else Gdk.ModifierType(0)
    return bool(page.cards[item.id]._on_key(Gtk.EventControllerKey(), keyval, 0, state))


def test_space_picks_ctrl_a_picks_the_page_and_ctrl_d_downloads(store: Store) -> None:
    _record_download(store.root, candidate("aa0002"))
    page = store.page()
    page.activate(None)
    _searched(page)
    spin_until(lambda: page.cards["wallhaven:aa0002"].in_library, 5)
    first = page.pages.current[0]
    assert _press(page, first, Gdk.KEY_space)
    assert page.select_mode and list(page.picked) == [first.id]
    assert _press(page, first, Gdk.KEY_space)
    assert not page.picked, "Space again unpicks"

    select_all = _shortcut(page, "<Control>a").get_action()
    assert select_all is not None and select_all.activate(
        Gtk.ShortcutActionFlags(0), page.widget, None
    )
    assert set(page.picked) == {"wallhaven:aa0000", "wallhaven:aa0001"}, "not what is held"

    download = _shortcut(page, "<Control>d").get_action()
    assert download is not None and download.activate(Gtk.ShortcutActionFlags(0), page.widget, None)
    spin_until(lambda: not store.store.downloads.busy and len(store.script.downloads) == 2, 5)
    assert {name for name, _variant in store.script.downloads} == {"aa0000", "aa0001"}


def test_enter_previews_and_ctrl_enter_applies_what_is_held(store: Store) -> None:
    _record_download(store.root, candidate("aa0000"))
    page = store.page()
    page.activate(None)
    _searched(page)
    held = page.pages.current[0]
    spin_until(lambda: page.cards[held.id].in_library, 5)
    assert _press(page, held, Gdk.KEY_Return)
    assert page.dialog is not None and page.dialog.item.id == held.id
    page.dialog.force_close()
    assert _press(page, held, Gdk.KEY_Return, ctrl=True)
    spin_until(lambda: bool(store.backend.refreshes), 5)  # not in the session yet: rescan first
    path = store.root / "Wall-in-One" / "Wallhaven" / "wallhaven-aa0000.png"
    store.land(path)
    finished = store.backend.refreshes[-1]
    assert finished is not None
    finished(True)
    assert store.backend.played == [(path, None)]


# -- the Workshop and the page's other words ------------------------------------------------


def test_the_workshop_hand_off_is_offered_only_with_steam(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.library import workshop

    page = store.page()
    page.activate(None)
    spin_until(lambda: store.store._facts_future is None, 5)
    assert not page.header_end()[0].get_visible()
    monkeypatch.setattr(workshop, "is_steam_installed", lambda *a, **k: True)
    store.store.refresh_facts()
    spin_until(lambda: store.store.has_workshop, 5)
    assert page.header_end()[0].get_visible()
    launched: list[str] = []

    class Launcher:
        def __init__(self, uri: str) -> None:
            launched.append(uri)

        def launch(self, *_arguments: object) -> None: ...

    monkeypatch.setattr(Gtk, "UriLauncher", Launcher)
    monkeypatch.setattr(workshop, "links", lambda *a, **k: ("steam://url/x", "https://example/y"))
    store.store.open_workshop()
    spin_until(lambda: bool(launched), 5)
    assert launched == ["steam://url/x"]
    assert store.toasts == ["Opening the Wallpaper Engine Workshop (https://example/y)"]


def test_a_workshop_link_that_couldnt_be_made_is_said(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: the click was dropped without a word."""
    from wall_in_one.library import workshop

    def failing(*_arguments: object, **_keywords: object) -> tuple[str, str]:
        raise RuntimeError("injected: no links")

    monkeypatch.setattr(workshop, "links", failing)
    store.store.open_workshop()
    spin_until(lambda: bool(store.toasts), 5)
    assert store.toasts == ["The Wallpaper Engine Workshop couldn't be opened: injected: no links"]


def test_a_facts_read_asked_for_during_one_that_failed_still_happens(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: the failure dropped the re-read asked for meanwhile, so the facts stayed old."""
    import threading

    from wall_in_one.library import workshop

    page = store.page()
    page.activate(None)
    spin_until(lambda: store.store._facts_future is None, 5)
    assert not store.store.has_workshop
    gate = threading.Event()
    calls: list[int] = []

    def steam(*_arguments: object, **_keywords: object) -> bool:
        calls.append(1)
        if len(calls) == 1:
            gate.wait(5)
            raise RuntimeError("injected: Steam's roots couldn't be read")
        return True

    monkeypatch.setattr(workshop, "is_steam_installed", steam)
    store.store.refresh_facts()  # the read that fails
    spin_until(lambda: bool(calls), 5)
    store.store.refresh_facts()  # asked for meanwhile
    gate.set()
    spin_until(lambda: store.store.has_workshop, 5)
    assert len(calls) == 2


def test_an_owned_read_asked_for_during_one_that_failed_still_happens(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: the failure dropped the re-read the library change asked for (no badges)."""
    import threading

    from wall_in_one import browse

    page = store.page()
    page.activate(None)
    spin_until(lambda: store.store._owned_future is None, 5)
    gate = threading.Event()
    calls: list[int] = []
    real = vars(browse.Browser)["owned"]

    def owned(browser: browse.Browser) -> object:
        calls.append(1)
        if len(calls) == 1:
            gate.wait(5)
            raise RuntimeError("injected: a library folder couldn't be walked")
        return real.fget(browser)

    monkeypatch.setattr(browse.Browser, "owned", property(owned))
    store.store._refresh_owned()  # the read that fails
    spin_until(lambda: bool(calls), 5)
    store.store._forget_owned()  # the library changed meanwhile: read it again
    gate.set()
    spin_until(lambda: len(calls) == 2 and store.store._owned_future is None, 5)


def test_a_view_is_the_providers_candidate(store: Store) -> None:
    view = CandidateView(candidate("ab1234"))
    assert view.id == "wallhaven:ab1234" and view.provider == WALLHAVEN
    assert view.detail == "3840 \u00d7 2160" and not view.is_moving
    moving = CandidateView(candidate("xyz", MOTIONBGS))
    assert moving.is_moving and moving.detail == "Video · 4K"


# -- AB-6: the page shows what is true before anything is said --------------------------------


def _toasts_raise(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every toast raises on its way through the application; what was being said."""
    raised: list[str] = []

    def durability_raises(message: str) -> str:
        raised.append(message)
        raise RuntimeError("injected: the toast failed")

    monkeypatch.setattr(store.backend, "with_task_durability", durability_raises)
    return raised


def test_a_later_page_that_failed_ends_its_spinner_before_it_is_said(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's repro: the toast came before the pager, and when it raised the
    spinner and "Loading the next page…" stayed, with nothing left to end them."""
    store.script.pages[(WALLHAVEN, "", 1)] = result(*ids(3), has_next=True)
    answer = store.script.answer

    def failing(name: str, query: SearchQuery) -> SearchResult:
        if query.page == 2:
            raise ProviderError("timeout", "the site did not answer")
        return answer(name, query)

    monkeypatch.setattr(store.script, "answer", failing)
    page = store.page()
    page.activate(None)
    _searched(page)
    assert page.next.get_sensitive() and not page._fetch_spinner.get_visible()
    raised = _toasts_raise(store, monkeypatch)

    page.show_next()  # the provider's next page: it fails
    spin_until(lambda: bool(raised), 5, what="the failed page to be said")

    assert not page.fetching
    assert not page._fetch_spinner.get_visible(), "the spinner has ended"
    assert page.next.get_tooltip_text() == "Next page"
    assert not page.next.get_sensitive(), "a Next that fails is not offered again"
    assert _shown(page) == ids(3), "every page reached is kept"


def test_a_refused_download_leaves_the_bar_counting_what_is_still_picked(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The download's refusal is a toast, and the bar was brought up to date after it."""
    page = store.page()
    page.activate(None)
    _searched(page)
    first, second = page.pages.current[:2]
    page.toggle_pick(first)
    page.toggle_pick(second)
    assert page._selection_label.get_label() == "2 selected"
    monkeypatch.setattr(store.store, "download_blocked", lambda: "Add a library folder first")
    raised = _toasts_raise(store, monkeypatch)

    with pytest.raises(RuntimeError, match="injected"):
        page.download(first)  # refused, and saying so raises

    assert raised == ["Add a library folder first"] and list(page.picked) == [second.id]
    # Before: still "2 selected", with one picked.
    assert page._selection_label.get_label() == "1 selected"


def test_automatic_quality_is_labelled_as_following_the_listing() -> None:
    """The blank choice follows the card's badge, so it must not promise the best file (R32-2)."""
    from wall_in_one.ui.next.store_state import QUALITIES

    assert QUALITIES[0] == ("", "Automatic (as listed)")
    assert all("best" not in label.lower() for _variant, label in QUALITIES)
