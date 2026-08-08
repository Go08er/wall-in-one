"""What a wallpaper provider is, and what it hands back.

Providers are ordinary objects the app calls directly. The implementation this
package was lifted from drove them through a request/response-file RPC protocol
with guard files, nonces and advisory locks, because a Luau plugin had no other
way to reach Python. None of that survives the move into a single process; what
survives is everything that was learned about the two remote sites.

Everything crossing this boundary is a frozen dataclass. A provider's answer is
untrusted JSON or untrusted HTML, so it is parsed into these types at the edge
and never handed onwards as a raw document.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from wall_in_one.library.model import Kind

#: Ceiling on results returned from one search, whatever the remote says.
MAX_RESULTS: Final = 48


class ProviderError(Exception):
    """A provider could not answer, with a machine-readable reason.

    ``kind`` is what the UI branches on -- `credential` wants a settings
    dialogue, `rate-limit` wants a retry, `site-markup` wants a bug report --
    while the message is for a human to read. The kinds in use:

    ``challenge``, ``conflict``, ``content-type``, ``credential``,
    ``dimensions``, ``http``, ``invalid-path``, ``invalid-request``,
    ``invalid-url``, ``local-io``, ``no-root``, ``rate-limit``, ``redirects``,
    ``remote``, ``response``, ``site-markup``, ``size-limit``,
    ``size-mismatch``, ``timeout``, ``transport``, ``unknown-provider``,
    ``validation``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """One page of one search, in terms every provider understands.

    ``options`` is the escape hatch for the parts that differ -- MotionBGS has
    browse modes, Wallhaven has a dozen filters. It is `str`-to-`str` because
    that is what a form produces, and because each provider validates its own
    keys and rejects the rest rather than silently ignoring a typo.
    """

    text: str = ""
    page: int = 1
    options: Mapping[str, str] = field(default_factory=dict)

    def option(self, name: str, default: str = "") -> str:
        value = self.options.get(name, default)
        return value if isinstance(value, str) else default


@dataclass(frozen=True, slots=True)
class WallpaperCandidate:
    """One wallpaper offered by a provider, before anything is downloaded."""

    provider: str
    #: Stable within the provider: a MotionBGS slug, a Wallhaven six-character id.
    identifier: str
    title: str
    kind: Kind
    page_url: str
    thumbnail_url: str = ""
    #: A short video loop, where the provider offers one. MotionBGS only.
    preview_url: str = ""
    resolution: str = ""
    #: Which download to take when the provider offers several. MotionBGS
    #: quality (`hd` / `4k`); empty where there is only one file.
    variant: str = ""
    size_hint: int = 0


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A page of candidates plus what the provider said about the rest."""

    provider: str
    #: The URL actually requested, so a failure can be reproduced by hand.
    query_url: str
    items: tuple[WallpaperCandidate, ...]
    page: int = 1
    has_previous: bool = False
    has_next: bool = False
    #: Best guess at the total, or 0 when the provider does not say.
    total_hint: int = 0
    #: Results the provider returned that we refused to normalise. Surfaced
    #: rather than hidden: a sudden jump means the remote schema moved.
    dropped: int = 0
    cached: bool = False

    def __len__(self) -> int:
        return len(self.items)


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """A wallpaper installed into a directory this app manages."""

    provider: str
    identifier: str
    path: Path
    #: The per-file sidecar. Together with the directory marker it is what
    #: makes `library.scan` call the file `Ownership.MANAGED`.
    sidecar: Path
    marker: Path
    kind: Kind
    size: int
    source_url: str
    download_url: str
    sha256: str
    downloaded_at: str


@dataclass(frozen=True, slots=True)
class Fact:
    """One labelled thing a provider knows about a wallpaper."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class CandidateDetail:
    """Everything a provider will say about one wallpaper.

    Label-and-value pairs rather than a field per property, because the two
    providers know almost disjoint things -- Wallhaven has views, favourites
    and a category; MotionBGS has a duration and two download qualities -- and
    a struct wide enough for both would be mostly empty whichever one filled
    it. The dialog renders whatever it is handed and needs to know nothing
    about either site.

    The named fields are the ones the window does more than print: a preview
    to show, tags to make searchable, colours to swatch, variants to choose
    between.
    """

    candidate: WallpaperCandidate
    #: A larger preview than the card's thumbnail. Falls back to the thumbnail.
    preview_url: str = ""
    facts: tuple[Fact, ...] = ()
    tags: tuple[str, ...] = ()
    #: Palette entries, six hex digits without the hash.
    colours: tuple[str, ...] = ()
    #: What can be downloaded when there is a choice -- MotionBGS `hd`/`4k`.
    #: Empty where the provider offers exactly one file.
    variants: tuple[str, ...] = ()


def human_bytes(size: int) -> str:
    """A file size a person can read, for a `Fact` value.

    Binary units, because that is what a file manager shows for the same file
    and disagreeing with it would only raise the question of which was right.
    """
    if size <= 0:
        return ""
    step = 1024.0
    amount = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < step or unit == "GiB":
            precision = 0 if unit == "B" or amount >= 100 else 1
            return f"{amount:.{precision}f} {unit}"
        amount /= step
    return f"{amount:.1f} GiB"


@runtime_checkable
class Provider(Protocol):
    """The surface the app drives. Deliberately five verbs wide."""

    @property
    def name(self) -> str:
        """Stable identifier used in settings and by the registry."""

    @property
    def title(self) -> str:
        """What to show a person."""

    @property
    def media_kind(self) -> Kind:
        """What this provider serves: stills or videos."""

    def search(self, query: SearchQuery) -> SearchResult: ...

    def describe(self, candidate: WallpaperCandidate) -> CandidateDetail:
        """Everything known about one result, for a detail view.

        A second request in general: a search response is deliberately thin,
        and tags, colours and an exact file size are what the per-wallpaper
        endpoint is for. Providers cache it, so re-opening the same wallpaper
        does not ask twice.
        """
        ...

    def download(
        self, candidate: WallpaperCandidate, root: Path, *, variant: str = ""
    ) -> DownloadResult: ...

    def clear_cache(self) -> None: ...
