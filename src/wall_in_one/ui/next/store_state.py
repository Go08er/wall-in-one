"""What the new interface's Store page needs from the app.

Browsing the providers and downloading into the library. `AppState.store_access`
returns a `StoreAccess`, or None while the page is not ported or the app can't
offer it; the page then shows its "not in the new interface yet" placeholder.
The real one lives beside the adapter in ``real_store.py``; the prototype's
demo state is the other.

**What the page asks.** A search is one provider page of one `StoreQuery`,
answered later on the main loop (`StoreResults`, or a `StoreProblem` saying
what went wrong). The page turns answers into bounded 40-card pages itself
(``store_paging.py``), so both adapters only ever answer "page N of this".
Pictures, details and downloads are asked for the same way; nothing the page
calls waits on a website, a disk walk or a decoder.

**What it hears.** The adapter emits ``changed("store")`` on the `AppState`
when anything the page shows about results changes without the page asking:
a download starting or landing, which results the library holds, whether
NSFW results are reachable, whether Steam is here. ``library`` and
``settings`` mean the library or its folders changed.

**What it can do.** Exactly what the backend does: one download at a time,
shown as busy plus "Downloading N of M", with no byte progress and no cancel.
A finished download is in the library folder, not yet in the library: the
adapter rescans, and only then applies it or shows it, when asked to.

GTK-free: annotations only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

if TYPE_CHECKING:
    from gi.repository import Gdk

# -- the providers -----------------------------------------------------------------

#: The registry's names, which the page and both adapters use as keys.
WALLHAVEN: Final = "wallhaven"
MOTIONBGS: Final = "motionbgs"


@dataclass(frozen=True, slots=True)
class StoreProvider:
    """A site the Store searches, and how to name it."""

    name: str
    title: str
    site: str
    #: What it offers, for placeholders and tooltips.
    noun: str
    icon: str


PROVIDERS: Final = (
    StoreProvider(WALLHAVEN, "Wallhaven", "wallhaven.cc", "images", "image-x-generic-symbolic"),
    StoreProvider(
        MOTIONBGS, "MotionBGS", "motionbgs.com", "live wallpapers", "video-x-generic-symbolic"
    ),
)
PROVIDER: Final = {provider.name: provider for provider in PROVIDERS}

# -- the filters: (key, label[, ...]) ---------------------------------------------

#: Wallhaven's sortings. "relevance" only means something with a query.
SORTS: Final = (
    ("relevance", "Relevance"),
    ("toplist", "Popular"),
    ("date_added", "Latest"),
    ("hot", "Hot"),
    ("views", "Most viewed"),
    ("favorites", "Most favorited"),
    ("random", "Random"),
)
#: Wallhaven's toplist windows: (key, label, phrase for the heading).
TOP_RANGES: Final = (
    ("1d", "Last day", "today"),
    ("3d", "Last 3 days", "in the last 3 days"),
    ("1w", "Last week", "this week"),
    ("1M", "Last month", "this month"),
    ("3M", "Last 3 months", "in the last 3 months"),
    ("6M", "Last 6 months", "in the last 6 months"),
    ("1y", "Last year", "this year"),
)
#: A minimum size: (key, label, Wallhaven ``atleast``).
SIZES: Final = (
    ("any", "Any size", ""),
    ("1080", "1080p or larger", "1920x1080"),
    ("1440", "1440p or larger", "2560x1440"),
    ("2160", "4K or larger", "3840x2160"),
    ("2880", "5K or larger", "5120x2880"),
)
#: Aspect ratios: (key, label, Wallhaven ``ratios``). Wallhaven's validator
#: takes only ``WxH`` values, so "Portrait" is the common tall ones.
RATIOS: Final = (
    ("any", "Any ratio", ""),
    ("16x9", "16:9", "16x9"),
    ("16x10", "16:10", "16x10"),
    ("21x9", "21:9 ultrawide", "21x9"),
    ("32x9", "32:9 super ultrawide", "32x9"),
    ("portrait", "Portrait", "9x16,10x16,9x18"),
)
#: Wallhaven's three-bit masks, in bit order.
CATEGORIES: Final = ("General", "Anime", "People")
PURITIES: Final = ("SFW", "Sketchy", "NSFW")
#: Wallhaven's colour search palette, in the site's order (all 29 it accepts).
#: Spelled out rather than imported, so the page does not load a provider to
#: draw its filters; a test pins it to `providers.wallhaven.COLOR_ORDER`.
COLOURS: Final[tuple[str, ...]] = (
    "660000", "990000", "cc0000", "cc3333", "ea4c88", "993399",
    "663399", "333399", "0066cc", "0099cc", "66cccc", "77cc33",
    "669900", "336600", "666600", "999900", "cccc33", "ffff00",
    "ffcc33", "ff9900", "ff6600", "cc6633", "996633", "663300",
    "000000", "999999", "cccccc", "ffffff", "424153",
)  # fmt: skip
#: MotionBGS's browse modes. Searching is implied by typing a query.
MODES: Final = (
    ("latest", "Latest"),
    ("4k", "4K"),
    ("hd", "HD"),
    ("genre", "By genre"),
)
#: Which file a MotionBGS download takes: (variant, label). "" is the best
#: the wallpaper offers (4K where there is one).
QUALITIES: Final = (
    ("", "Best available"),
    ("4k", "4K"),
    ("hd", "HD"),
)

#: The page's grid size: at most this many result cards exist at once.
PAGE_SIZE: Final = 40
#: Results kept as cheap records across pages of one search (the measured
#: MotionBGS 4K catalogue is 7,380).
MAX_RESULTS: Final = 10_000

#: How a download finishes: "" (stay), "apply" it, or "show" it in the Library.
Then = Literal["", "apply", "show"]


@dataclass(frozen=True, slots=True)
class StoreQuery:
    """Everything the Store's search box and filters say, with no toolkit in sight.

    Every field defaults to the page's opening view (Wallhaven's popular
    wallpapers this week, safe for work). Fields of the other provider are
    ignored, so switching back and forth keeps each provider's choices.
    """

    provider: str = WALLHAVEN
    text: str = ""
    # -- Wallhaven ------------------------------------------------------------
    sort: str = "toplist"
    #: Ascending instead of Wallhaven's descending order.
    reverse: bool = False
    top_range: str = "1w"
    size: str = "any"
    ratio: str = "any"
    #: One of `COLOURS`, or empty for any.
    colour: str = ""
    categories: frozenset[str] = frozenset(CATEGORIES)
    purity: frozenset[str] = frozenset({"SFW"})
    # -- MotionBGS --------------------------------------------------------------
    mode: str = "latest"
    #: A MotionBGS genre (its tag slug), for the "By genre" mode.
    genre: str = ""
    #: The download variant (see `QUALITIES`).
    quality: str = ""

    @property
    def uses_range(self) -> bool:
        """Only the top list has a time window ("Top list covers")."""
        return self.provider == WALLHAVEN and self.sort == "toplist"

    @property
    def can_reverse(self) -> bool:
        return self.provider == WALLHAVEN and self.sort not in ("random", "relevance")


def bits(chosen: frozenset[str], names: Sequence[str]) -> str:
    """Wallhaven's mask for ``chosen`` (``"110"``); nothing chosen means the first."""
    mask = "".join("1" if name in chosen else "0" for name in names)
    return mask if "1" in mask else "1" + "0" * (len(names) - 1)


def option(table: Sequence[tuple[str, ...]], key: str, column: int) -> str:
    """Column ``column`` of the ``key`` row of an option table, or of its first row."""
    for row in table:
        if row[0] == key:
            return row[column]
    return table[0][column]


def short_resolution(resolution: str) -> str:
    """The badge on a card: ``5K``, ``4K``, ``1440p``, plus a ratio that isn't 16:9.

    ``resolution`` is a provider's own (``3840x2160``), or already short
    (MotionBGS says ``4K`` or ``HD``), or empty.
    """
    match = re.fullmatch(r"\s*(\d+)\s*[x\u00d7]\s*(\d+)\s*", resolution)
    if match is None:
        return resolution.strip()
    width, height = int(match.group(1)), int(match.group(2))
    if not width or not height:
        return ""
    short = min(width, height)
    label = "5K" if short >= 2880 else "4K" if short >= 2160 else f"{short}p"
    if height > width:
        return f"{label} · Portrait"
    value = width / height
    for name, target in (("16:10", 16 / 10), ("21:9", 21 / 9), ("32:9", 32 / 9)):
        if abs(value - target) < 0.08:
            return f"{label} · {name}"
    return label


def nearest_colour(hex_colour: str) -> str:
    """The Wallhaven search colour closest to ``hex_colour`` (six hex digits)."""
    value = hex_colour.lstrip("#").lower()
    if value in COLOURS:
        return value
    if len(value) != 6:
        return ""

    def rgb(text: str) -> tuple[int, int, int]:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)

    try:
        target = rgb(value)
    except ValueError:
        return ""
    return min(
        COLOURS, key=lambda c: sum((a - b) ** 2 for a, b in zip(rgb(c), target, strict=True))
    )


# -- what the adapter hands back ---------------------------------------------------


class StoreItemView(Protocol):
    """One search result, before anything is downloaded."""

    #: Unique across providers (both may call an item ``abc123``).
    @property
    def id(self) -> str: ...

    #: `WALLHAVEN` or `MOTIONBGS`.
    @property
    def provider(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def is_moving(self) -> bool: ...

    #: The provider's resolution (``3840x2160``, ``4K``), or empty.
    @property
    def resolution(self) -> str: ...

    #: One short line under the card (a size, a length), or empty.
    @property
    def detail(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StoreResults:
    """One provider page of results."""

    items: tuple[StoreItemView, ...]
    #: The provider says there is another page.
    has_next: bool = False
    #: The provider's own count of every match, or 0 when it doesn't say.
    total_hint: int = 0
    #: Results the provider sent that could not be read.
    dropped: int = 0
    cached: bool = False


@dataclass(frozen=True, slots=True)
class StoreProblem:
    """Why a search did not answer."""

    #: The provider's kind (``rate-limit``, ``challenge``, ``credential``,
    #: ``validation``, ``timeout``, ``transport``...), or ``failed``.
    kind: str
    #: For a person: the provider's own words, without the kind, which the
    #: error page shows in its title and buttons instead.
    message: str

    @property
    def reported(self) -> str:
        """``kind: message``, as a toast (and `ctl`) says it; ``failed`` says itself."""
        return self.message if self.kind == "failed" else f"{self.kind}: {self.message}"


@dataclass(frozen=True, slots=True)
class StoreDetail:
    """Everything a provider says about one result, for the preview dialog."""

    #: (label, value) pairs; the two providers know almost disjoint things.
    facts: tuple[tuple[str, str], ...] = ()
    tags: tuple[str, ...] = ()
    #: Palette entries, six hex digits without the ``#``.
    colours: tuple[str, ...] = ()
    #: The download qualities when there is a choice (MotionBGS ``hd``/``4k``).
    variants: tuple[str, ...] = ()
    #: A larger picture than the card's, or None.
    picture: Gdk.Paintable | None = None
    #: Why the facts are missing (they could not be fetched), or empty.
    problem: str = ""
    #: Where it came from (for "Open on ..."), or empty.
    page_url: str = ""


SearchDone = Callable[[StoreResults | StoreProblem], None]
DetailDone = Callable[[StoreDetail], None]
PictureDone = Callable[[StoreItemView, "Gdk.Paintable"], None]


@dataclass(frozen=True, slots=True)
class Downloads:
    """The download lane, as the footer and the cards show it."""

    #: Ids of results being downloaded (the first) or waiting for their turn.
    busy: tuple[str, ...] = ()
    #: "Downloading 2 of 5", or empty when nothing is queued.
    text: str = ""


class StoreAccess(Protocol):
    """The Store page's view of the app.

    Every callback runs later on the main loop, at ``GLib.PRIORITY_DEFAULT``,
    and only for the request that is still current: a newer search makes an
    older one's answer disappear rather than arrive. Nothing here blocks.
    """

    # -- searching ------------------------------------------------------------------

    def search_store(self, query: StoreQuery, page: int, done: SearchDone) -> None:
        """Ask ``query.provider`` for its page ``page`` (1 starts a new search)."""
        ...

    def searching(self) -> bool:
        """Whether the latest search's answer is still on its way.

        False once it has been delivered, and also when it never will be:
        the adapter was closed (the window went away) while it was out. A
        page still waiting for an answer then asks again.
        """
        ...

    def item_pictures(self, items: Sequence[StoreItemView], done: PictureDone) -> None:
        """Fetch these cards' pictures, first first; anything not listed is dropped."""
        ...

    def item_detail(self, item: StoreItemView, done: DetailDone) -> None:
        """What the provider says about ``item``, and a larger picture."""
        ...

    # -- what the library already holds ------------------------------------------------

    def in_library(self, item: StoreItemView) -> bool:
        """Whether a library folder holds ``item``, as last read (never reads the disk)."""
        ...

    # -- downloading -------------------------------------------------------------------

    @property
    def downloads(self) -> Downloads: ...

    def download_blocked(self) -> str:
        """Why nothing can be downloaded now (no library folder), or empty."""
        ...

    def download(self, item: StoreItemView, *, variant: str = "", then: Then = "") -> None:
        """Queue ``item`` (one download runs at a time); ``then`` once it is in the library."""
        ...

    def apply_item_blocked(self, item: StoreItemView) -> str:
        """Why ``item`` can't be applied or shown from the Store now, or empty."""
        ...

    def apply_item(self, item: StoreItemView) -> None:
        """Apply a result the library holds (rescanning first if it must)."""
        ...

    def show_item(self, item: StoreItemView) -> None:
        """Open a result the library holds in the Library page."""
        ...

    #: Where downloads go (the first library folder), as a person reads it, or
    #: empty when there is no library folder.
    @property
    def download_destination(self) -> str: ...

    # -- the API key and the Workshop ----------------------------------------------------

    def refresh_facts(self) -> None:
        """Re-read what can change outside the page: the key, Steam. Answers on "store"."""
        ...

    #: Why NSFW results can't be had (the provider's own words), or empty.
    @property
    def nsfw_blocked(self) -> str: ...

    #: Whether the Steam Workshop hand-off is offered (Steam is here).
    @property
    def has_workshop(self) -> bool: ...

    def open_workshop(self) -> None:
        """Hand off to Wallpaper Engine's Steam Workshop."""
        ...

    def open_site(self, item: StoreItemView) -> None:
        """Open ``item``'s page on its site in the browser."""
        ...
