"""The window's colors and glass styles, applied from one place.

* **Colors** follow what the desktop shows. With the real Noctalia attached
  (``noctalia_live``) its palette tokens are mapped exactly as the real app's
  ``theme/css.py`` maps them; otherwise the simulated desktop's palette
  (``AppState.desktop_swatches``) tints the accent and every surface.
* The stylesheet sits at USER + 1 priority, like the real app's: Noctalia's
  own ``~/.config/gtk-4.0/noctalia.css`` is loaded at USER priority, defines
  opaque ``:root`` colors, and is only read when GTK starts, so anything
  lower would lose to stale, opaque colors.
* **Translucent** mirrors the real app's opacity setting: the window, view,
  header and sidebar backgrounds get an alpha so the compositor shows the
  desktop behind. On niri, a ``background-effect { blur true }`` window rule
  turns that into real frosted glass.
* **Frosted** works on any compositor: the app draws its own current
  wallpaper behind those panels, blurred by the *Frost* dial, and crossfades
  when the wallpaper changes. The panels' *opacity* dial decides how much of
  their palette color covers it; nothing else is painted on top.

:class:`Look` listens to every topic that can change any of this and rebuilds
the stylesheet once, before the next frame, so colors never lag a repaint.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk

from . import art, data

#: Compositor rule that blurs behind Wall-in-One on niri (copied from Settings).
NIRI_RULE = """window-rule {
    match app-id=r"^dev\\.goober\\.WallInOne$"
    background-effect {
        blur true
    }
}"""

#: Strongest in-app blur, in pixels, at Frost = 100%.
MAX_BLUR = 60

# libadwaita's own surfaces, used for glass when the app isn't following Noctalia.
_ADWAITA = {
    True: {
        "window": "#222226", "view": "#1d1d20", "headerbar": "#2e2e32", "sidebar": "#2e2e32",
        "secondary-sidebar": "#28282c", "card": "#343439",
    },
    False: {
        "window": "#fafafb", "view": "#ffffff", "headerbar": "#ffffff", "sidebar": "#ebebed",
        "secondary-sidebar": "#f3f3f5", "card": "#ffffff",
    },
}  # fmt: skip

# libadwaita variable <- Noctalia token (then fallbacks), as the real app's css.py maps them.
_SURFACE_TOKENS = {
    "window": ("surface",),
    "view": ("surface_container_low", "surface"),
    "headerbar": ("surface_container", "surface"),
    "sidebar": ("surface_container_low", "surface"),
    "secondary-sidebar": ("surface_container_lowest", "surface"),
    "card": ("surface_container", "surface_variant"),
    "dialog": ("surface_container_high", "surface_variant"),
    "popover": ("surface_container_high", "surface_variant"),
}
_TOKENS = {
    "accent-color": ("primary",),
    "accent-bg-color": ("primary",),
    "accent-fg-color": ("on_primary",),
    "destructive-color": ("error",),
    "destructive-bg-color": ("error",),
    "destructive-fg-color": ("on_error",),
    "success-color": ("tertiary", "primary"),
    "success-bg-color": ("tertiary", "primary"),
    "success-fg-color": ("on_tertiary", "on_primary"),
    "warning-color": ("secondary", "primary"),
    "warning-bg-color": ("secondary", "primary"),
    "warning-fg-color": ("on_secondary", "on_primary"),
    "error-color": ("error",),
    "error-bg-color": ("error",),
    "error-fg-color": ("on_error",),
    "headerbar-border-color": ("outline_variant", "outline"),
    "sidebar-border-color": ("outline_variant", "outline"),
    **{f"{name}-fg-color": ("on_surface",) for name in _SURFACE_TOKENS},
}


def _rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha:.3f})"


def stylesheet(state) -> str:
    """Accent, surfaces and glass for the current state, as one stylesheet."""
    css = []
    tokens = state.live_tokens() if state.follow_noctalia_colors else None
    if tokens:
        # The real palette, token for token.
        def pick(names: tuple[str, ...]) -> str:
            return next((tokens[name] for name in names if name in tokens), tokens["surface"])

        surfaces = {name: pick(names) for name, names in _SURFACE_TOKENS.items()}
        css.append(":root { " + "; ".join(f"--{var}: {pick(names)}" for var, names in _TOKENS.items()) + "; }")
    else:
        swatches = state.desktop_swatches() if state.follow_noctalia_colors else []
        accent = data.accent_from(swatches, state.dark)
        if accent:
            background, text = accent
            css.append(
                f":root {{ --accent-bg-color: {background}; --accent-color: {text}; --accent-fg-color: white; }}"
            )
        surfaces = data.surfaces_from(swatches, state.dark)
    if surfaces:
        values = "; ".join(f"--{name}-bg-color: {color}" for name, color in surfaces.items())
        css.append(
            f":root {{ {values}; --headerbar-backdrop-color: {surfaces['headerbar']};"
            f" --sidebar-backdrop-color: {surfaces['sidebar']}; }}"
        )

    style = state.window_style
    if style == "solid":
        return "\n".join(css)
    base = surfaces or _ADWAITA[state.dark]
    css.append(glass_layers(base, state.background_alpha, state.panel_alpha))
    return "\n".join(css)


def on_background(panel: float, background: float) -> float:
    """CSS alpha for an element that sits on the page background, so that the
    two layers together show ``panel``. An element can't be clearer than the
    background under it, so below ``background`` it simply adds nothing."""
    if panel <= background:
        return 0.0
    return (panel - background) / (1.0 - background) if background < 1.0 else 1.0


def glass_layers(base: dict[str, str], background: float, panel: float) -> str:
    """One painted layer per region, so each dial shows exactly what it says.

    The window itself is clear. Regions that sit straight on the desktop (or
    the frosted backdrop) paint once: the sidebar, the content header and the
    player bar at ``panel``; the page area under the content at ``background``.
    Elements on the page (cards, lists, the inspector) are solved with
    :func:`on_background` so the stack still shows ``panel``. Stacking layers
    at the same alpha is what made 75% look solid: 0.75 over 0.75 is 0.94.
    """
    on_page = on_background(panel, background)
    sidebar = _rgba(base["sidebar"], panel)
    header = _rgba(base["headerbar"], panel)
    values = [
        f"--window-bg-color: {_rgba(base['window'], background)}",
        f"--view-bg-color: {_rgba(base['view'], on_page)}",
        f"--headerbar-bg-color: {header}",
        f"--headerbar-backdrop-color: {header}",
        f"--sidebar-bg-color: {sidebar}",
        f"--sidebar-backdrop-color: {sidebar}",
        f"--secondary-sidebar-bg-color: {_rgba(base['secondary-sidebar'], on_page)}",
        f"--card-bg-color: {_rgba(base['card'], on_page)}",
    ]
    solid = [f"--{name}-bg-color: {base[name]}" for name in ("window", "view", "card", "headerbar", "sidebar")]
    solid += [f"--secondary-sidebar-bg-color: {base['secondary-sidebar']}"]
    solid += [f"--headerbar-backdrop-color: {base['headerbar']}", f"--sidebar-backdrop-color: {base['sidebar']}"]
    content = "window.wio-glass navigation-view-page.wio-content-page > toolbarview"
    return f"""
window.wio-glass, window.wio-glass.background, window.wio-glass.background.csd {{
  background-color: transparent;
}}
window.wio-glass {{ {"; ".join(values)}; }}
/* Straight on the desktop: one layer each. The shell's two pages are tagged,
   so this holds both side by side and when the split view collapses into a
   navigation view (which would otherwise paint its own page backgrounds). */
window.wio-glass navigation-split-view > widget.sidebar-pane,
window.wio-glass navigation-split-view > widget.content-pane,
window.wio-glass navigation-split-view navigation-view-page {{ background-color: transparent; }}
window.wio-glass navigation-view-page.wio-sidebar-page {{ background-color: {sidebar}; }}
{content} > revealer.top-bar {{ background-color: {header}; }}
{content} > stack {{ background-color: {_rgba(base["window"], background)}; }}
/* On the page: the inspector pane paints once, its contents stay clear. When
   it floats over the grid in a narrow window it is solid, like a popover. */
window.wio-glass overlay-split-view > widget.sidebar-pane {{
  background-color: {_rgba(base["secondary-sidebar"], on_page)};
}}
window.wio-glass overlay-split-view > widget.background {{ background-color: {base["secondary-sidebar"]}; }}
window.wio-glass .inspector {{ background-color: transparent; }}
window.wio-glass banner > revealer > widget {{ background-color: alpha(currentColor, 0.07); }}
/* Dialogs and popovers float above everything: back to solid surfaces. */
window.wio-glass dialog, window.wio-glass popover {{ {"; ".join(solid)}; }}
"""


class Look:
    """Keeps one window's colors and glass in step with the state."""

    TOPICS = frozenset({"now", "library", "settings", "displays", "theme", "appearance"})
    _provider: Gtk.CssProvider | None = None

    def __init__(self, window: Gtk.Window, state) -> None:
        self.window = window
        self.state = state
        self._css: str | None = None
        self._queued = 0
        if Look._provider is None:
            Look._provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), Look._provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1
            )
        state.connect("changed", self._on_changed)
        self.apply()

    def _on_changed(self, _state, topic: str) -> None:
        # A change often arrives as several topics; rebuild once. HIGH_IDLE runs
        # before GTK's layout and paint, so the next frame already has the colors.
        if topic in self.TOPICS and not self._queued:
            self._queued = GLib.idle_add(self._flush, priority=GLib.PRIORITY_HIGH_IDLE)

    def _flush(self) -> bool:
        self._queued = 0
        self.apply()
        return False

    def apply(self) -> None:
        css = stylesheet(self.state)
        if css != self._css:  # reloading restyles every widget; skip when nothing changed
            self._css = css
            Look._provider.load_from_string(css)
        if self.state.window_style == "solid":
            self.window.remove_css_class("wio-glass")
        else:
            self.window.add_css_class("wio-glass")


class Backdrop(Gtk.Widget):
    """The current wallpaper, blurred by the Frost dial, drawn behind the whole window."""

    def __init__(self, state) -> None:
        super().__init__()
        self.state = state
        self.set_can_target(False)
        self._key: tuple | None = None
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

    def _on_changed(self, _state, topic: str) -> None:
        if topic in ("now", "displays", "theme", "appearance", "settings"):
            self.refresh(animate=topic in ("now", "settings"))

    def refresh(self, animate: bool = True) -> None:
        self.set_visible(self.state.window_style == "frosted")
        live = self.state.live if self.state.live_colors() else None
        if live is not None and live.wallpaper is not None:
            # Following the real desktop: blur its actual wallpaper.
            key, texture = ("live", live.wallpaper_path), live.wallpaper
        else:
            wallpaper = self.state.color_wallpaper()
            key, texture = wallpaper.key, None
        if key == self._key:
            self.queue_draw()  # the Frost dial moved
            return
        self._key = key
        # Big enough to stay a picture when Frost is near 0; cached per wallpaper.
        texture = texture or art.texture(*wallpaper.key, 384, 216)
        if animate and self._new is not None and self.get_mapped():
            self._old, self._new = self._new, texture
            self._animation.reset()
            self._animation.play()
        else:
            self._old, self._new, self._fade = None, texture, 1.0
            self.queue_draw()

    def do_measure(self, _orientation, _for_size):
        return 0, 0, -1, -1

    def _draw(self, snapshot: Gtk.Snapshot, texture: Gdk.Texture, width: int, height: int, overscan: float) -> None:
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
