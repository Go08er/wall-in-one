"""Searching two wallpaper sites, and downloading into the library.

The providers do the talking; `wall_in_one.browse` decides where a download
lands. This is only the part that has to be a window: a provider picker, a
query, a grid of results, and one button per result. Every remote call happens
on a worker and comes back through `GLib.idle_add`, the same arrangement
`ui.thumbnails` uses, because a search takes as long as the site takes.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from wall_in_one import thumbnails
from wall_in_one.browse import Browser, Downloaded
from wall_in_one.library.model import Kind
from wall_in_one.providers import wallhaven
from wall_in_one.providers.base import (
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
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.candidate = candidate
        self._on_download = on_download
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
        self._browser = Browser()
        self._loader = PreviewLoader(self._browser)
        # Separate pools: a 40 MB video download must not hold up the next
        # search, and two searches at once would only fight over the cache.
        self._searches = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search")
        self._downloads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="download")
        self._infos = self._browser.available
        self._cards: list[_CandidateCard] = []
        self._result: SearchResult | None = None
        self._page = 1
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
        self._wallhaven_filters.append(_labelled("Sort by", self._sorting))

        self._categories = _CheckRow(
            "Categories", ("General", "Anime", "People"), (True, True, True)
        )
        self._wallhaven_filters.append(self._categories)

        key_missing = any(
            info.limitations for info in self._infos if info.name == wallhaven.Wallhaven.name
        )
        self._purity = _CheckRow("Rating", ("SFW", "Sketchy", "NSFW"), (True, False, False))
        # NSFW results need a key; offering the toggle without one produces an
        # empty grid and no explanation.
        self._purity.set_enabled(2, not key_missing)
        if key_missing:
            self._purity.set_hint(2, "needs a Wallhaven API key")
        self._wallhaven_filters.append(self._purity)

        self._atleast = Gtk.Entry(placeholder_text="At least, e.g. 1920x1080")
        self._wallhaven_filters.append(_labelled("Minimum size", self._atleast))
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
        return popover

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

    def _query(self, page: int) -> SearchQuery:
        text = self._entry.get_text().strip()
        options: dict[str, str] = {}
        if self.provider_name == wallhaven.Wallhaven.name:
            options["sorting"] = SORTINGS[self._sorting.get_selected()][0]
            options["categories"] = self._categories.bits()
            options["purity"] = self._purity.bits()
            atleast = self._atleast.get_text().strip()
            if atleast:
                options["atleast"] = atleast
        else:
            mode = MODES[self._mode.get_selected()][0]
            # Typing a query means searching, whatever the browse mode says;
            # MotionBGS rejects the two together.
            options["mode"] = "search" if text else mode
            if options["mode"] == "genre":
                options["genre"] = self._genre.get_text().strip()
        return SearchQuery(text=text, page=page, options=options)

    # -- searching ---------------------------------------------------------

    def start_search(self, *, page: int) -> None:
        if self._searching or page < 1:
            return
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

        for candidate in result.items:
            card = _CandidateCard(candidate, self._on_download)
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

    def _on_download(self, candidate: WallpaperCandidate) -> None:
        card = self._card_for(candidate)
        if card is not None:
            card.set_busy(True)

        def work() -> Downloaded:
            return self._browser.download(candidate)

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
            if done is None:
                if card is not None:
                    card.set_busy(False)
                self.report(message)
            else:
                if card is not None:
                    card.mark_downloaded()
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
