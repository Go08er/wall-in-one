"""A small bounded cache, so a provider is not re-asked for the same page.

The predecessor cached to a JSON file under the plugin's cache directory, with
an LRU order, a schema version, size ceilings and a lock, because each request
was a fresh process that shared nothing with the last. A long-lived app shares
everything, so the file, the lock and the schema are gone and this is a dict.

Kept from that design: the entry ceiling and the TTL. Both exist because a
provider's answer goes stale and because a user scrolling a grid can otherwise
pin an unbounded number of pages in memory.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Final, Generic, TypeVar

#: Search pages are large; details are small and re-read constantly.
DEFAULT_MAX_ENTRIES: Final = 32
DEFAULT_TTL_SECONDS: Final = 15 * 60

Value = TypeVar("Value")


class TtlCache(Generic[Value]):
    """Least-recently-used, with a per-entry expiry."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[str, tuple[float, Value]] = OrderedDict()
        self._max_entries = max(1, max_entries)
        self._ttl = ttl
        self._clock = clock

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> Value | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at > self._ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def put(self, key: str, value: Value) -> None:
        self._entries[key] = (self._clock(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
