"""The real `StoreAccess`: the classic Store's `Browser`, lanes and rules.

Searching, describing and downloading are `wall_in_one.browse.Browser`'s, the
same object the classic Store and ``ctl search``/``download`` drive: one
transport shared by both providers, MotionBGS parsing in the shared
subinterpreter pool, a cross-process claim per download (``busy`` while
another request holds it, ``conflict`` when the library already does), and
downloads into the first library folder. Card pictures come through the
classic `PreviewLoader` (bounded to the cards on screen, a disk tier, ffmpeg
for WebP) and are decoded on a worker here.

**Threads.** Every network request, provider parse, disk walk (which results
the library holds), key-file read and Steam lookup runs on a lane of its own,
as the classic Store's do: one search at a time, one download at a time, two
detail requests, one ownership walk. Each answer comes back on the main loop
at ``GLib.PRIORITY_DEFAULT``, and a search that a newer one superseded is
dropped, never delivered.

**After a download.** The file is in a library folder, not yet in the
library. The adapter asks the application to rescan
(`Application.refresh_library`) and, for "Download & apply" or "Show in
Library", waits for that rescan's completion before it looks the file up and
applies it (`RealAppState.apply`) or opens it. A download that outlived a
change of library folders (``root_current`` false) went to the old folder,
which the library no longer scans: it is said so, and Apply stays off for it.

**Lifetime.** Lanes start on first use and stop with the window
(`RealStore.close`, from `RealAppState.close`); closing the transport wakes a
running transfer, and the providers stage under a private name, so nothing
half-written is left. A window that comes back starts them again.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Final, Protocol, TypeVar, runtime_checkable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk

from wall_in_one import browse, config, thumbnails, worker_processes
from wall_in_one.browse import Browser, Downloaded
from wall_in_one.library import owned, workshop
from wall_in_one.library.model import Kind
from wall_in_one.providers import registry
from wall_in_one.providers.base import (
    CandidateDetail,
    ProviderError,
    SearchQuery,
    SearchResult,
    WallpaperCandidate,
)
from wall_in_one.session import Session
from wall_in_one.ui.browse_dialog import PreviewLoader
from wall_in_one.ui.next.store_state import (
    CATEGORIES,
    COLOURS,
    PROVIDER,
    PURITIES,
    RATIOS,
    SIZES,
    WALLHAVEN,
    DetailDone,
    Downloads,
    PictureDone,
    SearchDone,
    StoreDetail,
    StoreItemView,
    StoreProblem,
    StoreQuery,
    StoreResults,
    Then,
    bits,
    option,
)

_T = TypeVar("_T")

#: Decoded card pictures kept for instant page turns: a few pages' worth.
PICTURE_CAPACITY: Final = 160


@runtime_checkable
class StoreBackend(Protocol):
    """What the Store uses of `wall_in_one.ui.app.Application`."""

    @property
    def session(self) -> Session: ...

    @property
    def settings(self) -> config.Settings: ...

    def refresh_library(self, *, finished: Callable[[bool], None] | None = None) -> None: ...

    def require_authoring_ready(self, *, report: bool = True) -> bool: ...

    def window_report(self, message: str) -> None: ...


class StoreHost(Protocol):
    """What the Store uses of the `AppState` it belongs to."""

    def connect(self, detailed_signal: str, handler: Callable[..., Any], *args: Any) -> int: ...

    def emit_changed(self, *topics: str) -> None: ...

    def toast(
        self,
        text: str,
        undo: Callable[[], None] | None = None,
        action: tuple[str, Callable[[], None]] | None = None,
    ) -> None: ...

    def navigate(self, page: str) -> None: ...

    @property
    def scope(self) -> str: ...

    def has_wallpaper(self, wid: str) -> bool: ...

    def apply(self, wid: str, scope: str = "all") -> None: ...

    def apply_blocked(self, wid: str) -> str: ...


# -- the filters, as the classic Store sends them ----------------------------------------


def filters_for(query: StoreQuery, seed: str = "") -> browse.Filters:
    """``query`` in `browse.Filters`' terms, which owns every provider rule."""
    return browse.Filters(
        text=query.text.strip(),
        sorting=query.sort,
        order="asc" if query.reverse and query.can_reverse else "desc",
        categories=bits(query.categories, CATEGORIES),
        purity=bits(query.purity, PURITIES),
        atleast=option(SIZES, query.size, 2),
        ratios=option(RATIOS, query.ratio, 2),
        colour=query.colour if query.colour in COLOURS else "",
        top_range=query.top_range,
        seed=seed,
        mode=query.mode,
        genre=query.genre.strip(),
    )


#: Why nothing can be downloaded without a library folder, and where to add one.
NO_FOLDER: Final = (
    "Downloads go into your first library folder, and there is none yet. "
    "Add one in Settings \u2192 Library"
)
#: Settings, scrolled to its Library group.
SETTINGS_LIBRARY: Final = "settings:library"


def _words(error: ProviderError) -> str:
    """A provider error's message without its kind, for the Store's error page."""
    return Exception.__str__(error)


def search_request(query: StoreQuery, page: int, seed: str = "") -> tuple[SearchQuery, str]:
    """The provider request for ``page``, and the random-sort seed to keep for the next.

    The classic Store's rules: a seed is held across the pages of one random
    search and dropped by any other sorting (Wallhaven refuses a stale one),
    and an empty genre is refused here with words about the empty box.
    """
    filters = filters_for(query, seed)
    if query.provider == WALLHAVEN:
        filters = filters.seeded()
        return filters.to_query(browse.WALLHAVEN, page), filters.seed
    if filters.motionbgs_options().get("mode") == "genre" and not filters.genre:
        raise ProviderError("validation", "type a genre, or pick another way to browse")
    return filters.to_query(browse.MOTIONBGS, page), ""


# -- results ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateView:
    """One provider result as the Store page shows it (`StoreItemView`)."""

    candidate: WallpaperCandidate

    @property
    def id(self) -> str:
        return f"{self.candidate.provider}:{self.candidate.identifier}"

    @property
    def provider(self) -> str:
        return self.candidate.provider

    @property
    def title(self) -> str:
        return self.candidate.title or self.candidate.identifier

    @property
    def is_moving(self) -> bool:
        return self.candidate.kind is Kind.VIDEO

    @property
    def resolution(self) -> str:
        return self.candidate.resolution

    @property
    def detail(self) -> str:
        resolution = self.candidate.resolution.replace("x", " \u00d7 ")
        if self.is_moving:
            return f"Video · {resolution}" if resolution else "Video"
        return resolution


def _candidate(item: StoreItemView) -> WallpaperCandidate:
    if not isinstance(item, CandidateView):
        raise TypeError(f"not a Store result of this adapter: {item!r}")
    return item.candidate


def _home_relative(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def _later(work: Callable[[], None]) -> None:
    """Run ``work`` on the main loop above GTK's redraw (it ends a busy state)."""

    def run() -> bool:
        work()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]


def _decode(data: bytes) -> Gdk.Texture | None:
    """A texture from image bytes; on a worker (GDK textures are immutable)."""
    if not data:
        return None
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
    except GLib.Error:
        return None


class _Lanes:
    """One window's worth of Browser, transport and worker lanes."""

    def __init__(self, browser: Browser) -> None:
        self.browser = browser
        self.previews = PreviewLoader(browser)
        # Separate lanes, as the classic Store's: a 40 MB video must not hold
        # up the next search, and two searches would only fight over the cache.
        self.searches = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store-search")
        self.downloads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store-download")
        self.details = ThreadPoolExecutor(max_workers=2, thread_name_prefix="store-detail")
        self.owned = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store-owned")
        self.chores = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store-facts")
        self.decode = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store-decode")
        self.processes = worker_processes.Cancellation()
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.browser.shutdown()
        self.previews.shutdown()
        self.processes.cancel()
        for lane in (
            self.searches,
            self.details,
            self.owned,
            self.chores,
            self.decode,
            self.downloads,
        ):
            lane.shutdown(wait=False, cancel_futures=True)


class RealStore:
    """`StoreAccess` over `Browser`, for `RealAppState.store_access`."""

    def __init__(
        self,
        backend: StoreBackend,
        host: StoreHost,
        *,
        browser: Callable[[tuple[Path, ...]], Browser] | None = None,
    ) -> None:
        self._backend = backend
        self._host = host
        self._make_browser = browser or (
            lambda roots: Browser(root=roots[0] if roots else None, library_roots=roots)
        )
        self._lanes: _Lanes | None = None
        self._roots: tuple[Path, ...] = tuple(backend.settings.roots)
        # -- searching
        self._search_generation = 0
        self._search_future: Future[SearchResult] | None = None
        #: The latest search's answer is still to come (see `searching`).
        self._awaiting = False
        self._seed = ""
        self._searched = False
        # -- pictures
        self._pictures: OrderedDict[str, Gdk.Texture] = OrderedDict()
        self._picture_generation = 0
        # -- ownership
        self._owned_generation = 0
        self._owned_future: Future[owned.Index] | None = None
        # -- downloads: id -> candidate, in queue order
        self._busy: dict[str, WallpaperCandidate] = {}
        self._then: dict[str, Then] = {}
        self._queued = 0
        self._finished = 0
        #: Downloads of this run of the lane that landed: (path, title).
        self._landed: list[tuple[Path, str]] = []
        #: Where this session downloaded each result (id -> file).
        self._files: dict[str, Path] = {}
        #: Results that landed in a folder the library no longer scans.
        self._elsewhere: dict[str, Path] = {}
        # -- facts from outside the page
        self._nsfw_blocked = ""
        self._steam = False
        self._facts_future: Future[tuple[str, bool]] | None = None
        self._facts_again = False
        host.connect("changed", self._on_changed)

    # -- lifetime -----------------------------------------------------------------
    def _open(self) -> _Lanes:
        if self._lanes is None:
            self._lanes = _Lanes(self._make_browser(self._roots))
        return self._lanes

    def close(self) -> None:
        """Stop every lane and the transport; the window is going away."""
        lanes, self._lanes = self._lanes, None
        if lanes is not None:
            lanes.close()
        self._search_generation += 1
        self._picture_generation += 1
        self._owned_generation += 1
        self._awaiting = False  # whatever was out will never be answered now
        self._search_future = self._owned_future = self._facts_future = None
        self._busy.clear()
        self._then.clear()
        self._queued = self._finished = 0
        self._landed.clear()
        self._pictures.clear()

    def _current(self, lanes: _Lanes) -> bool:
        return lanes is self._lanes and not lanes.closed

    def _on_changed(self, _host: object, topic: str) -> None:
        if topic == "library":
            # A removal or a rescan may have changed what the folders hold.
            self._forget_owned()
        elif topic == "settings":
            roots = tuple(self._backend.settings.roots)
            if roots != self._roots:
                self._roots = roots
                if self._lanes is not None:
                    self._lanes.browser.configure_roots(
                        root=roots[0] if roots else None, library_roots=roots
                    )
                self._elsewhere.clear()
                self._forget_owned()
                self._host.emit_changed("store")

    # -- searching --------------------------------------------------------------------
    def searching(self) -> bool:
        return self._awaiting

    def search_store(self, query: StoreQuery, page: int, done: SearchDone) -> None:
        lanes = self._open()
        self._search_generation += 1
        generation = self._search_generation
        self._awaiting = True
        if page <= 1:
            self._seed = ""  # a fresh search re-rolls a random one
        try:
            request, self._seed = search_request(query, max(1, page), self._seed)
        except ProviderError as error:
            problem = StoreProblem(error.kind, _words(error))
            _later(lambda: self._answer(lanes, generation, problem, done))
            return
        name = query.provider
        browser = lanes.browser

        def work() -> SearchResult:
            if generation != self._search_generation:
                raise CancelledError("a newer search superseded this one")
            return browser.search(name, request)

        if self._search_future is not None:
            self._search_future.cancel()
        future = lanes.searches.submit(work)
        self._search_future = future
        self._searched = True
        future.add_done_callback(partial(self._searched_one, lanes, generation, done))

    def _searched_one(
        self, lanes: _Lanes, generation: int, done: SearchDone, future: Future[SearchResult]
    ) -> None:
        answer: StoreResults | StoreProblem | None
        try:
            result = future.result()
        except CancelledError:
            answer = None  # superseded: the newer search answers instead
        except ProviderError as error:
            answer = StoreProblem(error.kind, _words(error))
        except Exception as error:  # a search must never take the app down
            answer = StoreProblem("failed", f"search failed: {error}")
        else:
            answer = StoreResults(
                items=tuple(CandidateView(candidate) for candidate in result.items),
                has_next=result.has_next,
                total_hint=result.total_hint,
                dropped=result.dropped,
                cached=result.cached,
            )
        if answer is not None:
            found = answer
            _later(lambda: self._answer(lanes, generation, found, done))

    def _answer(
        self,
        lanes: _Lanes,
        generation: int,
        answer: StoreResults | StoreProblem,
        done: SearchDone,
    ) -> None:
        if self._current(lanes) and generation == self._search_generation:
            self._awaiting = False
            done(answer)

    # -- pictures ---------------------------------------------------------------------
    def item_pictures(self, items: Sequence[StoreItemView], done: PictureDone) -> None:
        lanes = self._open()
        self._picture_generation += 1
        generation = self._picture_generation
        by_url: dict[str, list[StoreItemView]] = {}
        wanted: list[tuple[WallpaperCandidate, float]] = []
        for index, item in enumerate(items):
            candidate = _candidate(item)
            url = candidate.thumbnail_url
            if not url:
                continue
            texture = self._pictures.get(url)
            if texture is not None:
                self._pictures.move_to_end(url)
                done(item, texture)
                continue
            if url not in by_url:
                wanted.append((candidate, float(index)))
            by_url.setdefault(url, []).append(item)
        # Only these: anything queued for a page no longer shown is dropped.
        lanes.previews.prioritize(
            wanted, partial(self._picture_bytes, lanes, generation, by_url, done)
        )

    def _picture_bytes(
        self,
        lanes: _Lanes,
        generation: int,
        by_url: dict[str, list[StoreItemView]],
        done: PictureDone,
        candidate: WallpaperCandidate,
        data: bytes,
    ) -> None:
        if not data or not self._current(lanes):
            return
        url = candidate.thumbnail_url
        future = lanes.decode.submit(_decode, data)

        def decoded(finished: Future[Gdk.Texture | None]) -> None:
            try:
                texture = finished.result()
            except Exception:
                return
            if texture is None:
                return
            ready = texture

            def deliver() -> None:
                if not self._current(lanes):
                    return
                self._pictures[url] = ready
                self._pictures.move_to_end(url)
                while len(self._pictures) > PICTURE_CAPACITY:
                    self._pictures.popitem(last=False)
                if generation == self._picture_generation:
                    for item in by_url.get(url, ()):
                        done(item, ready)

            _later(deliver)

        future.add_done_callback(decoded)

    # -- the preview dialog -------------------------------------------------------------
    def item_detail(self, item: StoreItemView, done: DetailDone) -> None:
        lanes = self._open()
        candidate = _candidate(item)
        browser = lanes.browser

        def work() -> StoreDetail:
            try:
                detail = browser.describe(candidate)
            except ProviderError as error:
                return StoreDetail(problem=str(error), page_url=candidate.page_url)
            return _detail_of(detail, _picture_for(browser, detail.preview_url, lanes.processes))

        def finished(future: Future[StoreDetail]) -> None:
            try:
                answer = future.result()
            except CancelledError:
                return
            except Exception as error:  # a worker must never take the app down
                answer = StoreDetail(
                    problem=f"could not load the details: {error}", page_url=candidate.page_url
                )
            _later(lambda: done(answer) if self._current(lanes) else None)

        lanes.details.submit(work).add_done_callback(finished)

    # -- what the library holds -----------------------------------------------------------
    def in_library(self, item: StoreItemView) -> bool:
        lanes = self._lanes
        if lanes is None:
            return False
        ready = lanes.browser.cached_owned
        if ready is None:
            self._refresh_owned()
            return False
        return ready.holds(_candidate(item))

    def _forget_owned(self) -> None:
        self._owned_generation += 1
        lanes = self._lanes
        if lanes is not None:
            lanes.browser.forget_owned()
            if self._searched:
                self._refresh_owned()

    def _refresh_owned(self) -> None:
        lanes = self._lanes
        if lanes is None or lanes.closed or self._owned_future is not None:
            return
        generation = self._owned_generation
        browser = lanes.browser
        future = lanes.owned.submit(lambda: browser.owned)
        self._owned_future = future

        def finished(done: Future[owned.Index]) -> None:
            _later(lambda: self._owned_read(lanes, generation, done))

        future.add_done_callback(finished)

    def _owned_read(self, lanes: _Lanes, generation: int, future: Future[owned.Index]) -> None:
        if self._owned_future is future:
            self._owned_future = None
        if not self._current(lanes):
            return
        try:
            future.result()
        except Exception:
            # Ownership is a display hint, never download authority; a later
            # search or rescan retries an unreadable drive.
            return
        if generation != self._owned_generation:
            self._refresh_owned()
            return
        self._host.emit_changed("store")

    def _library_path(self, item: StoreItemView) -> Path | None:
        """The file the library holds for ``item``, as far as is known on this thread."""
        known = self._files.get(item.id)
        if known is not None:
            return known
        lanes = self._lanes
        ready = lanes.browser.cached_owned if lanes is not None else None
        return ready.path_for(_candidate(item)) if ready is not None else None

    # -- downloading --------------------------------------------------------------------
    @property
    def downloads(self) -> Downloads:
        text = ""
        if self._finished < self._queued:
            text = f"Downloading {self._finished + 1} of {self._queued}"
        return Downloads(busy=tuple(self._busy), text=text)

    def download_blocked(self) -> str:
        if not self._backend.settings.roots:
            return NO_FOLDER
        return ""

    @property
    def download_destination(self) -> str:
        roots = self._backend.settings.roots
        return _home_relative(roots[0]) if roots else ""

    def download(self, item: StoreItemView, *, variant: str = "", then: Then = "") -> None:
        candidate = _candidate(item)
        key = item.id
        if key in self._busy:
            if then:
                self._then[key] = then  # "& apply" on one that is already coming
            else:
                self._backend.window_report(f"{item.title} is already downloading")
            return
        if not self._backend.require_authoring_ready():
            return
        blocked = self.download_blocked()
        if blocked:
            # Said with the way to fix it: Settings' Library group, one press away.
            settings = ("Settings", lambda: self._host.navigate(SETTINGS_LIBRARY))
            self._host.toast(blocked, action=settings)
            return
        lanes = self._open()
        self._busy[key] = candidate
        if then:
            self._then[key] = then
        self._elsewhere.pop(key, None)
        # Counted here, the one place a download starts: a card pressed while
        # a batch runs joins that batch's "N of M".
        self._queued += 1
        browser = lanes.browser
        future = lanes.downloads.submit(lambda: browser.download(candidate, variant=variant))

        def finished(done: Future[Downloaded]) -> None:
            _later(lambda: self._downloaded(lanes, item, done))

        future.add_done_callback(finished)
        self._host.emit_changed("store")

    def _downloaded(self, lanes: _Lanes, item: StoreItemView, future: Future[Downloaded]) -> None:
        if not self._current(lanes):
            return
        key = item.id
        self._busy.pop(key, None)
        then = self._then.pop(key, "")
        # Counted whether it worked or not: a batch of five with one failure
        # has still finished.
        self._finished += 1
        try:
            done = future.result()
        except CancelledError:
            done = None
        except ProviderError as error:
            # Words for "busy" (another request holds this download) and
            # "conflict" (the library already has it) come from the claim.
            self._backend.window_report(str(error))
            if error.kind == "conflict":
                self._forget_owned()
            done = None
        except Exception as error:  # a worker must never take the app down
            self._backend.window_report(f"download failed: {error}")
            done = None
        if done is not None:
            self._landed_one(item, done, then)
        if self._finished >= self._queued:
            self._lane_drained()
        self._host.emit_changed("store")

    def _landed_one(self, item: StoreItemView, done: Downloaded, then: Then) -> None:
        path = done.result.path
        if not done.root_current:
            # Installed, but in the folder Settings replaced while it ran.
            self._elsewhere[item.id] = done.root
            self._host.toast(
                f"{item.title}: {done.describe()}. It is not in your library, which no longer "
                "scans that folder"
            )
            return
        self._files[item.id] = path
        if then:
            self._when_in_library(path, item.title, then)
            return
        self._landed.append((path, item.title))
        # The file is in a library folder, and in the library once it is looked at.
        self._backend.refresh_library()

    def _lane_drained(self) -> None:
        landed, self._landed = self._landed, []
        self._queued = self._finished = 0
        if len(landed) == 1:
            path, title = landed[0]
            self._host.toast(
                f"“{title}” is in your library",
                action=("Apply", lambda: self._when_in_library(path, title, "apply")),
            )
        elif landed:
            self._host.toast(
                f"{len(landed)} wallpapers are in your library",
                action=("Show", lambda: self._host.navigate("library")),
            )

    def _when_in_library(self, path: Path, title: str, then: Then) -> None:
        """Apply or show ``path`` once the library holds it, rescanning when it must."""
        wid = str(path)
        host = self._host

        def act() -> None:
            if then == "show":
                host.navigate(f"library:{wid}")
            else:
                host.apply(wid, host.scope)

        if host.has_wallpaper(wid):
            act()
            return
        lanes = self._lanes

        def finished(installed: bool) -> None:
            if lanes is None or not self._current(lanes):
                return
            if host.has_wallpaper(wid):
                act()
            elif installed:
                self._backend.window_report(
                    f"{title} is in {_home_relative(path.parent)}, but the library scan did "
                    "not pick it up"
                )
            else:
                self._backend.window_report(
                    f"{title} was downloaded, but the library could not be rescanned, "
                    "so it was not " + ("shown" if then == "show" else "applied")
                )

        self._backend.refresh_library(finished=finished)

    def apply_item_blocked(self, item: StoreItemView) -> str:
        elsewhere = self._elsewhere.get(item.id)
        if elsewhere is not None:
            return f"Downloaded to {_home_relative(elsewhere)}, which is no longer a library folder"
        path = self._library_path(item)
        if path is not None and self._host.has_wallpaper(str(path)):
            return self._host.apply_blocked(str(path))
        return ""

    def apply_item(self, item: StoreItemView) -> None:
        self._act_on(item, "apply")

    def show_item(self, item: StoreItemView) -> None:
        self._act_on(item, "show")

    def _act_on(self, item: StoreItemView, then: Then) -> None:
        blocked = self.apply_item_blocked(item)
        if blocked:
            self._backend.window_report(blocked)
            return
        path = self._library_path(item)
        if path is None:
            self._backend.window_report(f"{item.title} is not in your library")
            return
        self._when_in_library(path, item.title, then)

    # -- the API key and the Workshop ------------------------------------------------------
    def refresh_facts(self) -> None:
        lanes = self._open()
        if self._facts_future is not None:
            self._facts_again = True
            return

        def read() -> tuple[str, bool]:
            # Both read the disk: the key file (and its directory), Steam's roots.
            limitations = next(
                (info.limitations for info in registry.describe() if info.name == WALLHAVEN),
                (),
            )
            return (limitations[0] if limitations else ""), workshop.is_steam_installed()

        future = lanes.chores.submit(read)
        self._facts_future = future

        def finished(done: Future[tuple[str, bool]]) -> None:
            _later(lambda: self._facts_read(lanes, done))

        future.add_done_callback(finished)

    def _facts_read(self, lanes: _Lanes, future: Future[tuple[str, bool]]) -> None:
        if self._facts_future is future:
            self._facts_future = None
        if not self._current(lanes):
            return
        try:
            nsfw, steam = future.result()
        except Exception:
            return
        changed = (nsfw, steam) != (self._nsfw_blocked, self._steam)
        self._nsfw_blocked, self._steam = nsfw, steam
        if self._facts_again:
            self._facts_again = False
            self.refresh_facts()
        if changed:
            self._host.emit_changed("store")

    @property
    def nsfw_blocked(self) -> str:
        return self._nsfw_blocked

    @property
    def has_workshop(self) -> bool:
        return self._steam

    def open_workshop(self) -> None:
        """Steam's Workshop, which is not a provider and cannot be one.

        Wallpaper Engine's catalogue is Steam's, and subscribing has no
        unauthenticated API; Steam handles the account and the download.
        """
        lanes = self._open()
        future = lanes.chores.submit(workshop.links)

        def finished(done: Future[tuple[str, str]]) -> None:
            def launch() -> None:
                if not self._current(lanes):
                    return
                try:
                    preferred, fallback = done.result()
                except Exception:
                    return
                if not preferred:
                    return
                # No callback: whether Steam came to the front is Steam's business.
                Gtk.UriLauncher(uri=preferred).launch(None, None, None, None)
                self._host.toast(f"Opening the Wallpaper Engine Workshop ({fallback})")

            _later(launch)

        future.add_done_callback(finished)

    def open_site(self, item: StoreItemView) -> None:
        url = _candidate(item).page_url
        if url.startswith("https://"):
            Gtk.UriLauncher(uri=url).launch(None, None, None, None)
            site = PROVIDER[item.provider].site if item.provider in PROVIDER else url
            self._host.toast(f"Opening {site} in your browser")


def _picture_for(
    browser: Browser, url: str, processes: worker_processes.Cancellation
) -> Gdk.Texture | None:
    """The detail's larger picture, on a worker: cache, fetch, WebP via ffmpeg, decode.

    Optional: a picture that will not come must not lose the facts.
    """
    if not url:
        return None
    try:
        cached = thumbnails.lookup_preview(url)
        if cached:
            return _decode(cached)
        picture = browser.preview(url)
        displayable = thumbnails.to_displayable(picture, processes=processes) if picture else b""
        thumbnails.store_preview(url, displayable)
        return _decode(displayable)
    except Exception:
        return None


def _detail_of(detail: CandidateDetail, picture: Gdk.Texture | None) -> StoreDetail:
    return StoreDetail(
        facts=tuple((fact.label, fact.value) for fact in detail.facts),
        tags=detail.tags,
        colours=detail.colours,
        variants=detail.variants if len(detail.variants) > 1 else (),
        picture=picture,
        page_url=detail.candidate.page_url,
    )
