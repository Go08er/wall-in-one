"""The app's thumbnail pipeline as the new interface's `ThumbnailProvider`.

It is the classic grid's `ThumbnailLoader`: the cache lookup, ffmpeg when it
misses and the decode all run on its pool, and textures are delivered on the
main thread above GTK's redraw priority. This adds what the widgets need on
top: an in-memory set of recent textures, so a rebuilt page shows its cards at
once, and one request per item however many widgets ask.

Every thumbnail is the cache's 320 x 180; widgets draw it cover-fit at
whatever size they are, so the requested size is not part of the key.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import gi

gi.require_version("Gdk", "4.0")

from gi.repository import Gdk

from wall_in_one.library.model import MediaItem
from wall_in_one.ui.next.thumbs import Deliver
from wall_in_one.ui.thumbnails import ThumbnailLoader

#: Recent textures kept for synchronous hits: a few pages of cards.
CAPACITY: Final = 512


class HasMediaItem(Protocol):
    """A source this provider can draw: anything that names its library item."""

    @property
    def item(self) -> MediaItem: ...


@dataclass(frozen=True, slots=True)
class StillSource:
    """A still on disk, for the frosted backdrop."""

    item: MediaItem


class LibraryThumbnails:
    """`ThumbnailProvider` over the app's `ThumbnailLoader`."""

    def __init__(self, loader: ThumbnailLoader | None = None) -> None:
        self._loader = loader if loader is not None else ThumbnailLoader()
        self._ready: OrderedDict[MediaItem, Gdk.Texture] = OrderedDict()
        self._failed: set[MediaItem] = set()
        self._waiting: dict[MediaItem, list[Deliver]] = {}
        self._closed = False

    @staticmethod
    def _item(source: object) -> MediaItem | None:
        if isinstance(source, MediaItem):
            return source
        item = getattr(source, "item", None)
        return item if isinstance(item, MediaItem) else None

    def key(self, source: object, width: int, height: int) -> Hashable:
        item = self._item(source)
        return ("thumbnail", item) if item is not None else ("nothing", id(source))

    def cached(self, source: object, width: int, height: int) -> Gdk.Texture | None:
        item = self._item(source)
        if item is None:
            return None
        found = self._ready.get(item)
        if found is not None:
            self._ready.move_to_end(item)
        return found

    def request(
        self, source: object, width: int, height: int, deliver: Deliver
    ) -> Gdk.Texture | None:
        item = self._item(source)
        if item is None or self._closed:
            return None
        found = self.cached(item, width, height)
        if found is not None or item in self._failed:
            return found
        waiting = self._waiting.get(item)
        if waiting is not None:
            waiting.append(deliver)
            return None
        self._waiting[item] = [deliver]
        self._loader.request(item, self._delivered)
        return None

    def _delivered(self, item: MediaItem, texture: Gdk.Texture | None) -> None:
        if texture is None:
            # Not thumbnailable (or no ffmpeg): the placeholder stays, and the
            # same item is not asked for again while this window is open.
            self._failed.add(item)
        else:
            self._ready[item] = texture
            self._ready.move_to_end(item)
            while len(self._ready) > CAPACITY:
                self._ready.popitem(last=False)
        for deliver in self._waiting.pop(item, []):
            deliver(texture)

    def placeholder(self, source: object) -> Gdk.RGBA | None:
        return None

    def pending(self) -> int:
        return len(self._waiting)

    def shutdown(self) -> None:
        """Stop the pool and its ffmpeg children; nothing is delivered after this."""
        self._closed = True
        self._waiting.clear()
        self._ready.clear()
        self._loader.shutdown()


def still_item(path: Path, items: tuple[MediaItem, ...]) -> MediaItem | None:
    """The scanned item for a still at ``path`` (a library item or a known still)."""
    return next((item for item in items if item.path == path), None)
