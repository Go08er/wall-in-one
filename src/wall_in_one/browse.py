"""Searching and downloading, in terms the window can drive.

The providers know how to talk to two websites. This knows which of them is
selected, where a download lands, and that a finished download leaves the
library stale. GTK-free like `session`, so the whole flow is testable without
a display and -- because every provider shares one injected HTTP client --
without a network.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from wall_in_one.library import owned, scan
from wall_in_one.providers import http, registry
from wall_in_one.providers.base import (
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


def new_seed() -> str:
    """A fresh Wallhaven random-search seed.

    `secrets` rather than `random` is not about secrecy -- it is that this is
    the module-level generator, and seeding a wallpaper search should not
    disturb, or be disturbed by, anything else that draws from it.
    """
    return "".join(secrets.choice(SEED_CHARACTERS) for _ in range(SEED_LENGTH))


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

    def describe(self) -> str:
        megabytes = self.result.size / (1024 * 1024)
        return f"downloaded {self.result.path.name} ({megabytes:.1f} MB)"


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
        self._library_roots = tuple(library_roots)
        self._providers: dict[str, Provider] = {}
        self._owned: owned.Index | None = None

    # -- what we already have ----------------------------------------------

    @property
    def owned(self) -> owned.Index:
        """Which results are already in the library.

        Built on first use and then kept, because walking every root is
        filesystem work and the answer is wanted once per card.
        """
        if self._owned is None:
            self._owned = owned.read(self._library_roots or scan.default_roots())
        return self._owned

    def forget_owned(self) -> None:
        """Drop the index, so the next question re-reads the disk.

        For the cases this object cannot see: a wallpaper removed through the
        library window, or the roots being reconfigured underneath it.
        """
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

    # -- where downloads land ---------------------------------------------

    def download_root(self) -> Path:
        """The directory downloads are installed under.

        Noctalia's own wallpaper directory by default, so a download appears in
        the library -- and in Noctalia -- without the user configuring the same
        path twice. The provider adds its own `Wall-in-One/<Provider>` beneath
        this; nothing is written directly here.
        """
        if self._root is not None:
            return self._root
        roots = scan.default_roots()
        if roots:
            return roots[0]
        raise ProviderError(
            "no-root",
            "no wallpaper directory is configured, so there is nowhere to download to",
        )

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

    def download(self, candidate: WallpaperCandidate, *, variant: str = "") -> Downloaded:
        """Fetch ``candidate`` into the library.

        The caller is expected to rescan afterwards: the new file is `MANAGED`,
        which the scanner works out from the marker and sidecar the provider
        wrote, not from anything we tell it.
        """
        root = self.download_root()
        result = self.provider(candidate.provider).download(candidate, root, variant=variant)
        # Record it rather than invalidating the index: this process knows both
        # the candidate and where it landed, so re-walking every root to learn
        # one fact it just created would be work for nothing.
        self.owned.add(candidate, result.path)
        return Downloaded(result=result, root=root)

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


def usable_names(infos: Sequence[registry.ProviderInfo]) -> tuple[str, ...]:
    return tuple(info.name for info in infos if info.usable)
