"""The demo's colors: the simulated (or real, read-only) Noctalia palette.

This is the prototype's own color pipeline and stays here. In the app the
palette comes from its own resolution and stylesheet; only the *glass
layering* is shared, from the app's CSS render
(``wall_in_one.theme.css.glass_layers``), and so is the frosted backdrop
(``wall_in_one.ui.next.backdrop``), which the shell draws.

* **Colors** follow what the desktop shows. With the real Noctalia attached
  (``noctalia_live``) its palette tokens are mapped exactly as the real app's
  ``theme/css.py`` maps them; otherwise the simulated desktop's palette
  (``AppState.desktop_swatches``) tints the accent and every surface.
* The stylesheet sits at USER + 1 priority, like the real app's: Noctalia's
  own ``~/.config/gtk-4.0/noctalia.css`` is loaded at USER priority, defines
  opaque ``:root`` colors, and is only read when GTK starts, so anything
  lower would lose to stale, opaque colors.
* **Translucent** and **Frosted** add the app's glass layers over those
  surfaces, scoped to ``window.wio-glass``.

:class:`Look` listens to every topic that can change any of this and rebuilds
the stylesheet once, before the next frame, so colors never lag a repaint.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from wall_in_one.theme.css import glass_layers, on_background

from . import data

__all__ = ["NIRI_RULE", "Look", "glass_layers", "on_background", "stylesheet"]

#: Compositor rule that blurs behind Wall-in-One on niri (copied from Settings).
NIRI_RULE = """window-rule {
    match app-id=r"^dev\\.goober\\.WallInOne$"
    background-effect {
        blur true
    }
}"""

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
