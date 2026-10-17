"""Shared widgets: the ones the app ships, from ``wall_in_one.ui.next.widgets``.

The prototype's cards, card grid, pictures and badges *are* the app's: this
module only re-exports them for the prototype's pages and adds what is
demo-only, the prototype's own icon folder.
"""

from __future__ import annotations

import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from wall_in_one.ui.next import style
from wall_in_one.ui.next.widgets import (
    CORNER_RESERVE,
    CardGrid,
    PickBadge,
    Swatches,
    Thumb,
    WallpaperCard,
    add_css,
    card_colors,
    clamp,
    color_summary,
    color_tooltip,
    dim,
    hang_on_corner,
    heading,
    icon_button,
    kind_badge,
    menu_from,
    pill,
    rgba,
    select_toggle,
    texture_picture,
    thumbnail,
)

from . import thumbs

__all__ = [
    "CORNER_RESERVE",
    "CardGrid",
    "PickBadge",
    "Swatches",
    "Thumb",
    "WallpaperCard",
    "add_css",
    "card_colors",
    "clamp",
    "color_summary",
    "color_tooltip",
    "dim",
    "hang_on_corner",
    "heading",
    "icon_button",
    "kind_badge",
    "load_css",
    "menu_from",
    "pill",
    "rgba",
    "select_toggle",
    "texture_picture",
    "thumbnail",
]


def load_css() -> None:
    """The app's widget styles, the demo's thumbnails and its own icons."""
    display = Gdk.Display.get_default()
    style.install(display)
    thumbs.install()
    # The prototype's own symbolic icons (e.g. wio-select-multiple), whatever the icon theme.
    Gtk.IconTheme.get_for_display(display).add_search_path(str(Path(__file__).with_name("icons")))


def finish_page_change(window: Gtk.Window, seconds: float = 5.0) -> bool:
    """Run the main loop until the shell's page crossfade has ended.

    The old page stays mapped, with any menu it has open, for the 120 ms of
    the crossfade. Wayland allows a new menu only on top of the last one, so
    a scene that opens its menu inside that window is refused by GTK ("Tried
    to map a grabbing popup with a non-top most parent"). A person's next
    click comes after the crossfade; scenes wait for it here. True when it
    has ended, False after ``seconds``.
    """
    stack = window.stack  # type: ignore[attr-defined]
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while stack.get_transition_running() and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)
    return not stack.get_transition_running()
