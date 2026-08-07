"""Generating thumbnails without freezing the window.

At ~0.3s each, a library of any size would stall the main loop if thumbnails
were generated inline. Work happens on a small thread pool; results come back
through `GLib.idle_add`, which is the only safe way to touch a widget from
another thread.

The cache itself lives in `wall_in_one.thumbnails` and knows nothing about GTK.
This module only decides *when* to ask it, and on which thread.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Final

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib

from wall_in_one import thumbnails
from wall_in_one.library.model import MediaItem

#: Enough to keep a few cores busy without turning a library refresh into a
#: fork bomb of ffmpeg processes.
MAX_WORKERS: Final = 4

Callback = Callable[[MediaItem, Path | None], None]


class ThumbnailLoader:
    """Requests thumbnails off-thread and delivers them on the main thread."""

    def __init__(self, max_workers: int = MAX_WORKERS) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="thumb")
        self._pending: dict[Path, Future[Path | None]] = {}
        self._closed = False
        # Bound the cache once at startup, on the pool rather than here. A user
        # who moves their wallpaper collection elsewhere generates nothing new,
        # so without this the thumbnails of a library they no longer own would
        # sit in `~/.cache` forever -- which is exactly what the plugin this
        # app replaces did.
        self._pool.submit(thumbnails.prune)

    def request(self, item: MediaItem, callback: Callback) -> None:
        """Ask for ``item``'s thumbnail. ``callback`` runs on the main thread.

        A cache hit is delivered immediately and synchronously, so a rebuilt
        grid of already-thumbnailed wallpapers does not flash through an empty
        state on its way to looking identical. `lookup` validates the entry
        before answering, so a thumbnail truncated by a power cut takes the slow
        path here instead of failing to decode in a tile.
        """
        if self._closed:
            return

        cached = thumbnails.lookup(item)
        if cached is not None:
            callback(item, cached)
            return

        if item.path in self._pending:
            return

        future = self._pool.submit(self._generate, item)
        self._pending[item.path] = future
        future.add_done_callback(lambda done: self._finish(item, done, callback))

    @staticmethod
    def _generate(item: MediaItem) -> Path | None:
        try:
            return thumbnails.generate(item)
        except thumbnails.ThumbnailError:
            # A wallpaper we cannot thumbnail is not an error worth stopping
            # for; the tile falls back to a placeholder.
            return None

    def _finish(self, item: MediaItem, future: Future[Path | None], callback: Callback) -> None:
        self._pending.pop(item.path, None)
        if self._closed:
            return
        try:
            result = future.result()
        except Exception:
            # Broad on purpose: a worker must never take the app down.
            result = None

        def deliver() -> bool:
            if not self._closed:
                callback(item, result)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def shutdown(self) -> None:
        """Stop delivering results and let the pool drain."""
        self._closed = True
        self._pending.clear()
        # Not waiting: an ffmpeg call can take seconds and quitting should be
        # immediate. The workers write to a temp name and rename, so a thumbnail
        # interrupted here leaves nothing half-written behind.
        self._pool.shutdown(wait=False, cancel_futures=True)
