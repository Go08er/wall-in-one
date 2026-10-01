"""The demo's thumbnail provider: procedural art, drawn on worker threads.

The widgets the app ships ask ``wall_in_one.ui.next.thumbs.provider()`` for
pictures. The app installs one over its real thumbnail pipeline; the demo
installs `LOADER` (``install()``), which draws the dummy wallpapers' and Store
items' art. ``art.render`` stays here: it is demo-only.

Card and row pictures load off the main thread: a worker draws the pixels
(``art.render``), ``GLib.idle_add`` hands them back at ``PRIORITY_HIGH_IDLE``
(above GTK's redraw, so a busy spinner can't starve the delivery), and the main
thread wraps them in a ``Gdk.MemoryTexture``. Widgets show a placeholder until
then (``ui.Thumb``). A picture that is already cached arrives synchronously, so
rebuilding a grid never flashes placeholders.
"""

from __future__ import annotations

import queue
import threading
from collections import OrderedDict
from collections.abc import Callable

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib

from wall_in_one.ui.next import thumbs as provider

from . import art

#: (style, seed, night, width, height)
Key = tuple[str, int, bool, int, int]


def texture(source, width: int = 480, height: int = 270) -> Gdk.Texture:
    """A picture of ``source`` (anything with an art ``key``), drawn now and cached.

    For the few places that need a texture at once (drag icons, the Displays
    arrangement); cards and rows use ``Loader.request`` through ``ui.Thumb``.
    """
    return art.texture(*source.key, width, height)


def placeholder(source) -> Gdk.RGBA:
    """A flat stand-in color while the picture loads: the scene's own mid-tone."""
    look = art.look_for(*source.key)
    r, g, b = art.mix(look.sky_low, look.land, 0.5)
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = r, g, b, 1.0
    return color


class Loader:
    """Draws thumbnails on worker threads and delivers them on the main thread."""

    def __init__(self, workers: int = 4, capacity: int = 512) -> None:
        self._workers = workers
        self._capacity = capacity
        self._jobs: queue.SimpleQueue[Key] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._ready: OrderedDict[Key, Gdk.Texture] = OrderedDict()
        self._waiting: dict[Key, list[Callable[[Gdk.Texture | None], None]]] = {}
        #: How many pictures were drawn off the main thread (checked by the smoke test).
        self.drawn_in_workers = 0

    def key(self, source, width: int, height: int) -> Key:
        """One request's identity: the art and its size."""
        return (*source.key, width, height)

    def placeholder(self, source) -> Gdk.RGBA:
        return placeholder(source)

    def cached(self, source, width: int, height: int) -> Gdk.Texture | None:
        key = (*source.key, width, height)
        found = self._ready.get(key)
        if found is not None:
            self._ready.move_to_end(key)
        return found

    def request(
        self, source, width: int, height: int, deliver: Callable[[Gdk.Texture | None], None]
    ) -> Gdk.Texture | None:
        """The texture now if it is cached; otherwise None, and ``deliver(texture)``
        runs on the main thread once a worker has drawn it."""
        found = self.cached(source, width, height)
        if found is not None:
            return found
        key = (*source.key, width, height)
        waiting = self._waiting.get(key)
        if waiting is not None:
            waiting.append(deliver)  # already being drawn
            return None
        self._waiting[key] = [deliver]
        self._start()
        self._jobs.put(key)
        return None

    def pending(self) -> int:
        """Pictures requested but not delivered yet."""
        return len(self._waiting)

    # -- workers -----------------------------------------------------------------
    def _start(self) -> None:
        while len(self._threads) < self._workers:
            # Daemon threads: quitting never waits for pictures nobody will see.
            thread = threading.Thread(target=self._work, name=f"thumbnails-{len(self._threads)}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _work(self) -> None:
        while True:
            key = self._jobs.get()
            try:
                pixels = art.render(*key)
                self.drawn_in_workers += 1
            except Exception:  # a broken picture must not leave its widgets waiting forever
                pixels = None
            GLib.idle_add(self._deliver, key, pixels, priority=GLib.PRIORITY_HIGH_IDLE)

    def _deliver(self, key: Key, pixels: tuple[bytes, int] | None) -> bool:
        found = art.adopt(*key, pixels) if pixels is not None else None
        if found is not None:
            self._ready[key] = found
            self._ready.move_to_end(key)
            while len(self._ready) > self._capacity:
                self._ready.popitem(last=False)
        for deliver in self._waiting.pop(key, []):
            deliver(found)
        return GLib.SOURCE_REMOVE


#: The loader every ui.Thumb uses in the demo.
LOADER = Loader()


def install() -> None:
    """Make the demo's art the pictures every shipped widget shows."""
    provider.install(LOADER)


install()
