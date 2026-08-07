"""Taking stills from videos in the background, so none is ever missing.

`library.stills` can make a still; `session` makes one at the moment dynamics
are switched off, for the video that would otherwise go dead. That is the
narrowest possible fix and it leaves the rest of the library as it was: every
other video keeps its "no still" badge, keeps dropping out of the rotation when
dynamics are off, and keeps giving Noctalia a palette generated from whatever
was on screen before it.

So this does the rest, on a pool, the way `ui.thumbnails` does thumbnails. The
two are deliberately the same shape -- work off-thread, deliver through
`GLib.idle_add` -- but not the same pool: a still is a full-resolution frame
out of a 4K video and takes about a second, against a thumbnail's third of one,
and letting those queue behind each other would leave the grid blank while the
stills ground away.

One worker, not four. Every job here is ffmpeg decoding a large video, so the
disk is the limit rather than the cores, and a library of forty videos should
not start forty of them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib

from wall_in_one.library import stills
from wall_in_one.library.model import Kind, MediaItem

#: Deliberately one. See the module docstring.
MAX_WORKERS: Final = 1

#: Called once, on the main thread, after a batch has made at least one still.
#: The argument is how many were made, so a caller can decide whether a rescan
#: is worth doing.
Callback = Callable[[int], None]


class StillMaker:
    """Fills in the missing stills for a library, off the main thread."""

    def __init__(self, max_workers: int = MAX_WORKERS) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="still")
        self._closed = False
        # Videos already attempted, whether or not it worked. Without this a
        # rescan after a successful batch would queue the failures again, and
        # since a finished batch *causes* a rescan, a video ffmpeg cannot read
        # would loop forever.
        self._attempted: set[Path] = set()

    def request(self, items: Iterable[MediaItem], root: Path, callback: Callback) -> None:
        """Make stills for any videos in ``items`` that have none.

        Returns immediately. ``callback`` runs on the main thread once, and
        only if something was actually made -- a rescan that would change
        nothing is not worth the disk.
        """
        if self._closed:
            return
        wanted = [
            item
            for item in items
            if item.kind is Kind.VIDEO
            and item.paired_still is None
            and item.path not in self._attempted
        ]
        if not wanted:
            return
        self._attempted.update(item.path for item in wanted)
        self._pool.submit(self._run, tuple(wanted), root, callback)

    def _run(self, items: tuple[MediaItem, ...], root: Path, callback: Callback) -> None:
        made = 0
        for item in items:
            if self._closed:
                return
            # `ensure` swallows its own failures: a still that cannot be made
            # is not a reason to stop making the others, and the video still
            # plays either way.
            if stills.ensure(item, root) is not None:
                made += 1
        if made == 0 or self._closed:
            return

        def deliver() -> bool:
            if not self._closed:
                callback(made)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def forget(self, path: Path) -> None:
        """Allow ``path`` to be attempted again.

        For a video whose file changed underneath us. Nothing calls this yet;
        it exists so that the memo above is a cache rather than a one-way door.
        """
        self._attempted.discard(path)

    def shutdown(self) -> None:
        self._closed = True
        # Not waiting: a 4K frame grab takes about a second and quitting should
        # be immediate. `library.stills` writes to a temporary name and renames,
        # so a still interrupted here leaves nothing half-written to be found.
        self._pool.shutdown(wait=False, cancel_futures=True)
