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

from wall_in_one.library import pairings
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper import renderer, scenes


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
        scene_renderer: scenes.SceneRenderer | None = None,
        *,
        own_scene_renderer: bool = False,
    ) -> None:
        self._renderer = video_renderer if video_renderer is not None else renderer.Renderer()
        self._scenes = (
            scene_renderer if scene_renderer is not None else scenes.SceneRenderer(output=output)
        )
        #: Whether this app may start `linux-wallpaperengine` at all. Off by
        #: default: the engine is single-instance per output and other things
        #: drive it, so taking it over is the user's decision.
        self.own_scene_renderer = own_scene_renderer
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

    def apply(
        self,
        item: MediaItem,
        *,
        dynamics_enabled: bool,
        palette: pairings.PalettePolicy | None = None,
        generator: str = noctalia.DEFAULT_SCHEME,
    ) -> Applied:
        """Put ``item`` on screen, honouring the dynamics setting.

        Order is still, mode, palette, then renderer, and it is not arbitrary.
        The still has to land first because Noctalia derives adaptive colours
        from whatever wallpaper is set, so asking for them before it would
        generate from the previous picture. The renderer goes last because it
        covers the still, and because a palette that fails should not leave a
        video running over the wrong colours.
        """
        target = item.playback_path(dynamics_enabled=dynamics_enabled)
        if target is None:
            raise ApplyError(f"{item.name} is a video with no still, and dynamics are off")

        moving = item.is_moving and dynamics_enabled
        still = item.paired_still if moving else target
        if moving:
            self._set_still_under_video(still)
        else:
            self._set_still(target)

        self._apply_palette(palette, generator)

        if not moving:
            applied = Applied(item=item, path=target, animated=False)
        elif item.kind is Kind.SCENE:
            applied = self._start_scene(item, target)
        else:
            applied = self._start_video(item, target)
        self._current = applied
        return applied

    def _apply_palette(self, palette: pairings.PalettePolicy | None, generator: str) -> None:
        """Ask Noctalia for this wallpaper's colours. Never fatal.

        A palette that will not apply is a disappointment, not a reason to
        leave the wallpaper unchanged -- the picture is what was asked for, and
        it is already on screen by the time we get here.
        """
        if palette is None:
            return
        if palette.mode is not pairings.Mode.KEEP:
            with contextlib.suppress(noctalia.NoctaliaError):
                noctalia.set_mode(palette.mode.value)
        selection = palette.selection(generator)
        if selection is None:
            return
        with contextlib.suppress(noctalia.NoctaliaError):
            noctalia.set_scheme(selection)

    def _set_still(self, path: Path) -> None:
        # Stop first: a running video or scene would cover the still we are
        # about to set.
        self._renderer.stop()
        self._scenes.stop()
        try:
            noctalia.set_wallpaper(path, self.output or None)
        except noctalia.NoctaliaError as error:
            raise ApplyError(str(error)) from error

    def _set_still_under_video(self, still: Path | None) -> None:
        """The still goes underneath before the video starts.

        So a renderer crash leaves the right image on screen rather than the
        previous wallpaper, and so an adaptive palette is generated from what
        the video looks like. Not fatal if it fails: the video is what was
        asked for.
        """
        if still is None:
            return
        with contextlib.suppress(noctalia.NoctaliaError):
            noctalia.set_wallpaper(still, self.output or None)

    def _start_scene(self, item: MediaItem, path: Path) -> Applied:
        """Hand a Wallpaper Engine scene to `linux-wallpaperengine`.

        Refuses rather than fights. The engine is single-instance per output,
        so starting a second one means two programs driving one wallpaper and
        the loser is whichever the user was actually looking at. The still is
        already on screen by this point either way, so a refusal leaves them
        with the scene's representative rather than with nothing.
        """
        if not self.own_scene_renderer:
            raise ApplyError(
                f"{item.name} is a Wallpaper Engine scene, and this app is not set to drive "
                "linux-wallpaperengine. Turn on 'own the scene renderer' in Settings."
            )
        held = scenes.running_elsewhere(self.output)
        if held:
            raise ApplyError(
                f"linux-wallpaperengine is already running on {self.output or 'this screen'} "
                f"(pid {held[0]}), so {item.name} was not started. Stop the other one first."
            )
        self._renderer.stop()
        try:
            self._scenes.start(item.scene)
        except scenes.SceneError as error:
            raise ApplyError(str(error)) from error
        return Applied(item=item, path=path, animated=True)

    def _start_video(self, item: MediaItem, path: Path) -> Applied:
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

    @property
    def scenes(self) -> scenes.SceneRenderer:
        return self._scenes

    def shutdown(self) -> None:
        """Stop the renderers. The still stays: Noctalia owns it now."""
        self._renderer.stop()
        self._scenes.stop()
