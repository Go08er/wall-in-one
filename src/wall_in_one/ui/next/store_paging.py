"""The Store's bounded result pages, with no toolkit in sight.

DECISIONS.md, "Browse pages card widgets; it does not retain an infinite photo
grid": infinite scroll kept every card alive (0.6 MB each, measured), so the
Store shows at most `PAGE_SIZE` cards at once and honest Previous/Next pages.
Results themselves are cheap records and are kept, up to `MAX_RESULTS`, so
going back is free and a 250-result MotionBGS search is reachable to its end.

Each provider page becomes its own result page(s) of at most 40, as in the
classic Store: merging them would move results under the person's feet when
the next provider page arrived (a page of 24 would grow to 40 and Next would
skip the 16 that slid onto it). A provider page that overlaps the previous one
adds only what is new, and one that adds nothing stops asking for more.
"""

from __future__ import annotations

from typing import Final

from wall_in_one.ui.next.store_state import MAX_RESULTS, PAGE_SIZE, StoreItemView, StoreResults

__all__: Final = ("ResultPages",)


class ResultPages:
    """Results of one search, cut into pages of at most `PAGE_SIZE`."""

    def __init__(self, page_size: int = PAGE_SIZE, ceiling: int = MAX_RESULTS) -> None:
        self._page_size = page_size
        self._ceiling = ceiling
        self.reset()

    def reset(self) -> None:
        """Forget everything: a new search."""
        self._items: list[StoreItemView] = []
        self._ids: set[str] = set()
        self._pages: list[tuple[StoreItemView, ...]] = []
        self._index = 0
        #: The provider pages fetched so far (the next one to ask for is this + 1).
        self.provider_page = 0
        #: The provider said there is more, and asking again can add something.
        self.has_more = False
        #: The `MAX_RESULTS` ceiling stopped the search.
        self.capped = False
        self.total_hint = 0
        self.dropped = 0
        self.cached = False

    # -- reading ------------------------------------------------------------------
    @property
    def items(self) -> tuple[StoreItemView, ...]:
        """Every result kept, in order."""
        return tuple(self._items)

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def pages(self) -> int:
        return len(self._pages)

    @property
    def index(self) -> int:
        """The page shown (0-based)."""
        return self._index

    @property
    def current(self) -> tuple[StoreItemView, ...]:
        """The results of the page shown: the only ones that get cards."""
        return self._pages[self._index] if self._pages else ()

    @property
    def can_previous(self) -> bool:
        return self._index > 0

    @property
    def can_next(self) -> bool:
        """Another page is kept, or the provider has more to ask for."""
        return self._index + 1 < len(self._pages) or self.has_more

    @property
    def next_is_kept(self) -> bool:
        """Next shows a page already here (no request)."""
        return self._index + 1 < len(self._pages)

    def label(self) -> str:
        """``Page 2 of 3``, ``· more online`` while the provider has more."""
        if not self._pages:
            return ""
        more = " · more online" if self.has_more else ""
        return f"Page {self._index + 1} of {len(self._pages)}{more}"

    def summary(self) -> str:
        """``64 results of about 1,200``, and what the provider dropped or cached."""
        shown = len(self._items)
        if self.capped:
            parts = [f"the first {shown:,} results"]
        else:
            parts = [f"{shown:,} result{'' if shown == 1 else 's'}"]
        if self.total_hint:
            parts[0] += f" of about {self.total_hint:,}"
        if self.dropped:
            # A sudden jump means the remote's markup moved: say it, don't hide it.
            parts.append(f"{self.dropped} unreadable")
        return " · ".join(parts)

    # -- changing -----------------------------------------------------------------
    def add(self, answer: StoreResults, provider_page: int) -> int:
        """Keep a provider page's new results; returns how many were new."""
        self.provider_page = max(self.provider_page, provider_page)
        self.has_more = answer.has_next
        self.total_hint = answer.total_hint or self.total_hint
        self.dropped += answer.dropped
        self.cached = answer.cached
        added: list[StoreItemView] = []
        for item in answer.items:
            if item.id in self._ids:
                continue  # an overlapping page: one record per result
            if len(self._items) >= self._ceiling:
                self.capped = True
                break
            self._ids.add(item.id)
            self._items.append(item)
            added.append(item)
        if len(self._items) >= self._ceiling and answer.has_next:
            self.capped = True
        if self.capped:
            self.has_more = False
        if provider_page > 1 and not added:
            # A provider that repeats a page must not turn each Next into
            # another request with nothing to show for it.
            self.has_more = False
        for start in range(0, len(added), self._page_size):
            self._pages.append(tuple(added[start : start + self._page_size]))
        return len(added)

    def show(self, index: int) -> bool:
        """Show page ``index`` if it is kept; False when it isn't."""
        if 0 <= index < len(self._pages):
            self._index = index
            return True
        return False

    def stop(self) -> None:
        """Asking for more failed: keep what is here and stop offering Next online."""
        self.has_more = False

    def index_of(self, item_id: str) -> int:
        """The page holding ``item_id``, or -1."""
        for index, page in enumerate(self._pages):
            if any(item.id == item_id for item in page):
                return index
        return -1
