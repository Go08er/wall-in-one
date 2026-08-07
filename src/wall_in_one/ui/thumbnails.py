"""Getting thumbnails onto tiles without freezing the window.

At ~0.3s each, a library of any size would stall the main loop if thumbnails
were generated inline. Work happens on a small thread pool; results come back
through `GLib.idle_add`, which is the only safe way to touch a widget from
another thread.

What comes back is a `Gdk.Texture`, not a path, and the decode happens on the
pool too. That is not a detail: a warm cache of six hundred thumbnails is 372
ms of `Gdk.Texture.new_from_filename` on the main thread, which was the single
largest cost of showing a large library and dwarfed building the widgets. GDK
textures are immutable and safe to create off the main thread, and one made by
a worker drops straight into a `Gtk.Picture` -- measured, along with the 3x
speedup from decoding four at a time.

Delivering a cache hit synchronously used to matter, because a rebuilt grid
would otherwise flash through an empty state on its way to looking identical.
`WallpaperGrid.populate` diffs now and reuses the tiles it already has, so
there is no rebuild to flash and no reason left to decode on this thread.

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
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib

from wall_in_one import thumbnails
from wall_in_one.library.model import MediaItem

#: Enough to keep a few cores busy without turning a library refresh into a
#: fork bomb of ffmpeg processes.
MAX_WORKERS: Final = 4

Callback = Callable[[MediaItem, Gdk.Texture | None], None]


class ThumbnailLoader:
    """Requests thumbnails off-thread and delivers them on the main thread."""

    def __init__(self, max_workers: int = MAX_WORKERS) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="thumb")
        self._pending: dict[Path, Future[Gdk.Texture | None]] = {}
        self._closed = False
        # Bound the cache once at startup, on the pool rather than here. A user
        # who moves their wallpaper collection elsewhere generates nothing new,
        # so without this the thumbnails of a library they no longer own would
        # sit in `~/.cache` forever -- which is exactly what the plugin this
        # app replaces did.
        self._pool.submit(thumbnails.prune)

    def request(self, item: MediaItem, callback: Callback) -> None:
        """Ask for ``item``'s thumbnail. ``callback`` runs on the main thread.

        Everything happens on the pool: the cache lookup, the ffmpeg call if
        it misses, and the decode either way. Nothing here touches a widget.
        """
        if self._closed or item.path in self._pending:
            return

        future = self._pool.submit(self._texture_for, item)
        self._pending[item.path] = future
        future.add_done_callback(lambda done: self._finish(item, done, callback))

    @staticmethod
    def _texture_for(item: MediaItem) -> Gdk.Texture | None:
        """Cache lookup, generation if needed, and the decode. Off-thread.

        `lookup` validates the entry before answering, so a thumbnail
        truncated by a power cut is regenerated here rather than failing to
        decode in a tile.
        """
        try:
            path = thumbnails.lookup(item) or thumbnails.generate(item)
        except thumbnails.ThumbnailError:
            # A wallpaper we cannot thumbnail is not an error worth stopping
            # for; the tile falls back to a placeholder.
            return None
        try:
            return Gdk.Texture.new_from_filename(str(path))
        except GLib.Error:
            # Decodable by ffmpeg, not by GDK. Same answer: a blank tile with
            # its name still under it.
            return None

    def _finish(
        self, item: MediaItem, future: Future[Gdk.Texture | None], callback: Callback
    ) -> None:
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
