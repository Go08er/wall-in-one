"""Finding one wallpaper among six hundred.

Searching, filtering by kind, and sort order, with no toolkit anywhere in
sight. The grid hands this module a `Query` and gets back the items to show,
in the order to show them; deciding *which* wallpapers those are is a
read-only question about a tuple of `MediaItem`, and answering it here is what
lets it be tested without a display -- the same split `library.pairing` keeps
from `library.stills`.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from wall_in_one.library.model import Kind, MediaItem


class Kinds(Enum):
    """Which kinds of wallpaper the grid is showing."""

    EVERYTHING = "everything"
    STILLS = "stills"
    VIDEOS = "videos"
    #: Not a kind of file but a kind of interest, and in this control rather
    #: than beside it because "which of these am I being shown" has one answer
    #: at a time: a separate favourites toggle would let someone ask for videos
    #: *and* favourites and then have to work out which of the two won.
    FAVOURITES = "favourites"

    @property
    def label(self) -> str:
        """What the control calls this choice."""
        return _KIND_LABELS[self]

    @property
    def noun(self) -> str:
        """What to call the things being shown, in a sentence about them."""
        return _KIND_NOUNS[self]

    def accepts(self, kind: Kind) -> bool:
        """Whether a file of this kind passes. Favourites accept every kind --
        which of them are favourites is a question about paths, and `apply`
        asks it separately because this enum has no way to know."""
        if self in (Kinds.EVERYTHING, Kinds.FAVOURITES):
            return True
        return kind is (Kind.STILL if self is Kinds.STILLS else Kind.VIDEO)


class Sort(Enum):
    """The orders the grid can be read in."""

    NAME = "name"
    #: Date added, which on disk is the modification time.
    NEWEST = "newest"
    LARGEST = "largest"

    @property
    def label(self) -> str:
        return _SORT_LABELS[self]


_KIND_LABELS: Final[Mapping[Kinds, str]] = {
    Kinds.EVERYTHING: "Everything",
    Kinds.STILLS: "Stills only",
    Kinds.VIDEOS: "Videos only",
    Kinds.FAVOURITES: "Favourites only",
}

_KIND_NOUNS: Final[Mapping[Kinds, str]] = {
    Kinds.EVERYTHING: "wallpapers",
    Kinds.STILLS: "stills",
    Kinds.VIDEOS: "videos",
    Kinds.FAVOURITES: "favourites",
}

#: Named for what they do rather than for the field they read: "Newest" is a
#: promise about the top of the grid, "mtime descending" is an implementation.
_SORT_LABELS: Final[Mapping[Sort, str]] = {
    Sort.NAME: "Name",
    Sort.NEWEST: "Newest first",
    Sort.LARGEST: "Largest first",
}

#: Every choice, in the order the controls offer them. The controls index back
#: into these tuples, so the list a user sees and the value they get cannot
#: drift apart.
KIND_CHOICES: Final[tuple[Kinds, ...]] = (
    Kinds.EVERYTHING,
    Kinds.STILLS,
    Kinds.VIDEOS,
    Kinds.FAVOURITES,
)
SORT_CHOICES: Final[tuple[Sort, ...]] = (Sort.NAME, Sort.NEWEST, Sort.LARGEST)


def fold(text: str) -> str:
    """The form of ``text`` that matching and name ordering compare.

    Three things happen, and each answers a way the same name can be spelled
    differently on disk than in the search box. It is decomposed, because a
    filename written with a combining accent (which is what a file copied off
    a Mac or a Samba share tends to carry) is a different string from the
    precomposed one a keyboard produces. The combining marks are then dropped,
    so that typing `cafe` finds `Café` -- nobody reaches for the accent key to
    search their own wallpapers. Finally `casefold` rather than `lower`,
    because it is the one that agrees that `STRASSE` and `Straße` are the same
    word.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def terms(text: str) -> tuple[str, ...]:
    """Split a query into the words a name has to satisfy."""
    return tuple(fold(text).split())


def matches(name: str, wanted: Sequence[str]) -> bool:
    """Does ``name`` contain every term in ``wanted``?

    The rule is: split the query on whitespace, and require each word to
    appear somewhere in the name, in any order. `snow vil` finds
    `snowy-village-still`, which plain substring matching does not, because
    the two words are separated in the name and not in the query.

    Subsequence matching -- `snwvlg` finding the same file -- was the other
    candidate and is rejected. It only reads as clever when the results are
    ranked by how well they matched, and this grid is sorted by whatever the
    user asked for in the sort control, so there is nowhere for a relevance
    score to go. Unranked, a subsequence over six hundred filenames matches
    most of them: three letters into a query the grid would still be full, and
    the user would have no idea why. Word-wise containment can be predicted
    from the filename by looking at it, which for a search box is worth more
    than reach.

    Only the stem is matched -- see `MediaItem.name`. Searching the whole path
    would mean every term that happens to name a directory silently matches
    everything underneath it.
    """
    folded = fold(name)
    return all(term in folded for term in wanted)


@dataclass(frozen=True, slots=True)
class Query:
    """What the library controls are asking for."""

    text: str = ""
    kinds: Kinds = Kinds.EVERYTHING
    sort: Sort = Sort.NAME

    @property
    def narrows(self) -> bool:
        """Is anything actually being hidden?

        Sorting is not narrowing: reordering the grid shows the same
        wallpapers, so it must not make the window start reporting a partial
        count.
        """
        return bool(terms(self.text)) or self.kinds is not Kinds.EVERYTHING


def order_key(item: MediaItem, sort: Sort) -> tuple[int, str, str]:
    """A key that puts ``item`` where ``sort`` wants it, ascending in each case.

    The leading number carries the whole difference between the sorts, negated
    for the two that read largest-first, so that one ascending sort serves all
    three and callers never have to remember which ones reverse.

    Ties fall back to the folded name and then the path. Without that, two
    wallpapers of the same size -- which is common, a pair downloaded from the
    same page -- would sit in whatever order the scan happened to yield, and
    the grid would reshuffle them on every rescan.
    """
    leading = 0
    if sort is Sort.NEWEST:
        leading = -item.mtime
    elif sort is Sort.LARGEST:
        leading = -item.size
    return (leading, fold(item.name), str(item.path))


def apply(
    items: Iterable[MediaItem],
    query: Query,
    favourites: Collection[Path] = (),
) -> tuple[MediaItem, ...]:
    """The items ``query`` selects, in the order it asks for.

    ``favourites`` is a separate argument rather than a field on the `Query`
    because the query is what the controls are asking for, while the favourites
    are data the controls know nothing about. Passing it every time also means
    a stale set cannot hide inside a query object that looks current.
    """
    wanted = terms(query.text)
    starred = frozenset(favourites)
    kept = [
        item
        for item in items
        if query.kinds.accepts(item.kind)
        and (query.kinds is not Kinds.FAVOURITES or item.path in starred)
        and matches(item.name, wanted)
    ]
    kept.sort(key=lambda item: order_key(item, query.sort))
    return tuple(kept)


def describe(query: Query) -> str:
    """A noun phrase for what ``query`` shows, for empty states to say.

    Phrased to survive a `No ` in front of it: "No videos matching "snow"" is
    a different complaint from "No wallpapers found", and a user who has
    typed a query needs to be told which one they are looking at.
    """
    wanted = query.text.strip()
    if wanted:
        return f'{query.kinds.noun} matching "{wanted}"'
    return query.kinds.noun
