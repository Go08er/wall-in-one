"""Applying a wallpaper: stills through Noctalia, videos through mpvpaper.

Two backends that must never both be on screen. A video renders on the
background layer and would sit on top of a still Noctalia had set, so applying
a still stops the renderer first. Applying a video sets its paired still
underneath, which means the screen is still right if mpvpaper dies.

Stills deliberately go through `noctalia msg wallpaper-set` rather than being
drawn ourselves: Noctalia then regenerates its palette from the new wallpaper,
which fires our template's post-hook, which reloads our colours. Setting the
wallpaper behind Noctalia's back would leave the palette pointing at the
previous image.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper import renderer


class ApplyError(Exception):
    """A wallpaper could not be applied."""


@dataclass(frozen=True, slots=True)
class Applied:
    """What actually ended up on screen."""

    item: MediaItem
    path: Path
    animated: bool

    def describe(self) -> str:
        how = "playing" if self.animated else "set"
        return f"{how} {self.path.name}"


class Applier:
    """Applies wallpapers, owning the video renderer's lifetime."""

    def __init__(
        self,
        video_renderer: renderer.Renderer | None = None,
        output: str = "",
    ) -> None:
        self._renderer = video_renderer if video_renderer is not None else renderer.Renderer()
        self._current: Applied | None = None
        #: Connector name, or empty for every output. Stills carry it to
        #: Noctalia's `wallpaper-set [connector]`; videos carry mpvpaper's own
        #: selector, which the renderer already holds.
        self.output = output

    @property
    def current(self) -> Applied | None:
        return self._current

    @property
    def renderer(self) -> renderer.Renderer:
        return self._renderer

    def apply(self, item: MediaItem, *, dynamics_enabled: bool) -> Applied:
        """Put ``item`` on screen, honouring the dynamics setting."""
        target = item.playback_path(dynamics_enabled=dynamics_enabled)
        if target is None:
            raise ApplyError(f"{item.name} is a video with no still, and dynamics are off")

        if item.kind is Kind.VIDEO and dynamics_enabled:
            applied = self._apply_video(item, target)
        else:
            applied = self._apply_still(item, target)
        self._current = applied
        return applied

    def _apply_still(self, item: MediaItem, path: Path) -> Applied:
        # Stop first: a running video would cover the still we are about to set.
        self._renderer.stop()
        try:
            noctalia.set_wallpaper(path, self.output or None)
        except noctalia.NoctaliaError as error:
            raise ApplyError(str(error)) from error
        return Applied(item=item, path=path, animated=False)

    def _apply_video(self, item: MediaItem, path: Path) -> Applied:
        # Put the paired still underneath first, so a renderer crash leaves the
        # right image on screen rather than the previous wallpaper -- and so
        # Noctalia's palette matches what the video looks like.
        if item.paired_still is not None:
            # Not fatal if it fails: the video is what the user asked for.
            with contextlib.suppress(noctalia.NoctaliaError):
                noctalia.set_wallpaper(item.paired_still, self.output or None)
        try:
            self._renderer.start(path)
        except renderer.RendererError as error:
            raise ApplyError(str(error)) from error
        return Applied(item=item, path=path, animated=True)

    def set_dynamics(self, enabled: bool) -> Applied | None:
        """Re-apply the current wallpaper under a changed dynamics setting.

        Returns ``None`` when the current wallpaper cannot survive the change --
        a video with no still, being paused. That is not an error: the caller
        picks something else to show. Raising here would leave the setting
        changed but the video still playing, which is the one outcome nobody
        wants.
        """
        current = self._current
        if current is None:
            if not enabled:
                self._renderer.stop()
            return None
        if current.item.kind is not Kind.VIDEO:
            return current
        if current.item.playback_path(dynamics_enabled=enabled) is None:
            self._renderer.stop()
            self._current = None
            return None
        return self.apply(current.item, dynamics_enabled=enabled)

    def shutdown(self) -> None:
        """Stop the renderer. The still stays: Noctalia owns it now."""
        self._renderer.stop()
