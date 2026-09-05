"""Searching and downloading, in terms the window can drive.

The providers know how to talk to two websites. This knows which of them is
selected, where a download lands, and that a finished download leaves the
library stale. GTK-free like `session`, so the whole flow is testable without
a display and -- because every provider shares one injected HTTP client --
without a network.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import secrets
import stat
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, cast

from wall_in_one import paths
from wall_in_one.library import owned
from wall_in_one.library.owned import Index as OwnedIndex
from wall_in_one.providers import http, registry
from wall_in_one.providers.base import (
    CandidateDetail,
    DownloadResult,
    Provider,
    ProviderError,
    SearchQuery,
    SearchResult,
    WallpaperCandidate,
)

#: Thumbnails come from the provider's CDN, so they are bounded harder than a
#: search response: anything larger is not a preview image.
MAX_THUMBNAIL_BYTES: Final = 4 * 1024 * 1024

#: A preview nobody is waiting on should not hold a worker for long.
THUMBNAIL_TIMEOUT: Final = 10.0

#: The detail view's picture is full width rather than a 300-pixel card, so it
#: gets its own ceiling. Still far below the wallpaper itself: opening a detail
#: view must not pull down 20 MB to show somebody something they have not asked
#: to download.
MAX_PREVIEW_BYTES: Final = 16 * 1024 * 1024

#: Longer than a thumbnail's, because somebody *is* waiting on this one.
PREVIEW_TIMEOUT: Final = 20.0

#: Named here rather than imported from the provider modules, so that building
#: a query does not drag two websites' worth of parsing in behind it.
WALLHAVEN: Final = "wallhaven"
MOTIONBGS: Final = "motionbgs"

#: Wallhaven sortings that make `top_range` mean anything. Sending it with any
#: other sorting is ignored rather than refused, but sending it anyway would
#: put a value in the URL that had no effect on the results.
RANGED_SORTINGS: Final[frozenset[str]] = frozenset({"toplist"})

#: The sorting that needs a seed. Without one, Wallhaven re-rolls between
#: requests, so page two of a random search overlaps page one -- which is
#: survivable while paging is two buttons and is not once results load as the
#: user scrolls.
SEEDED_SORTING: Final = "random"

#: Wallhaven's seed alphabet and length, per its own validation.
SEED_CHARACTERS: Final = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
SEED_LENGTH: Final = 6

# Candidate claims live in the per-login runtime directory.  The files are
# deliberately never unlinked: unlinking a flock file while another process
# has opened its inode lets a third process lock a replacement beside it.  The
# directory is normally session-cleaned; without XDG_RUNTIME_DIR, the paths
# module safely falls back to persistent cache and these tiny files remain.
# Names are fixed hashes rather than remote-controlled identifiers.
DOWNLOAD_CLAIM_DIRECTORY: Final = f"{paths.APP_ID}-download-claims"


def new_seed() -> str:
    """A fresh Wallhaven random-search seed.

    `secrets` rather than `random` is not about secrecy -- it is that this is
    the module-level generator, and seeding a wallpaper search should not
    disturb, or be disturbed by, anything else that draws from it.
    """
    return "".join(secrets.choice(SEED_CHARACTERS) for _ in range(SEED_LENGTH))


def source_page_url(provider: str, identifier: str) -> str:
    """Canonical provenance page for an identifier-only control request."""
    if provider == WALLHAVEN:
        return f"https://wallhaven.cc/w/{identifier}"
    if provider == MOTIONBGS:
        # The predecessor sidecar's exact spelling. ``owned`` also accepts the
        # slashless form emitted by intermediate identifier-only callers.
        return f"https://motionbgs.com/{identifier}/"
    return ""


def _claim_path(root: Path, provider: str, identifier: str) -> Path:
    # Resolve the destination before a potentially minutes-long network call,
    # but do not make it part of the lease identity. Provider/id is the
    # library identity; the selected destination is merely where this attempt
    # installs it. A root switch while one transfer is live must not let a
    # second GUI/control Browser contact the provider for the same item.
    try:
        root.resolve(strict=True)
    except OSError as error:
        raise ProviderError("invalid-path", f"download root is unusable: {error}") from error
    material = b"\0".join(
        (
            provider.casefold().encode("utf-8", "surrogatepass"),
            identifier.encode("utf-8", "surrogatepass"),
        )
    )
    identity = hashlib.sha256(material).hexdigest()
    directory = paths.runtime_dir() / DOWNLOAD_CLAIM_DIRECTORY
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError(f"download claim path is not a real directory: {directory}")
        if info.st_uid != os.getuid():
            raise OSError(f"download claim path is not owned by this user: {directory}")
        os.chmod(directory, 0o700, follow_symlinks=False)
    except OSError as error:
        raise ProviderError("local-io", f"cannot prepare download claims: {error}") from error
    return directory / f"{identity}.lock"


@contextmanager
def _download_claim(root: Path, provider: str, identifier: str) -> Iterator[None]:
    """Take one nonblocking, user/provider/id-scoped cross-process lease."""
    lock_path = _claim_path(root, provider, identifier)
    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
        ):
            raise OSError(f"download claim {lock_path} is not a private regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as error:
            raise ProviderError(
                "busy",
                f"{provider} {identifier} is already downloading in another request",
            ) from error
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(f"download claim {lock_path} changed while it was being locked")
        yield
    except ProviderError:
        raise
    except OSError as error:
        raise ProviderError("local-io", f"cannot safely claim this download: {error}") from error
    finally:
        if descriptor is not None:
            if locked:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class Filters:
    """What the browse controls are asking for, with no toolkit in sight.

    The dialog reads its widgets into one of these and asks for a `SearchQuery`
    back. That split is what lets the awkward parts be tested: a seed that is
    only legal with one sorting, a range that only means something with
    another, and two providers whose options have nothing in common.

    Every field defaults to what the provider would do anyway, so a `Filters()`
    is "search for this, no opinions".
    """

    text: str = ""

    # -- Wallhaven ---------------------------------------------------------
    sorting: str = "date_added"
    order: str = "desc"
    categories: str = "111"
    purity: str = "100"
    #: A minimum resolution, `1920x1080`.
    atleast: str = ""
    #: Aspect ratios, `16x9,16x10`.
    ratios: str = ""
    #: One of Wallhaven's documented palette entries, without the `#`.
    colour: str = ""
    top_range: str = "1M"
    seed: str = ""

    # -- MotionBGS ---------------------------------------------------------
    mode: str = "latest"
    genre: str = ""

    @property
    def needs_seed(self) -> bool:
        return self.sorting == SEEDED_SORTING

    @property
    def uses_range(self) -> bool:
        return self.sorting in RANGED_SORTINGS

    def seeded(self) -> Filters:
        """The same filters with a seed, generated if the sorting wants one.

        Returned rather than mutated because these are what a search is
        reproducible from: a caller that holds one and gets the same results
        twice is relying on it not having changed underneath.
        """
        if not self.needs_seed:
            # Dropping it matters: a stale seed left over from a random search
            # would be refused outright by the next sorted one.
            return self if not self.seed else replace(self, seed="")
        if self.seed:
            return self
        return replace(self, seed=new_seed())

    def wallhaven_options(self) -> dict[str, str]:
        """The option mapping Wallhaven's own validator expects.

        Conditional rather than exhaustive. `seed` is *refused* outside random
        sorting rather than ignored, so sending it always would turn a change
        of sorting into an error message.
        """
        options = {
            "sorting": self.sorting,
            "order": self.order,
            "categories": self.categories,
            "purity": self.purity,
        }
        if self.atleast:
            options["atleast"] = self.atleast
        if self.ratios:
            options["ratios"] = self.ratios
        if self.colour:
            options["colors"] = self.colour
        if self.uses_range:
            options["top_range"] = self.top_range
        if self.needs_seed and self.seed:
            options["seed"] = self.seed
        return options

    def motionbgs_options(self) -> dict[str, str]:
        """MotionBGS's, which are a different shape entirely.

        Typing a query means searching whatever the browse mode says, because
        MotionBGS rejects a query and a browse mode together and the user
        typing something is the less ambiguous of the two signals.
        """
        mode = "search" if self.text.strip() else self.mode
        options = {"mode": mode}
        if mode == "genre":
            options["genre"] = self.genre.strip()
        return options

    def to_query(self, provider: str, page: int = 1) -> SearchQuery:
        options = (
            self.wallhaven_options()
            if provider == WALLHAVEN
            else self.motionbgs_options()
            if provider == MOTIONBGS
            else {}
        )
        return SearchQuery(text=self.text.strip(), page=page, options=options)


@dataclass(frozen=True, slots=True)
class Downloaded:
    """A finished download, and what to say about it."""

    result: DownloadResult
    #: Where the library must be rescanned from for the file to appear.
    root: Path
    #: False when Settings changed folders while the transfer was running.
    #: The file is still safely installed, but the current library scan will
    #: not find it and the UI must say where it went.
    root_current: bool = True

    def describe(self) -> str:
        megabytes = self.result.size / (1024 * 1024)
        summary = f"downloaded {self.result.path.name} ({megabytes:.1f} MB)"
        if not self.root_current:
            summary += f" to the previous library folder {self.root} (folders changed mid-download)"
        return summary


class Browser:
    """Provider selection, search, and download-into-the-library."""

    def __init__(
        self,
        *,
        client: http.Client | None = None,
        root: Path | None = None,
        library_roots: Sequence[Path] = (),
    ) -> None:
        # One transport for every provider: connection reuse, and one seam for
        # a test to replace.
        self._client = client if client is not None else http.UrllibClient()
        self._root = root
        # A caller which only supplies the download destination still expects
        # repeat-download protection in that destination.  The GUI passes all
        # configured roots explicitly so a copy in any one of them counts.
        self._library_roots = tuple(library_roots) or ((root,) if root is not None else ())
        self._roots_generation = 0
        self._owned_generation = 0
        self._roots_lock = threading.RLock()
        self._providers: dict[str, Provider] = {}
        self._owned: owned.Index | None = None

    # -- what we already have ----------------------------------------------

    @property
    def owned(self) -> owned.Index:
        """Which results are already in the library.

        Built on first use and then kept, because walking every root is
        filesystem work and the answer is wanted once per card.
        """
        while True:
            with self._roots_lock:
                if self._owned is not None:
                    return self._owned
                generation = self._owned_generation
                roots = self._library_roots
            # Walking a large library can take long enough for somebody to
            # change the configured roots. Do that work outside the lock, then
            # publish it only if it still describes the current settings.
            built = owned.read(roots)
            with self._roots_lock:
                if generation != self._owned_generation:
                    continue
                if self._owned is None:
                    self._owned = built
                return self._owned

    @property
    def cached_owned(self) -> OwnedIndex | None:
        """Return only a ready snapshot; never walk the filesystem for a widget."""
        with self._roots_lock:
            return self._owned

    def forget_owned(self) -> None:
        """Drop the index, so the next question re-reads the disk.

        For the cases this object cannot see: a wallpaper removed through the
        library window, or the roots being reconfigured underneath it.
        """
        with self._roots_lock:
            self._owned_generation += 1
            self._owned = None

    def configure_roots(self, *, root: Path | None, library_roots: Sequence[Path] = ()) -> None:
        """Adopt changed library folders without rebuilding provider clients.

        Search providers deliberately live for the whole browse session: they
        own bounded response caches and HTTP connection reuse.  Download and
        ownership roots are different state, however, and settings may change
        while the Browse page is alive.  Updating those in place preserves the
        useful provider caches while making the next search and download use
        the folders the Settings page currently shows.
        """
        with self._roots_lock:
            self._root = root
            self._library_roots = tuple(library_roots) or ((root,) if root is not None else ())
            self._roots_generation += 1
            self._owned_generation += 1
            self._owned = None

    # -- providers -------------------------------------------------------

    @property
    def available(self) -> tuple[registry.ProviderInfo, ...]:
        """Every provider and what it can do right now. Never raises."""
        return registry.describe()

    def provider(self, name: str) -> Provider:
        """Build ``name`` once and keep it, so its search cache survives."""
        existing = self._providers.get(name)
        if existing is not None:
            return existing
        built = registry.build(name, client=self._client)
        self._providers[name] = built
        return built

    def clear_caches(self) -> None:
        for provider in self._providers.values():
            provider.clear_cache()

    def shutdown(self) -> None:
        """Cancel the real transport when this browser surface is finished.

        Injected test clients intentionally need only the two-method ``Client``
        protocol.  The production ``UrllibClient`` additionally owns active
        sockets, so close it when present without widening that provider seam.
        """
        close = cast("Callable[[], None] | None", getattr(self._client, "close", None))
        if close is not None:
            close()

    # -- where downloads land ---------------------------------------------

    def download_root(self) -> Path:
        """The directory downloads are installed under.

        It must be an explicit user choice. The first-run prompt offers
        Noctalia's wallpaper directory, but the Browser never adopts that path
        on its own. The provider adds its own `Wall-in-One/<Provider>` beneath
        the chosen root; nothing is written directly here.
        """
        with self._roots_lock:
            configured = self._root
        if configured is not None:
            return configured
        raise ProviderError(
            "no-root",
            "no library folder is configured; choose one in Settings before downloading",
        )

    def _download_roots_snapshot(self) -> tuple[int, Path, tuple[Path, ...]]:
        """Capture one coherent destination/provenance view for a transfer.

        Settings can replace both the selected download destination and the
        complete library-root inventory while a Browse worker is active.  The
        destination, generation and roots must consequently come from the
        same critical section: mixing an old roots tuple with a new destination
        could miss an existing provider identity in the new folder and perform
        a duplicate network transfer.
        """
        with self._roots_lock:
            root = self._root
            if root is None:
                raise ProviderError(
                    "no-root",
                    "no library folder is configured; choose one in Settings before downloading",
                )
            roots = self._library_roots
            # A malformed/injected caller must not make the actual destination
            # invisible to the under-claim provenance check.
            if root not in roots:
                roots = (*roots, root)
            return self._roots_generation, root, roots

    # -- verbs -------------------------------------------------------------

    def search(self, name: str, query: SearchQuery) -> SearchResult:
        """Ask ``name`` for a page, warming the owned index on the way past.

        The warming is deliberate and belongs here rather than in the caller.
        A search runs on a worker; the cards it produces are built on the UI
        thread, and each of them wants to know whether the library already
        holds that wallpaper. Reading the index lazily from the card would put
        a filesystem walk on the main loop the first time a search returns.
        """
        result = self.provider(name).search(query)
        _ = self.owned
        return result

    def describe(self, candidate: WallpaperCandidate) -> CandidateDetail:
        """Everything the provider will say about one result.

        A second request, so it belongs on a worker like `search` does. The
        providers cache it, which is what makes re-opening the same wallpaper
        free rather than another round trip to the site.
        """
        return self.provider(candidate.provider).describe(candidate)

    def download(self, candidate: WallpaperCandidate, *, variant: str = "") -> Downloaded:
        """Fetch ``candidate`` into the library.

        The caller is expected to rescan afterwards: the new file is `MANAGED`,
        which the scanner works out from the marker and sidecar the provider
        wrote, not from anything we tell it.
        """
        generation, root, roots = self._download_roots_snapshot()
        # The claim excludes GUI/ctl and second-process races without holding
        # the shared authoring-state mutation gate across a minutes-long
        # transfer.  Re-read provenance *after* taking it: a cached Index is a
        # display optimisation, never permission to download twice.
        with _download_claim(root, candidate.provider, candidate.identifier):
            current_owned = owned.read(roots)
            if current_owned.holds(candidate):
                with self._roots_lock:
                    if generation == self._roots_generation:
                        self._owned = current_owned
                existing = current_owned.path_for(candidate)
                raise ProviderError(
                    "conflict",
                    f"{candidate.title or candidate.identifier} is already in the library"
                    + (f" at {existing}" if existing is not None else ""),
                )
            result = self.provider(candidate.provider).download(
                candidate,
                root,
                variant=variant,
            )
        # Record it rather than invalidating the index: this process knows both
        # the candidate and where it landed, so re-walking every root to learn
        # one fact it just created would be work for nothing. A download may
        # outlive a Settings root change, though; never publish that old path
        # into the replacement roots' index. If the index was not warm yet,
        # leave it lazy rather than building it under this completion path.
        with self._roots_lock:
            root_current = generation == self._roots_generation
            if root_current and self._owned is not None:
                self._owned.add(candidate, result.path)
        return Downloaded(result=result, root=root, root_current=root_current)

    def thumbnail(self, candidate: WallpaperCandidate) -> bytes:
        """The provider's preview image, for a card in the browse dialog.

        Returns empty when the provider offered no thumbnail -- that is a card
        without a picture, not an error.
        """
        if not candidate.thumbnail_url:
            return b""
        response = self._client.fetch(
            http.Request(
                url=http.require_https(candidate.thumbnail_url),
                accept="image/*",
                timeout=THUMBNAIL_TIMEOUT,
                max_bytes=MAX_THUMBNAIL_BYTES,
            )
        )
        if response.status != 200 or not response.content_type.startswith("image/"):
            # A redirect or an error page is not a preview. Say nothing and let
            # the card fall back to its title.
            return b""
        return response.body

    def preview(self, url: str) -> bytes:
        """A larger preview for the detail view, by URL.

        Separate from `thumbnail` because the ceiling is different rather than
        because the fetch is: a detail preview is a full-width picture and a
        card thumbnail is 300 pixels wide, and one ceiling for both would
        either truncate the first or leave the second unbounded in practice.

        Still nowhere near the size of the wallpaper itself. Opening a detail
        view must not quietly pull 20 MB down to show somebody a picture they
        have not asked to download.
        """
        if not url:
            return b""
        response = self._client.fetch(
            http.Request(
                url=http.require_https(url),
                accept="image/*",
                timeout=PREVIEW_TIMEOUT,
                max_bytes=MAX_PREVIEW_BYTES,
            )
        )
        if response.status != 200 or not response.content_type.startswith("image/"):
            return b""
        return response.body


def usable_names(infos: Sequence[registry.ProviderInfo]) -> tuple[str, ...]:
    return tuple(info.name for info in infos if info.usable)
