"""Searching two wallpaper sites, and downloading into the library.

The providers do the talking; `wall_in_one.browse` decides where a download
lands. This is only the part that has to be a window: a provider picker, a
query, a grid of results, and one button per result. Every remote call happens
on a worker and comes back through `GLib.idle_add`, the same arrangement
`ui.thumbnails` uses, because a search takes as long as the site takes.
"""

from __future__ import annotations

import contextlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from wall_in_one import browse, thumbnails, worker_processes
from wall_in_one.browse import Browser, Downloaded
from wall_in_one.library import owned, workshop
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

#: Enough decoded previews to make a short scroll back instant without keeping
#: every image seen during a long browse alive for the rest of the session.
MAX_PREVIEW_CACHE_ENTRIES: Final = 64

#: Only this many heavyweight GTK cards exist at once.  A real MotionBGS run
#: measured roughly 0.6 MB of additional RSS per retained card, so keeping 600
#: widgets made Browse approach 600 MB.  Forty preserves the site's natural
#: photo-page size and gives a useful scroll surface without retaining every
#: result somebody has passed on the way to page fifteen.
MAX_MATERIALIZED_RESULTS: Final = 40

#: Candidate metadata is cheap, but it is still remote-controlled input.  Keep
#: enough page descriptions for the whole currently measured MotionBGS 4K
#: catalogue (7,380 entries) while retaining an honest finite ceiling.  The
#: widgets and decoded preview cache have their own much smaller bounds above.
MAX_RETAINED_CANDIDATES: Final = 10_000

#: Cards within one screen on either side of the viewport are worth fetching.
#: Anything farther away can wait until scrolling makes it relevant.
PREVIEW_LOOKAHEAD_SCREENS: Final = 1.0
PREVIEW_FALLBACK_CARDS: Final = 24

#: Card geometry, 16:9 like the library grid so the two views agree.
CARD_WIDTH: Final = 300
CARD_HEIGHT: Final = 169

type CandidateKey = tuple[str, str]
type RequestFingerprint = tuple[str, browse.Filters]


def _candidate_key(candidate: WallpaperCandidate) -> CandidateKey:
    """Identity that stays unique when two providers reuse the same id."""
    return candidate.provider, candidate.identifier


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


def _shortcut(action: Callable[[], None]) -> Callable[[Gtk.Widget, object], bool]:
    """Adapt a plain callable to `Gtk.CallbackAction`'s signature.

    Always reports the shortcut as handled, so a chord this dialog claims does
    not also reach whatever is behind it.
    """

    def run(_widget: Gtk.Widget, _arguments: object) -> bool:
        action()
        return True

    return run


# -- off-thread previews -------------------------------------------------


PreviewCallback = Callable[[WallpaperCandidate, bytes], None]


class PreviewLoader:
    """Fetches candidate thumbnails off-thread and decodes them for GTK."""

    def __init__(
        self,
        browser: Browser,
        max_workers: int = MAX_WORKERS,
        max_cache_entries: int = MAX_PREVIEW_CACHE_ENTRIES,
    ) -> None:
        self._browser = browser
        self._max_workers = max_workers
        self._max_cache_entries = max_cache_entries
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="preview")
        self._processes = worker_processes.Cancellation()
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._waiting: dict[str, tuple[float, int, WallpaperCandidate, PreviewCallback]] = {}
        self._active: set[str] = set()
        self._desired: set[str] = set()
        self._sequence = 0
        self._lock = threading.RLock()
        self._closed = False

    def prioritize(
        self,
        candidates: Sequence[tuple[WallpaperCandidate, float]],
        callback: PreviewCallback,
    ) -> None:
        """Fetch only the current viewport neighbourhood, nearest cards first.

        Work already using a network connection is allowed to finish, but a
        queued card that has fallen far outside the viewport is forgotten. At
        most one worker-width of stale work can therefore delay what somebody
        is looking at now.
        """
        cached: list[tuple[WallpaperCandidate, bytes]] = []
        with self._lock:
            if self._closed:
                return
            wanted = {candidate.thumbnail_url for candidate, _priority in candidates}
            wanted.discard("")
            self._desired = wanted
            self._waiting = {
                url: request for url, request in self._waiting.items() if url in wanted
            }
            for candidate, priority in candidates:
                url = candidate.thumbnail_url
                if not url:
                    continue
                hit = self._cache.pop(url, None)
                if hit is not None:
                    # Moving a hit to the end makes the in-memory tier a real
                    # LRU rather than an insertion-order bound.
                    self._cache[url] = hit
                    cached.append((candidate, hit))
                    continue
                if url in self._active:
                    continue
                self._sequence += 1
                self._waiting[url] = (priority, self._sequence, candidate, callback)
            self._pump_locked()
        for candidate, data in cached:
            callback(candidate, data)

    def _pump_locked(self) -> None:
        while not self._closed and len(self._active) < self._max_workers and self._waiting:
            url, request = min(self._waiting.items(), key=lambda item: (item[1][0], item[1][1]))
            _priority, _sequence, candidate, callback = request
            del self._waiting[url]
            self._active.add(url)
            future = self._pool.submit(self._fetch, candidate)
            future.add_done_callback(partial(self._finish, candidate, callback=callback))

    def _fetch(self, candidate: WallpaperCandidate) -> bytes:
        url = candidate.thumbnail_url
        # The disk tier. The dict above is free within one session and worth
        # nothing across two, so without this every card is re-downloaded from
        # the CDN each time the browser is opened.
        cached = thumbnails.lookup_preview(url)
        if cached:
            return cached
        try:
            data = self._browser.thumbnail(candidate)
        except ProviderError:
            # A preview that will not load is a card without a picture. The
            # download button still works.
            return b""
        # Decoding here rather than on the main thread: MotionBGS serves webp,
        # which means an ffmpeg call this closure's GdkPixbuf cannot avoid.
        displayable = thumbnails.to_displayable(data, processes=self._processes)
        # Cached after transcoding, so a webp preview costs one ffmpeg run
        # ever rather than one per session.
        thumbnails.store_preview(url, displayable)
        return displayable

    def _finish(
        self, candidate: WallpaperCandidate, future: Future[bytes], callback: PreviewCallback
    ) -> None:
        try:
            data = future.result()
        except Exception:
            # Broad on purpose: a worker must never take the app down.
            data = b""
        with self._lock:
            url = candidate.thumbnail_url
            self._active.discard(url)
            if self._closed:
                return
            self._cache.pop(url, None)
            self._cache[url] = data
            while len(self._cache) > self._max_cache_entries:
                self._cache.popitem(last=False)
            deliver_requested = url in self._desired
            self._pump_locked()

        def deliver() -> bool:
            if not self._closed and deliver_requested:
                callback(candidate, data)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            self._desired.clear()
            self._waiting.clear()
            self._active.clear()
            self._cache.clear()
        self._processes.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)


# -- one result ----------------------------------------------------------


class _CandidateCard(Gtk.Box):
    """One search result: a picture, what it is, and a download button."""

    def __init__(
        self,
        candidate: WallpaperCandidate,
        on_download: Callable[[WallpaperCandidate], None],
        on_open: Callable[[WallpaperCandidate], None] | None = None,
        on_pick: Callable[[WallpaperCandidate, bool], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.candidate = candidate
        self._on_download = on_download
        self._on_open = on_open
        self._on_pick = on_pick
        self.add_css_class("wio-tile")
        # Fixed width, not merely a minimum: a FlowBox hands children their
        # natural width, and a card that grows to fill the dialog stretches its
        # 16:9 frame into a letterbox.
        self.set_size_request(CARD_WIDTH, -1)
        self.set_hexpand(False)
        self.set_halign(Gtk.Align.CENTER)
        # In the focus chain, so arrow keys walk the grid. A page of results is
        # not a mouse target: 24 cards is a lot of pointing, and the whole
        # point of loading more on scroll is that there are far more than 24.
        self.set_focusable(True)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

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

        # Always present rather than revealed by a selection mode. A mode
        # would need discovering and then leaving, and the whole interaction
        # here is "these three, please".
        self._check = Gtk.CheckButton(tooltip_text="Pick for a batch download")
        self._check.connect("toggled", self._on_toggled)
        details.append(self._check)

        self._button = Gtk.Button(icon_name="folder-download-symbolic", tooltip_text="Download")
        self._button.add_css_class("flat")
        self._button.connect("clicked", self._on_clicked)
        details.append(self._button)
        self.append(details)

    @property
    def picked(self) -> bool:
        return self._check.get_active()

    @property
    def can_pick(self) -> bool:
        """Whether this card is still available to pick.

        False once the wallpaper is in the library, which is what stops a
        select-all appearing to tick something it cannot download.
        """
        return self._check.get_sensitive()

    def set_picked(self, picked: bool) -> None:
        """Tick or untick the batch checkbox.

        Not called `pick`: `Gtk.Widget.pick` is GTK's own hit-testing, and
        shadowing it on a widget would break far more than a checkbox.
        """
        self._check.set_active(picked)

    def _on_toggled(self, _check: Gtk.CheckButton) -> None:
        if self._on_pick is not None:
            self._on_pick(self.candidate, self.picked)

    def _on_clicked(self, _button: Gtk.Button) -> None:
        self._on_download(self.candidate)

    def _on_key(
        self, _controller: Gtk.EventControllerKey, keyval: int, _code: int, state: Gdk.ModifierType
    ) -> bool:
        """Enter opens, space picks, Ctrl+Enter downloads.

        Space is the pick rather than the download because picking is the
        reversible one: pressing it by accident ticks a box, where the other
        reading would start pulling a 40 MB video off somebody's server.
        """
        if state & Gdk.ModifierType.CONTROL_MASK:
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                if self._button.get_sensitive():
                    self._on_download(self.candidate)
                return True
            return False
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self._on_open is not None:
                self._on_open(self.candidate)
            return True
        if keyval == Gdk.KEY_space:
            if self._check.get_sensitive():
                self._check.set_active(not self._check.get_active())
            return True
        return False

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
        self._button.set_tooltip_text("Downloading" if busy else "Download")
        if busy:
            # Every route into the same download must become unavailable at
            # once.  Leaving the batch checkbox live is how one click on the
            # card and one click on the footer used to queue the same file.
            self._check.set_active(False)
        self._check.set_sensitive(not busy)

    def mark_downloaded(self) -> None:
        self._button.set_sensitive(False)
        self._button.set_icon_name("object-select-symbolic")
        self._button.set_tooltip_text("In your library")
        # Unpicked as well as disabled: a wallpaper that arrived while a batch
        # was running must not still be counted in "3 selected", and must not
        # be queued a second time by a later press.
        self._check.set_active(False)
        self._check.set_sensitive(False)


# -- the dialog ----------------------------------------------------------


class BrowseDialog(Adw.Dialog):
    """Search a provider and pull wallpapers into the library."""

    def __init__(self, application: Application) -> None:
        super().__init__()
        self._app = application
        # Downloads land in the first configured root, because that is the one
        # the user put first. Without one, downloads require a Settings choice.
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
        self._search_future: Future[SearchResult] | None = None
        self._downloads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="download")
        self._ownership_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browse-owned")
        self._ownership_future: Future[owned.Index] | None = None
        self._ownership_generation = 0
        # Detail views share one small lane.  A dialog used to create its own
        # executor, so opening a run of candidates could leave one provider
        # request and one OS thread per window alive until every request
        # timed out.  Two workers keep a slow provider from making the next
        # detail feel dead without allowing unbounded request/thread growth.
        self._details = ThreadPoolExecutor(max_workers=2, thread_name_prefix="detail")
        self._detail_processes = worker_processes.Cancellation()
        self._infos = self._browser.available
        #: Only the current explicit page.  Keeping this list bounded is the
        #: memory contract: `_CandidateCard` owns several GTK widgets and a
        #: decoded texture, while candidate records below are plain metadata.
        self._cards: list[_CandidateCard] = []
        self._candidates: list[WallpaperCandidate] = []
        self._candidate_by_key: dict[CandidateKey, WallpaperCandidate] = {}
        self._result_pages: list[tuple[WallpaperCandidate, ...]] = []
        self._result_page = 0
        self._advance_after_load = False
        self._picked_keys: set[CandidateKey] = set()
        #: A preview completion carries the materialisation generation which
        #: requested it.  Turning a page invalidates that token, so a late
        #: thumbnail cannot paint a newly-created card that happens to reuse
        #: the same provider identifier.
        self._preview_generation = 0
        self._result: SearchResult | None = None
        self._page = 1
        #: Whether the provider said there is another page. Also the switch
        #: that stops the grid asking for more, which is why a failed
        #: load-more clears it rather than retrying forever.
        self._has_next = False
        #: Identifiers already on screen, so an overlapping page cannot show
        #: the same wallpaper twice.
        self._shown: set[CandidateKey] = set()
        self._capped = False
        #: Batch progress. Counts every download this dialog started, not just
        #: the picked ones, so a single-card download that lands mid-batch does
        #: not make the count read wrong.
        self._queued = 0
        self._finished = 0
        #: Held across the pages of one random search so it does not re-roll
        #: between them, and cleared whenever a new search starts.
        self._seed = ""
        #: Search completions carry this generation back to the main loop.
        #: Changing provider or library roots invalidates the generation, so a
        #: slow answer cannot appear under controls that now describe a
        #: different request.
        self._search_generation = 0
        self._active_search_generation: int | None = None
        self._active_search_fingerprint: RequestFingerprint | None = None
        self._view_provider = ""
        #: Open detail views and downloads use provider-qualified identities.
        #: Wallhaven and MotionBGS are both allowed to call an item ``abc123``;
        #: an old completion must never mutate the other provider's surface.
        self._detail_dialogs: dict[CandidateKey, DetailDialog] = {}
        self._downloads_in_flight: set[CandidateKey] = set()
        self._searching = False
        self._closed = False
        self._presentation_parent: Gtk.Widget = self

        self.set_title("Store")
        self.set_content_width(1000)
        self.set_content_height(760)
        self.connect("closed", self._on_closed)

        self.set_child(self._build_content())
        self._add_shortcuts()
        # Wallhaven first: it is the one that answers an arbitrary query, and
        # MotionBGS only has videos, which not every setup can play.
        for index, info in enumerate(self._infos):
            if info.name == wallhaven.Wallhaven.name:
                self._providers.set_selected(index)
                break
        self._view_provider = self.provider_name
        self._sync_provider_controls()

    # -- keyboard --------------------------------------------------------

    def _add_shortcuts(self) -> None:
        """Ctrl+F to search, Ctrl+A to pick everything, Ctrl+D to download it.

        Escape is left to `Adw.Dialog`, which already closes on it.

        Ctrl+A selects the current explicit photo page. Picks made on earlier
        pages remain selected and the footer gives the combined count; it must
        never turn one shortcut into thousands of surprise downloads.
        """
        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.LOCAL)
        for accelerator, action in (
            ("<Control>f", self._focus_search),
            ("<Control>a", self._pick_all),
            ("<Control>d", self._download_all_picked),
        ):
            shortcuts.add_shortcut(
                Gtk.Shortcut(
                    trigger=Gtk.ShortcutTrigger.parse_string(accelerator),
                    action=Gtk.CallbackAction.new(_shortcut(action)),
                )
            )
        content = self.get_child()
        if content is not None:
            # `BrowsePage` reparents this widget and never presents the dialog.
            # Keeping the controller with the content keeps the shortcuts in
            # the hierarchy in both the dialog and embedded-page forms.
            content.add_controller(shortcuts)
        self._shortcuts = shortcuts

    def _focus_search(self) -> None:
        self._entry.grab_focus()

    def _pick_all(self) -> None:
        self._picked_keys.update(
            _candidate_key(card.candidate) for card in self._cards if card.can_pick
        )
        self._sync_visible_picks()
        self._update_pick_controls()

    # -- construction ----------------------------------------------------

    def _build_workshop_button(self) -> Gtk.Widget:
        """A way out to Steam's Workshop, which is not a provider and cannot be.

        Wallpaper Engine's catalogue is Steam's, and subscribing to an item has
        no unauthenticated API -- so a third search provider is not possible,
        and a scraper would be a browse-only tease that could never install
        anything. Steam already handles the account, the payment and the
        download; this hands off to it.

        Not offered when there is no Steam to hand off to, rather than opening
        a `steam://` link that does nothing.
        """
        button = Gtk.Button(
            icon_name="folder-remote-symbolic",
            tooltip_text="Get more wallpapers from the Steam Workshop",
        )
        button.connect("clicked", self._on_workshop_clicked)
        button.set_visible(workshop.is_steam_installed())
        return button

    def _on_workshop_clicked(self, _button: Gtk.Button) -> None:
        preferred, fallback = workshop.links()
        if not preferred:
            return
        launcher = Gtk.UriLauncher(uri=preferred)
        # No callback: whether Steam came to the front is Steam's business, and
        # there is nothing useful this dialog could do about it either way.
        launcher.launch(None, None, None, None)
        self.report(f"opening the Wallpaper Engine Workshop ({fallback})")

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._header = header
        header.set_title_widget(Adw.WindowTitle(title="Store", subtitle="Wallhaven, MotionBGS"))
        header.pack_end(self._build_workshop_button())
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

        self._scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self._scroller.set_child(self._flow)
        # Scrolling only reprioritises previews. Provider/result paging is an
        # explicit footer action, so a slow wheel cannot materialise hundreds
        # of heavyweight cards behind the viewport.
        self._scroller.get_vadjustment().connect("value-changed", self._on_scrolled)

        self._status = Adw.StatusPage(
            title="Nothing searched yet",
            description="Pick a provider and press Search.",
            icon_name="system-search-symbolic",
        )
        self._spinner = Adw.StatusPage(title="Searching...", icon_name="system-search-symbolic")

        self._stack = Gtk.Stack()
        self._stack.add_named(self._status, "empty")
        self._stack.add_named(self._spinner, "busy")
        self._stack.add_named(self._scroller, "results")
        self._stack.set_visible_child_name("empty")

        self._toast = Adw.ToastOverlay()
        self._toast.set_child(self._stack)
        toolbar.set_content(self._toast)
        toolbar.add_bottom_bar(self._build_pager())
        return toolbar

    def _build_search_bar(self) -> Gtk.Widget:
        area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Provider names, the text field and the labelled Search action all
        # grow with large-text accessibility.  A fixed horizontal box made
        # the last controls disappear at narrow window sizes even though the
        # results themselves already adapt.  Wrap whole controls so every
        # action stays reachable without introducing a horizontal scrollbar.
        bar = Adw.WrapBox(orientation=Gtk.Orientation.HORIZONTAL)
        bar.set_child_spacing(6)
        bar.set_line_spacing(6)
        bar.set_wrap_policy(Adw.WrapPolicy.NATURAL)
        self._search_controls = bar
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
        # `search-changed` is deliberately debounced for remote queries. This
        # control is local state, so `changed` keeps it honest on every edit.
        self._entry.connect("changed", self._on_search_changed)
        bar.append(self._entry)

        self._filters = Gtk.ToggleButton(icon_name="view-filter-symbolic", tooltip_text="Filters")
        self._filters.connect("toggled", self._on_filters_toggled)
        bar.append(self._filters)

        search = Gtk.Button(label="Search")
        search.add_css_class("suggested-action")
        search.connect("clicked", lambda _button: self.start_search(page=1))
        bar.append(search)
        area.append(bar)

        self._filter_revealer = Gtk.Revealer()
        self._filter_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._filter_revealer.set_child(self._build_filters())
        self._filter_revealer.connect("notify::reveal-child", self._on_filters_shown)
        area.append(self._filter_revealer)
        return area

    def _build_filters(self) -> Gtk.Box:
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
        self._motionbgs_hint = Gtk.Label(xalign=0.0, wrap=True)
        self._motionbgs_hint.set_label(
            "MotionBGS browses or searches; clear the search box to browse 4K, HD, "
            "latest, or by genre."
        )
        self._motionbgs_hint.add_css_class("dim-label")
        self._motionbgs_hint.add_css_class("caption")
        self._motionbgs_filters.append(self._motionbgs_hint)
        box.append(self._motionbgs_filters)

        # Re-asked every time the inline controls open rather than once at
        # construction: the settings dialogue can store a key while this dialog
        # is alive, and the window keeps one browse dialog for the session, so
        # a value read at construction would stay stale until it is closed.
        self._sync_nsfw_toggle()
        self._sync_top_range()
        self._sync_motionbgs_mode()
        return box

    def _on_filters_toggled(self, button: Gtk.ToggleButton) -> None:
        self._filter_revealer.set_reveal_child(button.get_active())

    def _on_filters_shown(self, revealer: Gtk.Revealer, _parameter: object) -> None:
        if revealer.get_reveal_child():
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
        """A real bounded pager plus count, batch state, and loading hint."""
        # Batch controls appear only after selection, so this row can grow by
        # three widgets after the page has already been allocated.  Wrapping
        # makes that transition honest at 800 px and under large text instead
        # of clipping Clear/Download beyond the right edge.
        bar = Adw.WrapBox(orientation=Gtk.Orientation.HORIZONTAL)
        bar.set_child_spacing(6)
        bar.set_line_spacing(6)
        bar.set_wrap_policy(Adw.WrapPolicy.NATURAL)
        self._pager = bar
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        bar.set_margin_start(12)
        bar.set_margin_end(12)

        self._summary = Gtk.Label(label="", xalign=0.0, hexpand=True)
        self._summary.add_css_class("dim-label")
        bar.append(self._summary)

        self._previous_results = Gtk.Button(
            icon_name="go-previous-symbolic",
            tooltip_text="Previous results page",
        )
        self._previous_results.add_css_class("flat")
        self._previous_results.set_sensitive(False)
        self._previous_results.connect("clicked", lambda _button: self._show_previous_page())
        bar.append(self._previous_results)

        self._result_page_label = Gtk.Label(label="")
        self._result_page_label.add_css_class("caption")
        bar.append(self._result_page_label)

        self._next_results = Gtk.Button(
            icon_name="go-next-symbolic",
            tooltip_text="Next results page",
        )
        self._next_results.add_css_class("flat")
        self._next_results.set_sensitive(False)
        self._next_results.connect("clicked", lambda _button: self._show_next_page())
        bar.append(self._next_results)

        self._more = Gtk.Label(label="")
        self._more.add_css_class("dim-label")
        self._more.add_css_class("caption")
        bar.append(self._more)

        # Its own label rather than sharing `_more`. Scrolling to the end of
        # the grid while a batch downloads is an ordinary thing to do, and one
        # label would have the two states overwriting each other.
        self._queue = Gtk.Label(label="")
        self._queue.add_css_class("caption")
        bar.append(self._queue)

        # Hidden until something is picked, so the footer stays a footer until
        # there is actually a batch to act on.
        self._picked = Gtk.Label(label="")
        self._picked.add_css_class("caption")
        self._picked.set_visible(False)
        bar.append(self._picked)

        self._clear_picked = Gtk.Button(label="Clear")
        self._clear_picked.add_css_class("flat")
        self._clear_picked.set_visible(False)
        self._clear_picked.connect("clicked", lambda _button: self._unpick_all())
        bar.append(self._clear_picked)

        self._download_picked = Gtk.Button(label="Download")
        self._download_picked.add_css_class("suggested-action")
        self._download_picked.set_visible(False)
        self._download_picked.connect("clicked", lambda _button: self._download_all_picked())
        bar.append(self._download_picked)
        return bar

    # -- picking several ---------------------------------------------------

    def _on_pick_changed(self, candidate: WallpaperCandidate, picked: bool) -> None:
        key = _candidate_key(candidate)
        if picked:
            self._picked_keys.add(key)
        else:
            self._picked_keys.discard(key)
        self._update_pick_controls()

    def _update_pick_controls(self) -> None:
        count = len(self._picked_keys)
        self._picked.set_label(f"{count} selected")
        for widget in (self._picked, self._clear_picked, self._download_picked):
            widget.set_visible(count > 0)

    def _sync_visible_picks(self) -> None:
        for card in self._cards:
            card.set_picked(_candidate_key(card.candidate) in self._picked_keys)

    def _unpick_all(self) -> None:
        self._picked_keys.clear()
        self._sync_visible_picks()
        self._update_pick_controls()

    def _download_all_picked(self) -> None:
        """Queue everything picked, then let go of the selection.

        The pool is one worker wide, so these run one at a time and the
        remote is asked for one file at a time -- which is the polite shape
        for a scraper and the only sane one for a 40 MB video.

        The selection is dropped immediately rather than as each lands: the
        request has been made, and leaving the boxes ticked would invite
        pressing Download again and queueing the whole batch twice.
        """
        picked = [
            candidate
            for candidate in self._candidates
            if _candidate_key(candidate) in self._picked_keys
        ]
        if not picked:
            return
        for candidate in picked:
            self._on_download(candidate)
        self._unpick_all()

    def _report_queue(self) -> None:
        """Say where the batch has got to, and forget it once it is done.

        Resetting both counters at zero outstanding is what lets a second
        batch read "1 of 3" rather than continuing the first one's numbering.
        """
        if self._finished >= self._queued:
            self._queued = self._finished = 0
            self._queue.set_label("")
            return
        self._queue.set_label(f"downloading {self._finished + 1} of {self._queued}")

    # -- provider selection ------------------------------------------------

    @property
    def provider_name(self) -> str:
        index = self._providers.get_selected()
        if index >= len(self._infos):
            return self._infos[0].name
        return self._infos[index].name

    def _on_provider_changed(self, *_arguments: object) -> None:
        selected = self.provider_name
        if self._view_provider and selected != self._view_provider:
            title = next(
                (info.title for info in self._infos if info.name == selected),
                selected,
            )
            self._invalidate_search(
                title=f"Browse {title}",
                description="Choose filters and press Search.",
            )
        self._view_provider = selected
        self._sync_provider_controls()

    def update_library_roots(self, roots: tuple[Path, ...]) -> None:
        """Retarget downloads and ownership checks after Settings changes.

        The Browse page lives as long as the window, so constructing its
        ``Browser`` once is not enough.  Keep its provider caches, but replace
        both folder roles and discard results whose downloaded badges describe
        the previous library.
        """
        self._ownership_generation += 1
        self._browser.configure_roots(
            root=roots[0] if roots else None,
            library_roots=roots,
        )
        self._invalidate_search(
            title="Library folders changed",
            description="Search again to check the newly configured library.",
        )

    def _invalidate_search(self, *, title: str, description: str) -> None:
        """Make every outstanding completion stale and reset the result view."""
        self._search_generation += 1
        if self._search_future is not None:
            self._search_future.cancel()
        self._active_search_generation = None
        self._active_search_fingerprint = None
        self._searching = False
        self._result = None
        self._page = 1
        self._has_next = False
        self._shown.clear()
        self._capped = False
        self._advance_after_load = False
        self._seed = ""
        self._more.set_label("")
        self._summary.set_label("")
        self._clear()
        self._status.set_title(title)
        self._status.set_description(description)
        self._stack.set_visible_child_name("empty")

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._sync_motionbgs_mode()

    def _on_mode_changed(self, *_arguments: object) -> None:
        self._genre_row.set_visible(MODES[self._mode.get_selected()][0] == "genre")

    def _sync_motionbgs_mode(self) -> None:
        searching = bool(self._entry.get_text().strip())
        self._mode.set_sensitive(not searching)
        self._genre.set_sensitive(not searching)
        self._motionbgs_hint.set_visible(searching)

    def _sync_provider_controls(self) -> None:
        """Show only the filters the selected provider actually understands."""
        is_wallhaven = self.provider_name == wallhaven.Wallhaven.name
        self._wallhaven_filters.set_visible(is_wallhaven)
        self._motionbgs_filters.set_visible(not is_wallhaven)
        if not is_wallhaven:
            self._on_mode_changed()
        self._sync_motionbgs_mode()
        self._entry.set_placeholder_text("Search Wallhaven" if is_wallhaven else "Search MotionBGS")

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

    def start_search(self, *, page: int, append: bool = False) -> None:
        """Ask for ``page``. ``append`` adds to the grid instead of replacing it.

        The two callers are a person pressing Search, and the grid running out
        of results to show. They differ only in whether what is already on
        screen survives.
        """
        if self._closed or page < 1:
            return
        current_fingerprint = (self.provider_name, self._read_filters())
        if self._searching:
            if current_fingerprint == self._active_search_fingerprint:
                return
            # The visible controls changed while an older request was in
            # flight.  Make that completion stale and let the new request join
            # the single search lane behind it instead of forcing the user to
            # press Search again after it returns.
            self._search_generation += 1
            self._active_search_generation = None
            self._active_search_fingerprint = None
            self._searching = False
        if not append:
            # A fresh search re-rolls; loading more within one does not.
            # Without this, asking for random wallpapers twice would return
            # the same ones, because the seed that keeps page two honest would
            # also pin page one.
            self._seed = ""
            self._search_generation += 1
        try:
            query = self._query(page)
        except ProviderError as error:
            self.report(str(error))
            return
        name = self.provider_name
        fingerprint = (name, self._read_filters())
        generation = self._search_generation
        if not append:
            # Submission is the lifecycle boundary for a result selection.
            # Waiting until a successful answer to clear it leaves a failed
            # new search showing an error page with an actionable Download
            # button for invisible candidates from the previous query.
            self._clear()
            self._summary.set_label("")
            self._more.set_label("")
        self._active_search_generation = generation
        self._active_search_fingerprint = fingerprint
        self._searching = True
        if append:
            # The current bounded page stays while the next provider page is
            # fetched.  Swapping to the full-page spinner would throw away the
            # user's scroll/focus context.
            self._more.set_label("Loading next page…")
            self._update_pager()
        else:
            self._stack.set_visible_child_name("busy")

        def work() -> SearchResult:
            if self._closed or generation != self._search_generation:
                raise CancelledError("search was superseded")
            return self._browser.search(name, query)

        if self._search_future is not None:
            self._search_future.cancel()
        future = self._searches.submit(work)
        self._search_future = future
        future.add_done_callback(
            lambda done: self._deliver(
                done,
                page,
                append,
                generation=generation,
                fingerprint=fingerprint,
            )
        )

    def _deliver(
        self,
        future: Future[SearchResult],
        page: int,
        append: bool = False,
        *,
        generation: int,
        fingerprint: RequestFingerprint,
    ) -> None:
        try:
            result: SearchResult | None = future.result()
            message = ""
        except ProviderError as error:
            result, message = None, str(error)
        except Exception as error:
            # Broad on purpose: a search must not be able to kill the app.
            result, message = None, f"search failed: {error}"

        def deliver() -> bool:
            owns_active_request = (
                generation == self._search_generation
                and generation == self._active_search_generation
                and fingerprint == self._active_search_fingerprint
            )
            current = owns_active_request and fingerprint == (
                self.provider_name,
                self._read_filters(),
            )
            if not self._closed and current:
                self._searching = False
                self._active_search_generation = None
                self._active_search_fingerprint = None
                self._more.set_label("")
                if result is None:
                    self._show_failure(message, append)
                else:
                    self._show_result(result, page, append)
            elif not self._closed and owns_active_request:
                # The user edited the visible query or filters but did not
                # submit them yet.  Never put this old answer underneath those
                # new controls, and never leave the page saying Searching.
                self._invalidate_search(
                    title="Search changed",
                    description="Press Search to use the current query and filters.",
                )
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def _show_failure(self, message: str, append: bool = False) -> None:
        if append:
            # Failing to load the next explicit page must not throw away what
            # is on screen. Stop offering a dead Next button until a new
            # search, while retaining every page already reached.
            self._has_next = False
            self._advance_after_load = False
            self._update_pager()
            self.report(message)
            return
        self._stack.set_visible_child_name("empty")
        self._status.set_title("That search did not work")
        self._status.set_description(message)
        self.report(message)

    def _show_result(self, result: SearchResult, page: int, append: bool = False) -> None:
        self._result = result
        self._page = page
        self._has_next = result.has_next
        if not append:
            self._clear()
            self._shown.clear()
            self._capped = False

        added: list[WallpaperCandidate] = []
        for candidate in result.items:
            # A provider page may overlap the one before it. Retain only one
            # metadata record, so moving backwards cannot show a duplicate.
            key = _candidate_key(candidate)
            if key in self._shown:
                continue
            if len(self._candidates) >= MAX_RETAINED_CANDIDATES:
                self._capped = True
                self._has_next = False
                break
            self._shown.add(key)
            self._candidates.append(candidate)
            self._candidate_by_key[key] = candidate
            added.append(candidate)

        if len(self._candidates) >= MAX_RETAINED_CANDIDATES and result.has_next:
            self._capped = True
            self._has_next = False

        for start in range(0, len(added), MAX_MATERIALIZED_RESULTS):
            self._result_pages.append(tuple(added[start : start + MAX_MATERIALIZED_RESULTS]))

        if not self._result_pages:
            self._stack.set_visible_child_name("empty")
            self._status.set_title("No results")
            self._status.set_description("Nothing came back for that query.")
        else:
            self._stack.set_visible_child_name("results")
            if not append:
                self._materialize_page(0)
            elif self._advance_after_load and self._result_page + 1 < len(self._result_pages):
                self._materialize_page(self._result_page + 1, focus=True)

        self._summary.set_label(self._describe(result))
        if append and not added and result.has_next:
            # A provider that repeats a page must not turn each press of Next
            # into another request with no visible progress.
            self._has_next = False
        self._advance_after_load = False
        self._update_pager()

    def _describe(self, result: SearchResult) -> str:
        shown = len(self._candidates)
        if self._capped:
            parts = [f"showing the first {shown} results"]
        else:
            parts = [f"{shown} result{'' if shown == 1 else 's'}"]
        if result.total_hint:
            parts.append(f"of about {result.total_hint}")
        if result.dropped:
            # A sudden jump here means the remote's markup moved, so it is
            # reported rather than quietly swallowed.
            parts.append(f"{result.dropped} unreadable")
        if result.cached:
            parts.append("cached")
        return " - ".join(parts)

    # -- bounded result pages ----------------------------------------------

    def _on_scrolled(self, _adjustment: Gtk.Adjustment) -> None:
        self._refresh_previews()

    def _show_previous_page(self) -> None:
        if self._result_page > 0:
            self._materialize_page(self._result_page - 1, focus=True)

    def _show_next_page(self) -> None:
        if self._searching:
            return
        if self._result_page + 1 < len(self._result_pages):
            self._materialize_page(self._result_page + 1, focus=True)
            return
        if not self._has_next:
            return
        self._advance_after_load = True
        self.start_search(page=self._page + 1, append=True)
        if not self._searching:
            self._advance_after_load = False
        self._update_pager()

    def _materialize_page(self, page: int, *, focus: bool = False) -> None:
        """Replace only the bounded card page; metadata and interaction state stay."""
        if page < 0 or page >= len(self._result_pages):
            return
        self._clear_materialized_cards()
        self._result_page = page
        held = self._ready_owned()
        for candidate in self._result_pages[page]:
            key = _candidate_key(candidate)
            card = _CandidateCard(
                candidate,
                self._on_download,
                self._open_detail,
                self._on_pick_changed,
            )
            if held.holds(candidate):
                self._picked_keys.discard(key)
                card.mark_downloaded()
            elif key in self._downloads_in_flight:
                self._picked_keys.discard(key)
                card.set_busy(True)
            elif key in self._picked_keys:
                card.set_picked(True)
            self._cards.append(card)
            self._flow.append(card)
        self._update_pick_controls()
        self._update_pager()

        def settle_page() -> bool:
            adjustment = self._scroller.get_vadjustment()
            adjustment.set_value(adjustment.get_lower())
            self._refresh_previews()
            if focus and self._cards:
                self._cards[0].grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(settle_page)

    def _update_pager(self) -> None:
        pages = len(self._result_pages)
        if pages == 0:
            self._result_page_label.set_label("")
            self._previous_results.set_sensitive(False)
            self._next_results.set_sensitive(False)
            return
        suffix = " · more online" if self._has_next else ""
        self._result_page_label.set_label(f"Page {self._result_page + 1} of {pages}{suffix}")
        self._previous_results.set_sensitive(not self._searching and self._result_page > 0)
        self._next_results.set_sensitive(
            not self._searching and (self._result_page + 1 < pages or self._has_next)
        )

    def _refresh_previews(self) -> bool:
        """Keep only a viewport-sized, distance-ordered preview queue."""
        callback = partial(self._on_preview, self._preview_generation)
        if self._closed or not self._cards:
            self._loader.prioritize((), callback)
            return GLib.SOURCE_REMOVE
        adjustment = self._scroller.get_vadjustment()
        value = adjustment.get_value()
        page_size = adjustment.get_page_size()
        upper = adjustment.get_upper()
        if page_size <= 1.0 or upper <= 1.0:
            nearby = [
                (card.candidate, float(index))
                for index, card in enumerate(self._cards[:PREVIEW_FALLBACK_CARDS])
            ]
        else:
            start = max(0.0, value - page_size * PREVIEW_LOOKAHEAD_SCREENS)
            end = value + page_size * (1.0 + PREVIEW_LOOKAHEAD_SCREENS)
            centre = value + page_size / 2.0
            nearby = []
            for card in self._cards:
                child = card.get_parent()
                if child is None:
                    continue
                allocation = child.get_allocation()
                top = float(allocation.y)
                bottom = top + float(allocation.height)
                if bottom < start or top > end:
                    continue
                priority = abs((top + bottom) / 2.0 - centre)
                nearby.append((card.candidate, priority))
        self._loader.prioritize(nearby, callback)
        return GLib.SOURCE_REMOVE

    def _clear(self) -> None:
        self._clear_materialized_cards()
        self._candidates.clear()
        self._candidate_by_key.clear()
        self._result_pages.clear()
        self._result_page = 0
        self._advance_after_load = False
        self._picked_keys.clear()
        self._update_pick_controls()
        self._update_pager()

    def _clear_materialized_cards(self) -> None:
        self._preview_generation += 1
        self._loader.prioritize(
            (),
            partial(self._on_preview, self._preview_generation),
        )
        for card in self._cards:
            self._flow.remove(card)
        self._cards.clear()

    def _on_preview(
        self,
        generation: int,
        candidate: WallpaperCandidate,
        data: bytes,
    ) -> None:
        if generation != self._preview_generation:
            # The loader has cached this completion already. If a recreated
            # card wants the same URL, ask again on the next main-loop turn so
            # it receives the cache hit under the current generation instead
            # of remaining blank.
            GLib.idle_add(self._refresh_previews)
            return
        for card in self._cards:
            if _candidate_key(card.candidate) == _candidate_key(candidate):
                card.set_preview(data)

    # -- downloading -------------------------------------------------------

    def _open_detail(self, candidate: WallpaperCandidate) -> None:
        """Show everything the provider knows about one result.

        The dialog is remembered so that a download started from inside it can
        report back: it owns its own button, and a card behind it is not
        necessarily the thing the user is looking at.
        """
        key = _candidate_key(candidate)
        existing = self._detail_dialogs.get(key)
        if existing is not None:
            existing.present(self._presentation_parent)
            return
        detail = DetailDialog(
            self._browser,
            candidate,
            self._on_download,
            executor=self._details,
            processes=self._detail_processes,
            held=self._ready_owned().holds(candidate),
        )
        self._detail_dialogs[key] = detail

        def closed(dialog: DetailDialog) -> None:
            # Do not let an older dialog's close remove a replacement.
            if self._detail_dialogs.get(key) is dialog:
                self._detail_dialogs.pop(key, None)

        detail.connect("closed", closed)
        if key in self._downloads_in_flight:
            detail.set_busy(True)
        detail.present(self._presentation_parent)

    def _on_download(self, candidate: WallpaperCandidate, variant: str = "") -> None:
        if self._closed:
            return
        if not self._app.require_authoring_ready():
            return
        key = _candidate_key(candidate)
        if key in self._downloads_in_flight:
            self.report(f"{candidate.title or candidate.identifier} is already downloading")
            return
        self._downloads_in_flight.add(key)
        self._set_download_busy(candidate, True)
        # Counted here rather than in the batch, so the one place a download
        # starts is the one place it is counted. A single card pressed while a
        # batch runs joins that batch's total instead of being invisible.
        self._queued += 1
        self._report_queue()

        def work() -> Downloaded:
            return self._browser.download(candidate, variant=variant)

        future = self._downloads.submit(work)
        future.add_done_callback(lambda done: self._downloaded(candidate, done))

    def _card_for(self, candidate: WallpaperCandidate) -> _CandidateCard | None:
        for card in self._cards:
            if _candidate_key(card.candidate) == _candidate_key(candidate):
                return card
        return None

    def _set_download_busy(self, candidate: WallpaperCandidate, busy: bool) -> None:
        key = _candidate_key(candidate)
        if busy:
            self._picked_keys.discard(key)
        card = self._card_for(candidate)
        if card is not None:
            card.set_busy(busy)
        detail = self._detail_dialogs.get(key)
        if detail is not None:
            detail.set_busy(busy)
        self._update_pick_controls()

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
            self._downloads_in_flight.discard(_candidate_key(candidate))
            if self._closed:
                return GLib.SOURCE_REMOVE
            # Counted whether it worked or not: a batch of five with one
            # failure has still finished, and a progress line that never
            # reaches the end is worse than one that stops early.
            self._finished += 1
            self._report_queue()
            card = self._card_for(candidate)
            detail = self._detail_dialogs.get(_candidate_key(candidate))
            if done is None:
                self._set_download_busy(candidate, False)
                self.report(message)
            else:
                self._picked_keys.discard(_candidate_key(candidate))
                if card is not None:
                    card.mark_downloaded()
                if detail is not None:
                    detail.downloaded()
                self._update_pick_controls()
                self.report(done.describe())
                # The file is in the library directory but not in the library
                # until something looks again.
                self._app.refresh_library()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    # -- housekeeping ------------------------------------------------------

    def _ready_owned(self) -> owned.Index:
        """Render immediately; a cold ownership snapshot belongs on a worker."""
        ready = self._browser.cached_owned
        if ready is None:
            self._refresh_ownership()
        return ready if ready is not None else owned.Index()

    def library_refreshed(self) -> None:
        self._browser.forget_owned()
        self._ownership_generation += 1
        if self._result_pages or self._detail_dialogs:
            self._refresh_ownership()

    def _refresh_ownership(self) -> None:
        if self._closed or self._ownership_future is not None:
            return
        generation = self._ownership_generation
        future = self._ownership_jobs.submit(lambda: self._browser.owned)
        self._ownership_future = future
        future.add_done_callback(
            lambda done: GLib.idle_add(self._finish_ownership, generation, done)
        )

    def _finish_ownership(self, generation: int, future: Future[owned.Index]) -> bool:
        if self._ownership_future is future:
            self._ownership_future = None
        try:
            ready = future.result()
        except Exception:
            # Ownership is a display hint, never download/deletion authority.
            # A later search or library refresh can retry an unreadable drive.
            return GLib.SOURCE_REMOVE
        if self._closed:
            return GLib.SOURCE_REMOVE
        if generation != self._ownership_generation:
            self._refresh_ownership()
            return GLib.SOURCE_REMOVE
        for card in self._cards:
            if ready.holds(card.candidate):
                card.mark_downloaded()
            else:
                card.set_busy(_candidate_key(card.candidate) in self._downloads_in_flight)
        for key, detail in self._detail_dialogs.items():
            detail.set_held(ready.holds(detail._candidate))
            detail.set_busy(key in self._downloads_in_flight)
        self._update_pick_controls()
        return GLib.SOURCE_REMOVE

    def report(self, message: str) -> None:
        self._toast.add_toast(Adw.Toast.new(message))

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        for detail in tuple(self._detail_dialogs.values()):
            detail.cancel_pending()
        self._detail_dialogs.clear()
        self._browser.shutdown()
        self._loader.shutdown()
        self._detail_processes.cancel()
        self._searches.shutdown(wait=False, cancel_futures=True)
        self._ownership_jobs.shutdown(wait=False, cancel_futures=True)
        self._details.shutdown(wait=False, cancel_futures=True)
        # Closing the shared transport wakes active transfers. Providers stage
        # under a private dot-name and only publish after validation, so a
        # cancelled transfer leaves no half-wallpaper in the library.
        self._downloads.shutdown(wait=False, cancel_futures=True)


class BrowsePage(Gtk.Box):
    """The established browser surface embedded as a primary application tab.

    ``BrowseDialog`` remains the tested implementation and compatibility
    surface. Its child is reparented here before either widget is presented;
    callbacks continue to own their worker pools while detail dialogs are
    parented to this visible page.
    """

    def __init__(self, application: Application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._surface = BrowseDialog(application)
        content = self._surface.get_child()
        self._surface.set_child(None)
        if content is not None:
            self.append(content)
        self._surface._presentation_parent = self
        self._surface._header.set_visible(False)

    def focus_search(self) -> None:
        self._surface._focus_search()

    def update_library_roots(self, roots: tuple[Path, ...]) -> None:
        self._surface.update_library_roots(roots)

    def library_refreshed(self) -> None:
        """Make the next provider result compare against the accepted scan."""
        self._surface.library_refreshed()

    def shutdown(self) -> None:
        if not self._surface._closed:
            self._surface._on_closed(self._surface)


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
        executor: ThreadPoolExecutor | None = None,
        processes: worker_processes.Cancellation | None = None,
        held: bool = False,
    ) -> None:
        super().__init__()
        self._browser = browser
        self._candidate = candidate
        self._on_download = on_download
        self._held = held
        self._closed = False
        self._detail: CandidateDetail | None = None
        # BrowseDialog supplies its one shared bounded detail lane.  Keeping a
        # private fallback makes the widget independently testable without
        # restoring the production one-dialog/one-thread design.
        self._pool = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="detail-standalone"
        )
        self._owns_pool = executor is None
        self._processes = processes or worker_processes.Cancellation()
        self._owns_processes = processes is None
        self._future: Future[tuple[CandidateDetail, bytes]] | None = None
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
            url = detail.preview_url
            # A preview is optional. CDN, conversion, or cache failures must
            # not discard successfully fetched facts and download qualities.
            try:
                cached = thumbnails.lookup_preview(url)
                if cached:
                    return detail, cached
                picture = self._browser.preview(url)
                displayable = (
                    thumbnails.to_displayable(picture, processes=self._processes)
                    if picture
                    else b""
                )
                thumbnails.store_preview(url, displayable)
                return detail, displayable
            except Exception:
                return detail, b""

        future = self._pool.submit(work)
        self._future = future
        future.add_done_callback(self._deliver)

    def _deliver(self, future: Future[tuple[CandidateDetail, bytes]]) -> None:
        if self._future is future:
            self._future = None
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
        self.set_held(True)

    def set_held(self, held: bool) -> None:
        self._held = held
        if held:
            self._mark_held()

    def failed(self) -> None:
        self.set_busy(False)

    def set_busy(self, busy: bool) -> None:
        """Reflect one shared candidate download, whichever surface started it."""
        if self._held:
            self._mark_held()
            return
        self._download.set_sensitive(not busy)
        self._download.set_label("Downloading…" if busy else "Download")

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self.cancel_pending()

    def cancel_pending(self) -> None:
        """Stop this view receiving a queued/running detail result."""
        self._closed = True
        future = self._future
        self._future = None
        if future is not None:
            future.cancel()
        if self._owns_processes:
            self._processes.cancel()
        if self._owns_pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
