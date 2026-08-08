"""Searching two wallpaper sites, and downloading into the library.

The providers do the talking; `wall_in_one.browse` decides where a download
lands. This is only the part that has to be a window: a provider picker, a
query, a grid of results, and one button per result. Every remote call happens
on a worker and comes back through `GLib.idle_add`, the same arrangement
`ui.thumbnails` uses, because a search takes as long as the site takes.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from wall_in_one import browse, thumbnails
from wall_in_one.browse import Browser, Downloaded
from wall_in_one.library.model import Kind
from wall_in_one.providers import registry, wallhaven
from wall_in_one.providers.base import (
    CandidateDetail,
    ProviderError,
    SearchQuery,
    SearchResult,
    WallpaperCandidate,
)

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application

#: Previews are small and independent, so a handful of workers saturates the
#: connection without turning a page of results into a thundering herd.
MAX_WORKERS: Final = 4

#: Card geometry, 16:9 like the library grid so the two views agree.
CARD_WIDTH: Final = 300
CARD_HEIGHT: Final = 169

#: Wallhaven's sorts, in the order the site itself lists them.
SORTINGS: Final[tuple[tuple[str, str], ...]] = (
    ("date_added", "Newest"),
    ("relevance", "Relevance"),
    ("random", "Random"),
    ("views", "Views"),
    ("favorites", "Favourites"),
    ("toplist", "Top list"),
    ("hot", "Hot"),
)

ORDERS: Final[tuple[tuple[str, str], ...]] = (
    ("desc", "Descending"),
    ("asc", "Ascending"),
)

#: Wallhaven's toplist windows, shortest first. Spelled out rather than left as
#: `1d`/`3M`, which are the API's names and not anybody's reading of them.
TOP_RANGES: Final[tuple[tuple[str, str], ...]] = (
    ("1d", "Last day"),
    ("3d", "Last 3 days"),
    ("1w", "Last week"),
    ("1M", "Last month"),
    ("3M", "Last 3 months"),
    ("6M", "Last 6 months"),
    ("1y", "Last year"),
)

#: MotionBGS browse modes. `search` is implied by typing a query, so it is not
#: offered as a mode: picking it with an empty box is an error the user would
#: have to decode.
MODES: Final[tuple[tuple[str, str], ...]] = (
    ("latest", "Latest"),
    ("4k", "4K"),
    ("hd", "HD"),
    ("genre", "Genre"),
)


# -- off-thread previews -------------------------------------------------


PreviewCallback = Callable[[WallpaperCandidate, bytes], None]


class PreviewLoader:
    """Fetches candidate thumbnails off-thread and decodes them for GTK."""

    def __init__(self, browser: Browser, max_workers: int = MAX_WORKERS) -> None:
        self._browser = browser
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="preview")
        self._cache: dict[str, bytes] = {}
        self._pending: set[str] = set()
        self._closed = False

    def request(self, candidate: WallpaperCandidate, callback: PreviewCallback) -> None:
        """Ask for ``candidate``'s preview. ``callback`` runs on the main thread."""
        if self._closed:
            return
        url = candidate.thumbnail_url
        if not url:
            return
        cached = self._cache.get(url)
        if cached is not None:
            # Paging back to a page already seen should not re-download it.
            callback(candidate, cached)
            return
        if url in self._pending:
            return
        self._pending.add(url)
        future = self._pool.submit(self._fetch, candidate)
        future.add_done_callback(lambda done: self._finish(candidate, done, callback))

    def _fetch(self, candidate: WallpaperCandidate) -> bytes:
        try:
            data = self._browser.thumbnail(candidate)
        except ProviderError:
            # A preview that will not load is a card without a picture. The
            # download button still works.
            return b""
        # Decoding here rather than on the main thread: MotionBGS serves webp,
        # which means an ffmpeg call this closure's GdkPixbuf cannot avoid.
        return thumbnails.to_displayable(data)

    def _finish(
        self, candidate: WallpaperCandidate, future: Future[bytes], callback: PreviewCallback
    ) -> None:
        self._pending.discard(candidate.thumbnail_url)
        if self._closed:
            return
        try:
            data = future.result()
        except Exception:
            # Broad on purpose: a worker must never take the app down.
            data = b""
        self._cache[candidate.thumbnail_url] = data

        def deliver() -> bool:
            if not self._closed:
                callback(candidate, data)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def shutdown(self) -> None:
        self._closed = True
        self._pending.clear()
        self._cache.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)


# -- one result ----------------------------------------------------------


class _CandidateCard(Gtk.Box):
    """One search result: a picture, what it is, and a download button."""

    def __init__(
        self,
        candidate: WallpaperCandidate,
        on_download: Callable[[WallpaperCandidate], None],
        on_open: Callable[[WallpaperCandidate], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.candidate = candidate
        self._on_download = on_download
        self._on_open = on_open
        self.add_css_class("wio-tile")
        # Fixed width, not merely a minimum: a FlowBox hands children their
        # natural width, and a card that grows to fill the dialog stretches its
        # 16:9 frame into a letterbox.
        self.set_size_request(CARD_WIDTH, -1)
        self.set_hexpand(False)
        self.set_halign(Gtk.Align.CENTER)

        self._frame = Gtk.Frame()
        self._frame.set_size_request(CARD_WIDTH, CARD_HEIGHT)
        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.COVER)
        self._picture.set_can_shrink(True)
        self._frame.set_child(self._picture)
        self.append(self._frame)

        if on_open is not None:
            # A gesture rather than wrapping the frame in a button: a button
            # brings its own padding and focus ring, which would put a border
            # inside the 16:9 frame and letterbox every preview by a few pixels.
            click = Gtk.GestureClick()
            click.connect("released", self._on_picture_clicked)
            self._frame.add_controller(click)
            self._frame.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
            self._frame.set_tooltip_text("Show details")

        caption = Gtk.Label(label=candidate.title or candidate.identifier, xalign=0.0)
        caption.set_ellipsize(Pango.EllipsizeMode.END)
        caption.set_max_width_chars(28)
        caption.add_css_class("heading")
        self.append(caption)

        details = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        subtitle = ", ".join(
            part
            for part in (
                candidate.resolution,
                "video" if candidate.kind is Kind.VIDEO else "still",
            )
            if part
        )
        label = Gtk.Label(label=subtitle, xalign=0.0)
        label.add_css_class("dim-label")
        label.add_css_class("caption")
        details.append(label)
        details.append(Gtk.Box(hexpand=True))

        self._button = Gtk.Button(icon_name="folder-download-symbolic", tooltip_text="Download")
        self._button.add_css_class("flat")
        self._button.connect("clicked", self._on_clicked)
        details.append(self._button)
        self.append(details)

    def _on_clicked(self, _button: Gtk.Button) -> None:
        self._on_download(self.candidate)

    def _on_picture_clicked(
        self, _gesture: Gtk.GestureClick, count: int, _x: float, _y: float
    ) -> None:
        # Single click only. A double click would otherwise open the dialog
        # twice, once per release.
        if count == 1 and self._on_open is not None:
            self._on_open(self.candidate)

    def set_preview(self, data: bytes) -> None:
        if not data:
            return
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except GLib.Error:
            # Already transcoded once by the loader; if GTK still refuses it,
            # the card keeps its blank frame.
            return
        self._picture.set_paintable(texture)

    def set_busy(self, busy: bool) -> None:
        self._button.set_sensitive(not busy)
        self._button.set_icon_name(
            "content-loading-symbolic" if busy else "folder-download-symbolic"
        )

    def mark_downloaded(self) -> None:
        self._button.set_sensitive(False)
        self._button.set_icon_name("object-select-symbolic")
        self._button.set_tooltip_text("In your library")


# -- the dialog ----------------------------------------------------------


class BrowseDialog(Adw.Dialog):
    """Search a provider and pull wallpapers into the library."""

    def __init__(self, application: Application) -> None:
        super().__init__()
        self._app = application
        # Downloads land in the first configured root, because that is the one
        # the user put first. With none configured the Browser asks
        # `library.scan`, which is the directory being read from anyway.
        configured = application.settings.roots
        # `library_roots` is every configured root rather than just the first:
        # downloads land in one place, but a wallpaper already in *any* of them
        # is one the user has, and offering it again is the thing being fixed.
        self._browser = Browser(
            root=configured[0] if configured else None,
            library_roots=configured,
        )
        self._loader = PreviewLoader(self._browser)
        # Separate pools: a 40 MB video download must not hold up the next
        # search, and two searches at once would only fight over the cache.
        self._searches = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search")
        self._downloads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="download")
        self._infos = self._browser.available
        self._cards: list[_CandidateCard] = []
        self._result: SearchResult | None = None
        self._page = 1
        #: Held across the pages of one random search so it does not re-roll
        #: between them, and cleared whenever a new search starts.
        self._seed = ""
        #: Open detail views by identifier, so a download started inside one
        #: can tell it what happened.
        self._detail_dialogs: dict[str, DetailDialog] = {}
        self._searching = False
        self._closed = False

        self.set_title("Browse")
        self.set_content_width(1000)
        self.set_content_height(760)
        self.connect("closed", self._on_closed)

        self.set_child(self._build_content())
        # Wallhaven first: it is the one that answers an arbitrary query, and
        # MotionBGS only has videos, which not every setup can play.
        for index, info in enumerate(self._infos):
            if info.name == wallhaven.Wallhaven.name:
                self._providers.set_selected(index)
                break
        self._sync_provider_controls()

    # -- construction ----------------------------------------------------

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Browse", subtitle="Wallhaven, MotionBGS"))
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self._build_search_bar())

        self._flow = Gtk.FlowBox()
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_homogeneous(True)
        self._flow.set_min_children_per_line(2)
        self._flow.set_max_children_per_line(4)
        self._flow.set_valign(Gtk.Align.START)
        self._flow.set_row_spacing(12)
        self._flow.set_column_spacing(12)
        self._flow.set_margin_top(12)
        self._flow.set_margin_bottom(12)
        self._flow.set_margin_start(12)
        self._flow.set_margin_end(12)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(self._flow)

        self._status = Adw.StatusPage(
            title="Nothing searched yet",
            description="Pick a provider and press Search.",
            icon_name="system-search-symbolic",
        )
        self._spinner = Adw.StatusPage(title="Searching...", icon_name="system-search-symbolic")

        self._stack = Gtk.Stack()
        self._stack.add_named(self._status, "empty")
        self._stack.add_named(self._spinner, "busy")
        self._stack.add_named(scroller, "results")
        self._stack.set_visible_child_name("empty")

        self._toast = Adw.ToastOverlay()
        self._toast.set_child(self._stack)
        toolbar.set_content(self._toast)
        toolbar.add_bottom_bar(self._build_pager())
        return toolbar

    def _build_search_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        bar.set_margin_start(12)
        bar.set_margin_end(12)

        self._providers = Gtk.DropDown.new_from_strings([info.title for info in self._infos])
        self._providers.set_tooltip_text("Which site to search")
        self._providers.connect("notify::selected", self._on_provider_changed)
        bar.append(self._providers)

        self._entry = Gtk.SearchEntry(hexpand=True)
        self._entry.set_placeholder_text("Search")
        self._entry.connect("activate", lambda _entry: self.start_search(page=1))
        bar.append(self._entry)

        self._filters = Gtk.MenuButton(icon_name="view-filter-symbolic", tooltip_text="Filters")
        self._filters.set_popover(self._build_filters())
        bar.append(self._filters)

        search = Gtk.Button(label="Search")
        search.add_css_class("suggested-action")
        search.connect("clicked", lambda _button: self.start_search(page=1))
        bar.append(search)
        return bar

    def _build_filters(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # -- Wallhaven -----------------------------------------------------
        self._wallhaven_filters = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._sorting = Gtk.DropDown.new_from_strings([label for _value, label in SORTINGS])
        self._sorting.connect("notify::selected", self._on_sorting_changed)
        self._wallhaven_filters.append(_labelled("Sort by", self._sorting))

        self._order = Gtk.DropDown.new_from_strings([label for _value, label in ORDERS])
        self._wallhaven_filters.append(_labelled("Order", self._order))

        # Only means anything for the top list, so it is shown only then rather
        # than sitting there inert next to six sortings that ignore it.
        self._top_range = Gtk.DropDown.new_from_strings([label for _value, label in TOP_RANGES])
        self._top_range.set_selected(_index_of(TOP_RANGES, "1M"))
        self._top_range_row = _labelled("Top list covers", self._top_range)
        self._wallhaven_filters.append(self._top_range_row)

        self._categories = _CheckRow(
            "Categories", ("General", "Anime", "People"), (True, True, True)
        )
        self._wallhaven_filters.append(self._categories)

        self._purity = _CheckRow("Rating", ("SFW", "Sketchy", "NSFW"), (True, False, False))
        self._wallhaven_filters.append(self._purity)

        self._atleast = Gtk.Entry(placeholder_text="At least, e.g. 1920x1080")
        self._wallhaven_filters.append(_labelled("Minimum size", self._atleast))

        self._ratios = Gtk.Entry(placeholder_text="e.g. 16x9, or 16x9,21x9")
        self._wallhaven_filters.append(_labelled("Aspect ratio", self._ratios))

        self._colours = _ColourPicker()
        self._wallhaven_filters.append(self._colours)
        box.append(self._wallhaven_filters)

        # -- MotionBGS -----------------------------------------------------
        self._motionbgs_filters = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._mode = Gtk.DropDown.new_from_strings([label for _value, label in MODES])
        self._mode.connect("notify::selected", self._on_mode_changed)
        self._motionbgs_filters.append(_labelled("Browse", self._mode))
        self._genre = Gtk.Entry(placeholder_text="Genre, e.g. anime")
        self._genre_row = _labelled("Genre", self._genre)
        self._motionbgs_filters.append(self._genre_row)
        box.append(self._motionbgs_filters)

        popover = Gtk.Popover()
        popover.set_child(box)
        # Re-asked every time the popover opens rather than once at
        # construction: the settings dialogue can store a key while this dialog
        # is alive, and the window keeps one browse dialog for the session, so
        # a value read at construction would stay stale until it is closed.
        popover.connect("notify::visible", self._on_filters_shown)
        self._sync_nsfw_toggle()
        self._sync_top_range()
        return popover

    def _on_filters_shown(self, popover: Gtk.Popover, _parameter: object) -> None:
        if popover.get_visible():
            self._sync_nsfw_toggle()

    def _on_sorting_changed(self, _dropdown: Gtk.DropDown, _parameter: object) -> None:
        """Show the toplist window only when it applies, and re-roll.

        Dropping the seed matters in both directions: leaving random sorting
        would carry a value Wallhaven refuses outright, and returning to it
        would repeat the previous random search rather than draw a new one.
        """
        self._seed = ""
        self._sync_top_range()

    def _sync_top_range(self) -> None:
        sorting = SORTINGS[self._sorting.get_selected()][0]
        self._top_range_row.set_visible(sorting in browse.RANGED_SORTINGS)

    def _sync_nsfw_toggle(self) -> None:
        """Offer the NSFW rating only when Wallhaven can actually return it.

        Matched against the specific limitation rather than "has any
        limitation", so a future limitation about something else does not
        silently disable an unrelated control.
        """
        limitations = next(
            (
                info.limitations
                for info in registry.describe()
                if info.name == wallhaven.Wallhaven.name
            ),
            (),
        )
        # Anything wrong with the key blocks NSFW, but only the plain
        # missing-key case has a short hint; a malformed key gets its own
        # sentence from the registry.
        blocked = bool(limitations)
        self._purity.set_enabled(2, not blocked)
        if blocked:
            self._purity.set_hint(2, limitations[0])

    def _build_pager(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        bar.set_margin_start(12)
        bar.set_margin_end(12)

        self._previous = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Previous page")
        self._previous.connect("clicked", lambda _button: self.start_search(page=self._page - 1))
        self._previous.set_sensitive(False)
        bar.append(self._previous)

        self._summary = Gtk.Label(label="", xalign=0.0, hexpand=True)
        self._summary.add_css_class("dim-label")
        bar.append(self._summary)

        self._next = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Next page")
        self._next.connect("clicked", lambda _button: self.start_search(page=self._page + 1))
        self._next.set_sensitive(False)
        bar.append(self._next)
        return bar

    # -- provider selection ------------------------------------------------

    @property
    def provider_name(self) -> str:
        index = self._providers.get_selected()
        if index >= len(self._infos):
            return self._infos[0].name
        return self._infos[index].name

    def _on_provider_changed(self, *_arguments: object) -> None:
        self._sync_provider_controls()

    def _on_mode_changed(self, *_arguments: object) -> None:
        self._genre_row.set_visible(MODES[self._mode.get_selected()][0] == "genre")

    def _sync_provider_controls(self) -> None:
        """Show only the filters the selected provider actually understands."""
        is_wallhaven = self.provider_name == wallhaven.Wallhaven.name
        self._wallhaven_filters.set_visible(is_wallhaven)
        self._motionbgs_filters.set_visible(not is_wallhaven)
        if not is_wallhaven:
            self._on_mode_changed()
        self._entry.set_placeholder_text(
            "Search Wallhaven" if is_wallhaven else "Search MotionBGS (first page only)"
        )

    def _read_filters(self) -> browse.Filters:
        """Read every control into one toolkit-free value.

        The awkward rules -- which options are legal with which sorting, how a
        seed survives paging -- live in `browse.Filters` where they can be
        tested. This is only the reading.
        """
        return browse.Filters(
            text=self._entry.get_text().strip(),
            sorting=SORTINGS[self._sorting.get_selected()][0],
            order=ORDERS[self._order.get_selected()][0],
            categories=self._categories.bits(),
            purity=self._purity.bits(),
            atleast=self._atleast.get_text().strip(),
            ratios=self._ratios.get_text().strip(),
            colour=self._colours.colour(),
            top_range=TOP_RANGES[self._top_range.get_selected()][0],
            seed=self._seed,
            mode=MODES[self._mode.get_selected()][0],
            genre=self._genre.get_text().strip(),
        )

    def _query(self, page: int) -> SearchQuery:
        filters = self._read_filters()
        if self.provider_name == wallhaven.Wallhaven.name:
            # Held across pages so a random search does not re-roll underneath
            # the user, and dropped by `seeded` when the sorting stops wanting
            # one -- Wallhaven refuses a stale seed rather than ignoring it.
            filters = filters.seeded()
            self._seed = filters.seed
            return filters.to_query(browse.WALLHAVEN, page)

        if filters.motionbgs_options().get("mode") == "genre" and not filters.genre:
            # MotionBGS would answer "genre is not a lowercase MotionBGS slug",
            # which is true and tells the user nothing about the empty box in
            # front of them.
            raise ProviderError("validation", "type a genre, or pick another way to browse")
        return filters.to_query(browse.MOTIONBGS, page)

    # -- searching ---------------------------------------------------------

    def start_search(self, *, page: int) -> None:
        if self._searching or page < 1:
            return
        if page == 1:
            # A fresh search re-rolls; paging within one does not. Without
            # this, asking for random wallpapers twice would return the same
            # ones, because the seed that keeps page two honest would also
            # pin page one.
            self._seed = ""
        try:
            query = self._query(page)
        except ProviderError as error:
            self.report(str(error))
            return
        name = self.provider_name
        self._searching = True
        self._stack.set_visible_child_name("busy")
        self._previous.set_sensitive(False)
        self._next.set_sensitive(False)

        def work() -> SearchResult:
            return self._browser.search(name, query)

        future = self._searches.submit(work)
        future.add_done_callback(lambda done: self._deliver(done, page))

    def _deliver(self, future: Future[SearchResult], page: int) -> None:
        try:
            result: SearchResult | None = future.result()
            message = ""
        except ProviderError as error:
            result, message = None, str(error)
        except Exception as error:
            # Broad on purpose: a search must not be able to kill the app.
            result, message = None, f"search failed: {error}"

        def deliver() -> bool:
            if not self._closed:
                self._searching = False
                if result is None:
                    self._show_failure(message)
                else:
                    self._show_result(result, page)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def _show_failure(self, message: str) -> None:
        self._stack.set_visible_child_name("empty")
        self._status.set_title("That search did not work")
        self._status.set_description(message)
        self.report(message)

    def _show_result(self, result: SearchResult, page: int) -> None:
        self._result = result
        self._page = page
        self._clear()

        # Asked once per page rather than once per card, and safe to touch from
        # the main loop because `Browser.search` warmed it on the worker that
        # produced these results.
        held = self._browser.owned
        for candidate in result.items:
            card = _CandidateCard(candidate, self._on_download, self._open_detail)
            if held.holds(candidate):
                card.mark_downloaded()
            self._cards.append(card)
            self._flow.append(card)
            self._loader.request(candidate, self._on_preview)

        if not result.items:
            self._stack.set_visible_child_name("empty")
            self._status.set_title("No results")
            self._status.set_description("Nothing came back for that query.")
        else:
            self._stack.set_visible_child_name("results")

        self._previous.set_sensitive(result.has_previous)
        self._next.set_sensitive(result.has_next)
        self._summary.set_label(self._describe(result))

    def _describe(self, result: SearchResult) -> str:
        parts = [f"{len(result)} result{'' if len(result) == 1 else 's'}"]
        if result.total_hint:
            parts.append(f"of about {result.total_hint}")
        parts.append(f"page {result.page}")
        if result.dropped:
            # A sudden jump here means the remote's markup moved, so it is
            # reported rather than quietly swallowed.
            parts.append(f"{result.dropped} unreadable")
        if result.cached:
            parts.append("cached")
        return " - ".join(parts)

    def _clear(self) -> None:
        for card in self._cards:
            self._flow.remove(card)
        self._cards.clear()

    def _on_preview(self, candidate: WallpaperCandidate, data: bytes) -> None:
        for card in self._cards:
            if card.candidate.identifier == candidate.identifier:
                card.set_preview(data)

    # -- downloading -------------------------------------------------------

    def _open_detail(self, candidate: WallpaperCandidate) -> None:
        """Show everything the provider knows about one result.

        The dialog is remembered so that a download started from inside it can
        report back: it owns its own button, and a card behind it is not
        necessarily the thing the user is looking at.
        """
        detail = DetailDialog(
            self._browser,
            candidate,
            self._on_download,
            held=self._browser.owned.holds(candidate),
        )
        self._detail_dialogs[candidate.identifier] = detail
        detail.connect(
            "closed", lambda _dialog: self._detail_dialogs.pop(candidate.identifier, None)
        )
        detail.present(self)

    def _on_download(self, candidate: WallpaperCandidate, variant: str = "") -> None:
        card = self._card_for(candidate)
        if card is not None:
            card.set_busy(True)

        def work() -> Downloaded:
            return self._browser.download(candidate, variant=variant)

        future = self._downloads.submit(work)
        future.add_done_callback(lambda done: self._downloaded(candidate, done))

    def _card_for(self, candidate: WallpaperCandidate) -> _CandidateCard | None:
        for card in self._cards:
            if card.candidate.identifier == candidate.identifier:
                return card
        return None

    def _downloaded(self, candidate: WallpaperCandidate, future: Future[Downloaded]) -> None:
        try:
            done: Downloaded | None = future.result()
            message = ""
        except ProviderError as error:
            done, message = None, str(error)
        except Exception as error:
            # Broad on purpose: see `_deliver`.
            done, message = None, f"download failed: {error}"

        def deliver() -> bool:
            if self._closed:
                return GLib.SOURCE_REMOVE
            card = self._card_for(candidate)
            detail = self._detail_dialogs.get(candidate.identifier)
            if done is None:
                if card is not None:
                    card.set_busy(False)
                if detail is not None:
                    detail.failed()
                self.report(message)
            else:
                if card is not None:
                    card.mark_downloaded()
                if detail is not None:
                    detail.downloaded()
                self.report(done.describe())
                # The file is in the library directory but not in the library
                # until something looks again.
                self._app.refresh_library()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    # -- housekeeping ------------------------------------------------------

    def report(self, message: str) -> None:
        self._toast.add_toast(Adw.Toast.new(message))

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        self._loader.shutdown()
        self._searches.shutdown(wait=False, cancel_futures=True)
        # Not cancelling in flight downloads: the provider writes to a temp
        # name and renames, so an interrupted one leaves nothing behind, and a
        # finished one is already in the library.
        self._downloads.shutdown(wait=False, cancel_futures=True)


# -- small widgets -------------------------------------------------------


def _index_of(choices: tuple[tuple[str, str], ...], value: str) -> int:
    """Where ``value`` sits in a dropdown's list, or the first entry.

    So a default is named by what it is rather than by the position it happens
    to occupy, and reordering the list cannot silently change it.
    """
    return next((at for at, (name, _label) in enumerate(choices) if name == value), 0)


def _labelled(text: str, widget: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    label = Gtk.Label(label=text, xalign=0.0, hexpand=True)
    box.append(label)
    box.append(widget)
    return box


class _CheckRow(Gtk.Box):
    """A labelled row of check buttons that reads out as Wallhaven's bit string."""

    def __init__(self, title: str, labels: tuple[str, ...], initial: tuple[bool, ...]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(label=title, xalign=0.0)
        heading.add_css_class("dim-label")
        heading.add_css_class("caption")
        self.append(heading)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._checks: list[Gtk.CheckButton] = []
        for label, active in zip(labels, initial, strict=True):
            check = Gtk.CheckButton(label=label, active=active)
            self._checks.append(check)
            row.append(check)
        self.append(row)

    def bits(self) -> str:
        """Wallhaven spells these as three bits; all-off means all-on there."""
        bits = "".join("1" if check.get_active() else "0" for check in self._checks)
        return bits if "1" in bits else "1" + "0" * (len(self._checks) - 1)

    def set_enabled(self, index: int, enabled: bool) -> None:
        self._checks[index].set_sensitive(enabled)
        if not enabled:
            self._checks[index].set_active(False)

    def set_hint(self, index: int, hint: str) -> None:
        self._checks[index].set_tooltip_text(hint)


#: Swatch edge in pixels. Large enough to judge a colour against the wallpaper
#: it will find, small enough that all 29 fit without the popover scrolling.
SWATCH: Final = 26


def _swatch_texture(colour: str, size: int = SWATCH) -> Gdk.Texture:
    """A solid square of ``colour``, as six hex digits without the hash.

    Built rather than drawn: a `Gtk.DrawingArea` per swatch would mean 29 draw
    callbacks for something that never changes, and styling 29 buttons through
    CSS would mean generating and parsing a stylesheet to say "this one is
    blue".
    """
    red, green, blue = (int(colour[at : at + 2], 16) for at in (0, 2, 4))
    pixels = bytes((red, green, blue)) * (size * size)
    return Gdk.MemoryTexture.new(
        size, size, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(pixels), size * 3
    )


class _ColourPicker(Gtk.Box):
    """Wallhaven's palette, one colour at a time.

    The filter this app has the most use for, and the one it was missing.
    Searching by colour is how somebody finds a wallpaper that will generate
    the scheme they want, which is the whole premise of pairing a wallpaper
    with a palette -- and Wallhaven has indexed it all along.

    Single-select, because the API takes one colour. Clicking the selected
    swatch clears it, so "no colour" is reachable without a separate button
    that would be dead most of the time.
    """

    def __init__(self, colours: Sequence[str] = wallhaven.COLOR_ORDER) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(label="Colour", xalign=0.0)
        heading.add_css_class("dim-label")
        heading.add_css_class("caption")
        self.append(heading)

        self._selected = ""
        self._buttons: dict[str, Gtk.ToggleButton] = {}
        grid = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=10,
            min_children_per_line=10,
            homogeneous=True,
            column_spacing=2,
            row_spacing=2,
        )
        for colour in colours:
            button = Gtk.ToggleButton(tooltip_text=f"#{colour}")
            button.add_css_class("flat")
            picture = Gtk.Picture.new_for_paintable(_swatch_texture(colour))
            picture.set_size_request(SWATCH, SWATCH)
            button.set_child(picture)
            button.connect("toggled", self._on_toggled, colour)
            self._buttons[colour] = button
            grid.append(button)
        self.append(grid)

    def _on_toggled(self, button: Gtk.ToggleButton, colour: str) -> None:
        if not button.get_active():
            # Untoggling the selected swatch is how "any colour" is said.
            if self._selected == colour:
                self._selected = ""
            return
        self._selected = colour
        for other, widget in self._buttons.items():
            if other != colour and widget.get_active():
                # Recurses once into this handler with `active` false, which
                # falls out at the guard above without clearing the selection.
                widget.set_active(False)

    def colour(self) -> str:
        return self._selected


# -- the detail view -----------------------------------------------------


#: How wide the detail preview is allowed to get. Wider than a card by enough
#: to actually judge a wallpaper, narrow enough to sit on a laptop screen.
DETAIL_WIDTH: Final = 720
DETAIL_HEIGHT: Final = 405


class DetailDialog(Adw.Dialog):
    """Everything one provider knows about one wallpaper.

    The endpoint behind this has been implemented in both providers since they
    were written and had no caller: a search response is deliberately thin, and
    tags, colours, an exact file size and MotionBGS's two download qualities
    only come from asking about a single wallpaper.

    Two requests, both on a worker: the detail, then the larger preview it
    names. They are sequential because the second needs a URL from the first,
    and the picture is the slower of the two, so the facts appear while it is
    still arriving rather than after.
    """

    def __init__(
        self,
        browser: Browser,
        candidate: WallpaperCandidate,
        on_download: Callable[[WallpaperCandidate, str], None],
        *,
        held: bool = False,
    ) -> None:
        super().__init__()
        self._browser = browser
        self._candidate = candidate
        self._on_download = on_download
        self._held = held
        self._closed = False
        self._detail: CandidateDetail | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detail")
        self.connect("closed", self._on_closed)

        self.set_title(candidate.title or candidate.identifier)
        self.set_content_width(DETAIL_WIDTH + 48)

        outer = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(
                title=candidate.title or candidate.identifier,
                subtitle=candidate.provider,
            )
        )
        outer.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)

        frame = Gtk.Frame()
        frame.set_size_request(DETAIL_WIDTH, DETAIL_HEIGHT)
        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_can_shrink(True)
        frame.set_child(self._picture)
        body.append(frame)

        self._facts = Gtk.Grid(column_spacing=18, row_spacing=4)
        body.append(self._facts)

        self._tags = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, column_spacing=4, row_spacing=4
        )
        self._tags.set_visible(False)
        body.append(self._tags)

        self._colours = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._colours.set_visible(False)
        body.append(self._colours)

        self._status = Gtk.Label(label="Loading…", xalign=0.0)
        self._status.add_css_class("dim-label")
        body.append(self._status)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(body)
        outer.set_content(scroller)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_margin_top(6)
        actions.set_margin_bottom(6)
        actions.set_margin_start(12)
        actions.set_margin_end(12)

        page = Gtk.LinkButton(uri=candidate.page_url, label="Open page")
        page.set_visible(bool(candidate.page_url))
        actions.append(page)
        actions.append(Gtk.Box(hexpand=True))

        # Hidden until the detail says there is a choice. MotionBGS offers HD
        # and 4K; Wallhaven publishes one file, and a dropdown with one entry
        # is a control that asks a question with no answer.
        self._variants = Gtk.DropDown.new_from_strings([])
        self._variants.set_visible(False)
        actions.append(self._variants)

        self._download = Gtk.Button(label="Download")
        self._download.add_css_class("suggested-action")
        self._download.connect("clicked", self._on_download_clicked)
        actions.append(self._download)
        outer.add_bottom_bar(actions)

        self.set_child(outer)
        if held:
            self._mark_held()
        self._load()

    def _mark_held(self) -> None:
        self._download.set_sensitive(False)
        self._download.set_label("In your library")

    # -- loading ---------------------------------------------------------

    def _load(self) -> None:
        candidate = self._candidate

        def work() -> tuple[CandidateDetail, bytes]:
            detail = self._browser.describe(candidate)
            picture = self._browser.preview(detail.preview_url)
            return detail, thumbnails.to_displayable(picture) if picture else b""

        future = self._pool.submit(work)
        future.add_done_callback(self._deliver)

    def _deliver(self, future: Future[tuple[CandidateDetail, bytes]]) -> None:
        try:
            detail, picture = future.result()
        except ProviderError as error:
            message = str(error)
            detail, picture = None, b""
        except Exception:
            # Broad on purpose: a worker must never take the app down.
            message = "could not load the details"
            detail, picture = None, b""
        else:
            message = ""

        def show() -> bool:
            if not self._closed:
                self._show(detail, picture, message)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(show)

    def _show(self, detail: CandidateDetail | None, picture: bytes, message: str) -> None:
        if detail is None:
            # The download button stays live: failing to describe a wallpaper
            # is no reason to refuse to fetch it, and the candidate from the
            # search is enough to download by.
            self._status.set_label(message or "could not load the details")
            return
        self._detail = detail
        self._status.set_visible(False)

        for row, fact in enumerate(detail.facts):
            label = Gtk.Label(label=fact.label, xalign=0.0)
            label.add_css_class("dim-label")
            value = Gtk.Label(label=fact.value, xalign=0.0, selectable=True)
            value.set_ellipsize(Pango.EllipsizeMode.END)
            value.set_max_width_chars(60)
            self._facts.attach(label, 0, row, 1, 1)
            self._facts.attach(value, 1, row, 1, 1)

        for tag in detail.tags:
            chip = Gtk.Label(label=tag)
            chip.add_css_class("caption")
            chip.add_css_class("dim-label")
            self._tags.append(chip)
        self._tags.set_visible(bool(detail.tags))

        for colour in detail.colours:
            swatch = Gtk.Picture.new_for_paintable(_swatch_texture(colour, SWATCH))
            swatch.set_size_request(SWATCH, SWATCH)
            swatch.set_tooltip_text(f"#{colour}")
            self._colours.append(swatch)
        self._colours.set_visible(bool(detail.colours))

        if len(detail.variants) > 1:
            self._variants.set_model(Gtk.StringList.new([v.upper() for v in detail.variants]))
            self._variants.set_visible(True)

        if picture:
            # A preview GTK will not decode leaves the frame blank, which is
            # what the card does too. The facts above it are the point.
            with contextlib.suppress(GLib.Error):
                self._picture.set_paintable(Gdk.Texture.new_from_bytes(GLib.Bytes.new(picture)))

    # -- acting ----------------------------------------------------------

    def _on_download_clicked(self, _button: Gtk.Button) -> None:
        variant = ""
        detail = self._detail
        if detail is not None and len(detail.variants) > 1:
            variant = detail.variants[self._variants.get_selected()]
        self._download.set_sensitive(False)
        self._download.set_label("Downloading…")
        self._on_download(self._candidate, variant)

    def downloaded(self) -> None:
        """Called back when the download this dialog started has landed."""
        self._held = True
        self._mark_held()

    def failed(self) -> None:
        self._download.set_sensitive(True)
        self._download.set_label("Download")

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
