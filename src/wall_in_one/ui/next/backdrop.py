"""The frosted style's backdrop: the current wallpaper, blurred, behind the window.

It draws only in the ``frosted`` window style, blurred by the Frost dial, and
crossfades when the wallpaper changes. What it shows is `AppState.backdrop`:
in the app, the still of the wallpaper the runtime reports on screen. Nothing
is painted over it but the window's own panels (see `theme.css.glass_layers`).
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, Graphene, Gsk, Gtk

from wall_in_one.ui.next import thumbs
from wall_in_one.ui.next.state import AppState

#: Strongest blur, in pixels, at Frost = 100%.
MAX_BLUR: Final = 60
#: Big enough to stay a picture when Frost is near 0.
SIZE: Final = (384, 216)
#: Topics that can change what, or how strongly, the backdrop shows.
TOPICS: Final = frozenset({"now", "displays", "theme", "appearance", "settings"})


class Backdrop(Gtk.Widget):
    """The current wallpaper, blurred by the Frost dial, drawn behind the whole window."""

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.set_can_target(False)
        self._key: Hashable | None = None
        self._new: Gdk.Texture | None = None
        self._old: Gdk.Texture | None = None
        self._fade = 1.0
        self._animation = Adw.TimedAnimation(
            widget=self,
            value_from=0.0,
            value_to=1.0,
            duration=700,
            target=Adw.CallbackAnimationTarget.new(self._on_fade),
        )
        self._animation.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
        state.connect("changed", self._on_changed)
        self.refresh(animate=False)

    def _on_fade(self, value: float) -> None:
        self._fade = value
        self.queue_draw()

    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic in TOPICS:
            self.refresh(animate=topic in ("now", "settings"))

    @property
    def texture(self) -> Gdk.Texture | None:
        """What is drawn now (the newer picture while a crossfade runs)."""
        return self._new

    def refresh(self, animate: bool = True) -> None:
        frosted = self.state.window_style == "frosted"
        self.set_visible(frosted)
        if not frosted:
            self.queue_draw()
            return
        source = self.state.backdrop()
        if source is None:
            return
        provider = thumbs.provider()
        key: Hashable = (
            ("texture", id(source))
            if isinstance(source, Gdk.Texture)
            else provider.key(source, *SIZE)
        )
        if key == self._key:
            self.queue_draw()  # the Frost dial moved
            return
        self._key = key
        if isinstance(source, Gdk.Texture):
            self._show(source, animate)
            return

        def deliver(texture: Gdk.Texture | None, backdrop: Backdrop = self) -> None:
            if texture is not None and backdrop._key == key:
                backdrop._show(texture, animate)

        texture = provider.request(source, *SIZE, deliver)
        if texture is not None:
            self._show(texture, animate)

    def _show(self, texture: Gdk.Texture, animate: bool) -> None:
        if animate and self._new is not None and self.get_mapped():
            self._old, self._new = self._new, texture
            self._animation.reset()
            self._animation.play()
        else:
            self._old, self._new, self._fade = None, texture, 1.0
            self.queue_draw()

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        return 0, 0, -1, -1

    @staticmethod
    def _draw(
        snapshot: Gtk.Snapshot, texture: Gdk.Texture, width: int, height: int, overscan: float
    ) -> None:
        tw, th = texture.get_width(), texture.get_height()
        scale = max(width / tw, height / th) * overscan
        dw, dh = tw * scale, th * scale
        bounds = Graphene.Rect().init((width - dw) / 2, (height - dh) / 2, dw, dh)
        snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.LINEAR, bounds)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if self._new is None or width <= 0 or height <= 0:
            return
        radius = self.state.frost * MAX_BLUR
        overscan = 1.0 + radius / 250  # hides the soft edges a blur leaves
        snapshot.push_clip(Graphene.Rect().init(0, 0, width, height))
        if radius > 0.5:
            snapshot.push_blur(radius)
        if self._old is not None and self._fade < 1.0:
            self._draw(snapshot, self._old, width, height, overscan)
            snapshot.push_opacity(self._fade)
            self._draw(snapshot, self._new, width, height, overscan)
            snapshot.pop()
        else:
            self._draw(snapshot, self._new, width, height, overscan)
        if radius > 0.5:
            snapshot.pop()  # blur
        snapshot.pop()  # clip
