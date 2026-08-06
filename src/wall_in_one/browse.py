"""Searching and downloading, in terms the window can drive.

The providers know how to talk to two websites. This knows which of them is
selected, where a download lands, and that a finished download leaves the
library stale. GTK-free like `session`, so the whole flow is testable without
a display and -- because every provider shares one injected HTTP client --
without a network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one.library import scan
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
    ) -> None:
        # One transport for every provider: connection reuse, and one seam for
        # a test to replace.
        self._client = client if client is not None else http.UrllibClient()
        self._root = root
        self._providers: dict[str, Provider] = {}

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
        return self.provider(name).search(query)

    def download(self, candidate: WallpaperCandidate, *, variant: str = "") -> Downloaded:
        """Fetch ``candidate`` into the library.

        The caller is expected to rescan afterwards: the new file is `MANAGED`,
        which the scanner works out from the marker and sidecar the provider
        wrote, not from anything we tell it.
        """
        root = self.download_root()
        result = self.provider(candidate.provider).download(candidate, root, variant=variant)
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
