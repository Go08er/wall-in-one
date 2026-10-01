"""The monitor arrangement for the Displays page, drawn to scale like GNOME Settings.

Each monitor is a real, focusable toggle button (so it gets tooltips, keyboard
focus and a context menu for free) laid out by a small custom container that
scales the compositor's logical layout into the space available.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk

from .. import ui
from ..models import Display

CSS = """
.arrangement {
  background-color: alpha(currentColor, 0.045);
  border: 1px solid alpha(currentColor, 0.07);
  border-radius: 18px;
}
button.monitor-tile {
  padding: 0; min-width: 0; min-height: 0;
  border-radius: 11px;
  border: 3px solid #141418;
  background: #141418;
  box-shadow: 0 1px 2px alpha(black, 0.25), 0 4px 12px alpha(black, 0.18);
  transition: box-shadow 150ms ease-out;
}
button.monitor-tile:hover {
  box-shadow: 0 0 0 2px alpha(var(--accent-bg-color), 0.65), 0 6px 16px alpha(black, 0.25);
}
button.monitor-tile:checked {
  box-shadow: 0 0 0 3px var(--accent-bg-color), 0 6px 18px alpha(black, 0.30);
}
button.monitor-tile:focus-visible { outline-offset: 4px; }
.monitor-caption {
  background-image: linear-gradient(to top, alpha(black, 0.72), alpha(black, 0.0));
  padding: 22px 10px 7px 10px;
  color: white;
}
.monitor-caption .connector { font-weight: 800; }
.monitor-caption .model { opacity: 0.82; }
.monitor-caption .detail { font-size: 0.85em; opacity: 0.78; }
.monitor-tile .pill.on-image { background-color: alpha(black, 0.58); }
.monitor-tile .pill.colors { background-color: alpha(black, 0.58); color: white; }
.monitor-tile .paused-veil { background-color: alpha(black, 0.28); }
.link-badge {
  padding: 7px;
  background-color: var(--accent-bg-color);
  color: var(--accent-fg-color);
  border-radius: 999px;
  box-shadow: 0 0 0 3px var(--window-bg-color), 0 2px 6px alpha(black, 0.30);
}
.arrangement.compact .link-badge { padding: 4px; }
.arrangement.compact .monitor-caption { padding: 14px 6px 5px 6px; }
.identify-number {
  font-size: 34px; font-weight: 800;
  color: white;
  background-color: alpha(black, 0.62);
  border: 2px solid alpha(white, 0.85);
  border-radius: 999px;
  min-width: 62px; min-height: 62px;
}
"""


class LinkGlyph(Gtk.Widget):
    """A chain-link glyph in the current text color (Adwaita has no chain icon)."""

    def __init__(self, size: int = 16) -> None:
        super().__init__()
        self._size = size
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_can_target(False)

    def do_measure(self, _orientation: Gtk.Orientation, _for_size: int):
        return self._size, self._size, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        size = self._size
        cr = snapshot.append_cairo(Graphene.Rect().init(0, 0, size, size))
        color = self.get_color()
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        cr.set_line_width(size * 0.13)
        length, radius = size * 0.50, size * 0.16
        for cx, cy in ((0.355, 0.645), (0.645, 0.355)):
            cr.save()
            cr.translate(cx * size, cy * size)
            cr.rotate(-math.pi / 4)
            half = length / 2 - radius
            cr.new_sub_path()
            cr.arc(half, 0, radius, -math.pi / 2, math.pi / 2)
            cr.arc(-half, 0, radius, math.pi / 2, 3 * math.pi / 2)
            cr.close_path()
            cr.restore()
            cr.stroke()


@dataclass
class TileInfo:
    """Everything a tile shows; the page computes it from state."""

    texture: Gdk.Texture
    wallpaper: str
    source: str  # playlist name, or "your pick"
    status: str  # "Playing", "Paused", ...
    status_icon: str
    paused: bool
    colors: bool  # this display drives the desktop colors


class MonitorTile(Gtk.ToggleButton):
    def __init__(self, display: Display, number: int) -> None:
        super().__init__()
        self.display = display
        self.add_css_class("monitor-tile")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._compact = False

        overlay = Gtk.Overlay()
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        overlay.set_child(self.picture)

        self._veil = Gtk.Box()
        self._veil.add_css_class("paused-veil")
        self._veil.set_can_target(False)
        overlay.add_overlay(self._veil)

        self._top = Gtk.Box(spacing=4, valign=Gtk.Align.START, margin_top=7, margin_start=7, margin_end=7)
        self._top.set_can_target(False)
        overlay.add_overlay(self._top)

        caption = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, valign=Gtk.Align.END)
        caption.add_css_class("monitor-caption")
        caption.set_can_target(False)
        names = Gtk.Box(spacing=6)
        self._connector = Gtk.Label(label=display.connector, xalign=0)
        self._connector.add_css_class("connector")
        self._model = Gtk.Label(label=display.model, xalign=0, hexpand=True, ellipsize=3)
        self._model.add_css_class("model")
        names.append(self._connector)
        names.append(self._model)
        self._detail = Gtk.Label(xalign=0, ellipsize=3)
        self._detail.add_css_class("detail")
        caption.append(names)
        caption.append(self._detail)
        overlay.add_overlay(caption)

        self._number = Gtk.Label(label=str(number), halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._number.add_css_class("identify-number")
        self._number.set_visible(False)
        self._number.set_can_target(False)
        overlay.add_overlay(self._number)

        self.set_child(overlay)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{display.connector}, {display.model}"])

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self._model.set_visible(not compact)
        self._detail.set_visible(not compact)
        # Centered, so a link badge on the seam never covers the name.
        self._connector.set_hexpand(compact)
        self._connector.set_xalign(0.5 if compact else 0)

    def set_identify(self, shown: bool) -> None:
        self._number.set_visible(shown)

    def update(self, info: TileInfo) -> None:
        self.picture.set_paintable(info.texture)
        self._veil.set_visible(info.paused)
        self._detail.set_label(f"{info.wallpaper} · {info.source}")
        child = self._top.get_first_child()
        while child:
            self._top.remove(child)
            child = self._top.get_first_child()
        status = ui.pill("" if self._compact else info.status, info.status_icon, "on-image")
        status.set_tooltip_text(info.status)
        self._top.append(status)
        self._top.append(Gtk.Box(hexpand=True))
        if info.colors:
            colors = ui.pill("" if self._compact else "Colors", "preferences-color-symbolic", "colors")
            self._top.append(colors)
        if self.display.primary:
            self._top.append(ui.pill("" if self._compact else "Primary", "starred-symbolic", "on-image"))
        bits = [f"{self.display.connector} · {self.display.model}", f"{info.wallpaper} · {info.source}", info.status]
        if info.colors:
            bits.append("Desktop colors follow this display")
        if self.display.primary:
            bits.append("Primary display")
        self.set_tooltip_text("\n".join(bits))


class Arrangement(Gtk.Widget):
    """Lays monitors out at their logical positions, scaled to fit, centered."""

    PAD = 26
    GAP = 4  # inset per side, so touching monitors show a slim seam
    MAX_HEIGHT = 330

    def __init__(self, on_select: Callable[[str], None], on_menu: Callable[[MonitorTile, float, float], None]) -> None:
        super().__init__()
        self.add_css_class("arrangement")
        self.set_hexpand(True)
        self._on_select = on_select
        self._on_menu = on_menu
        self.tiles: dict[str, MonitorTile] = {}
        self._displays: list[Display] = []
        self._links: list[tuple[Gtk.Widget, float, float]] = []
        self._linked = False
        self._selecting = False

    # -- content -------------------------------------------------------------------
    def set_displays(self, displays: list[Display]) -> None:
        for tile in self.tiles.values():
            tile.unparent()
        self.tiles = {}
        self._displays = list(displays)
        group: MonitorTile | None = None
        for number, display in enumerate(displays, start=1):
            tile = MonitorTile(display, number)
            if group is None:
                group = tile
            else:
                tile.set_group(group)
            tile.connect("toggled", self._toggled)
            secondary = Gtk.GestureClick(button=3)
            secondary.connect("pressed", lambda _g, _n, x, y, t=tile: self._on_menu(t, x, y))
            tile.add_controller(secondary)
            long_press = Gtk.GestureLongPress()
            long_press.connect("pressed", lambda _g, x, y, t=tile: self._on_menu(t, x, y))
            tile.add_controller(long_press)
            menu_key = Gtk.EventControllerKey()
            menu_key.connect("key-pressed", self._menu_key, tile)
            tile.add_controller(menu_key)
            tile.set_parent(self)
            self.tiles[display.connector] = tile
        self._relink()
        self.queue_resize()

    def _menu_key(self, _controller, keyval, _code, state, tile: MonitorTile) -> bool:
        if keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK):
            self._on_menu(tile, tile.get_width() / 2, tile.get_height() / 2)
            return True
        return False

    def set_compact(self, compact: bool) -> None:
        if compact:
            self.add_css_class("compact")
        else:
            self.remove_css_class("compact")
        for tile in self.tiles.values():
            tile.set_compact(compact)

    def select(self, connector: str) -> None:
        tile = self.tiles.get(connector)
        if tile and not tile.get_active():
            self._selecting = True
            tile.set_active(True)
            self._selecting = False

    def _toggled(self, tile: MonitorTile) -> None:
        if tile.get_active() and not self._selecting:
            self._on_select(tile.display.connector)

    def set_linked(self, linked: bool) -> None:
        self._linked = linked
        self._relink()

    def _relink(self) -> None:
        for widget, _x, _y in self._links:
            widget.unparent()
        self._links = []
        if not self._linked:
            self.queue_allocate()
            return
        for x, y in _junctions(self._displays):
            badge = Gtk.Box()
            badge.append(LinkGlyph(16))
            badge.add_css_class("link-badge")
            badge.set_tooltip_text("Linked: the same wallpaper on every display")
            badge.set_parent(self)  # after the tiles, so it is drawn on top
            self._links.append((badge, x, y))
        self.queue_allocate()

    # -- layout --------------------------------------------------------------------
    def _bounds(self) -> tuple[int, int, int, int]:
        if not self._displays:
            return 0, 0, 1920, 1080
        left = min(d.x for d in self._displays)
        top = min(d.y for d in self._displays)
        right = max(d.x + d.width for d in self._displays)
        bottom = max(d.y + d.height for d in self._displays)
        return left, top, right - left, bottom - top

    def _scale(self, width: float, height: float | None) -> float:
        _x, _y, bw, bh = self._bounds()
        scale = (width - 2 * self.PAD) / bw
        scale = min(scale, (self.MAX_HEIGHT - 2 * self.PAD) / bh)
        if height is not None:
            scale = min(scale, (height - 2 * self.PAD) / bh)
        return max(scale, 0.01)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation: Gtk.Orientation, for_size: int):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 240, 720, -1, -1
        width = for_size if for_size > 0 else 720
        _x, _y, _bw, bh = self._bounds()
        height = int(bh * self._scale(width, None) + 2 * self.PAD)
        return height, height, -1, -1

    def do_size_allocate(self, width: int, height: int, _baseline: int) -> None:
        left, top, bw, bh = self._bounds()
        scale = self._scale(width, height)
        origin_x = (width - bw * scale) / 2
        origin_y = (height - bh * scale) / 2
        gap = self.GAP
        for display in self._displays:
            tile = self.tiles[display.connector]
            tile.measure(Gtk.Orientation.HORIZONTAL, -1)
            x = origin_x + (display.x - left) * scale + gap
            y = origin_y + (display.y - top) * scale + gap
            w = max(8, round(display.width * scale - 2 * gap))
            h = max(8, round(display.height * scale - 2 * gap))
            transform = Gsk.Transform().translate(Graphene.Point().init(round(x), round(y)))
            tile.allocate(w, h, -1, transform)
        for badge, lx, ly in self._links:
            _min, natural = badge.get_preferred_size()
            bw_, bh_ = natural.width, natural.height
            x = origin_x + (lx - left) * scale - bw_ / 2
            y = origin_y + (ly - top) * scale - bh_ / 2
            transform = Gsk.Transform().translate(Graphene.Point().init(round(x), round(y)))
            badge.allocate(bw_, bh_, -1, transform)

    def do_dispose(self) -> None:
        for tile in list(self.tiles.values()):
            tile.unparent()
        for widget, _x, _y in self._links:
            widget.unparent()
        self.tiles, self._links = {}, []
        Gtk.Widget.do_dispose(self)


def _junctions(displays: list[Display]) -> list[tuple[float, float]]:
    """Midpoints of the shared edges between touching monitors (logical px).
    Two monitors that don't touch get a badge halfway between their centers."""
    points: list[tuple[float, float]] = []
    for index, a in enumerate(displays):
        for b in displays[index + 1 :]:
            for first, second in ((a, b), (b, a)):
                if first.x + first.width == second.x:
                    top, bottom = max(first.y, second.y), min(first.y + first.height, second.y + second.height)
                    if bottom > top:
                        points.append((second.x, (top + bottom) / 2))
                        break
                if first.y + first.height == second.y:
                    left, right = max(first.x, second.x), min(first.x + first.width, second.x + second.width)
                    if right > left:
                        points.append(((left + right) / 2, second.y))
                        break
            else:
                if len(displays) == 2:
                    points.append(
                        ((a.x + a.width / 2 + b.x + b.width / 2) / 2, (a.y + a.height / 2 + b.y + b.height / 2) / 2)
                    )
    return points
