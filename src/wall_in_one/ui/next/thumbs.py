"""Where the new interface's widgets get pictures from: an injected provider.

Cards, the player bar, the inspector and the frosted backdrop never decode or
draw a picture themselves. They ask the installed `ThumbnailProvider` for a
*source* (a wallpaper view, or whatever else that provider understands) and
show a flat placeholder until the texture arrives on the main thread.

* The app installs one backed by its thumbnail pipeline
  (`wall_in_one.ui.next.library_thumbnails`): ffmpeg off the main thread,
  the bounded disk cache, decode on the pool.
* The design prototype installs one that draws procedural art on worker
  threads (``prototypes/ui-redesign/wio_demo/thumbs.py``).

A texture that is already cached comes back from `ThumbnailProvider.request`
at once, so rebuilding a grid never flashes placeholders.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Protocol

import gi

gi.require_version("Gdk", "4.0")

from gi.repository import Gdk

#: Called once on the main thread with the texture, or None when there is none.
Deliver = Callable[[Gdk.Texture | None], None]


class ThumbnailProvider(Protocol):
    """Pictures of sources, drawn off the main thread and delivered on it."""

    def key(self, source: object, width: int, height: int) -> Hashable:
        """Identity of one request: equal keys are the same picture."""
        ...

    def cached(self, source: object, width: int, height: int) -> Gdk.Texture | None:
        """The texture now if it is ready, without starting any work."""
        ...

    def request(
        self, source: object, width: int, height: int, deliver: Deliver
    ) -> Gdk.Texture | None:
        """The texture now if cached; otherwise None and ``deliver`` runs later."""
        ...

    def placeholder(self, source: object) -> Gdk.RGBA | None:
        """A flat stand-in while the picture loads, or None for a neutral one."""
        ...

    def pending(self) -> int:
        """Pictures requested but not delivered yet."""
        ...


class NoPictures:
    """The provider before any is installed: every source stays a placeholder."""

    def key(self, source: object, width: int, height: int) -> Hashable:
        return (id(source), width, height)

    def cached(self, source: object, width: int, height: int) -> Gdk.Texture | None:
        return None

    def request(
        self, source: object, width: int, height: int, deliver: Deliver
    ) -> Gdk.Texture | None:
        return None

    def placeholder(self, source: object) -> Gdk.RGBA | None:
        return None

    def pending(self) -> int:
        return 0


_installed: ThumbnailProvider = NoPictures()


def install(provider: ThumbnailProvider) -> None:
    """Make ``provider`` the one every widget built from now on uses."""
    global _installed
    _installed = provider


def uninstall(provider: ThumbnailProvider) -> None:
    """Drop ``provider`` if it is still the installed one (a closing window's)."""
    global _installed
    if _installed is provider:
        _installed = NoPictures()


def provider() -> ThumbnailProvider:
    """The installed provider."""
    return _installed
