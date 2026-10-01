"""Pieces used by the Settings page: a desktop preview, the default-scheme
picker and the runtime log view. Kept apart so settings.py reads as a list of
settings rather than drawing code."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from .. import data, ui


def _rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def rounded(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# ---------------------------------------------------------------------------
# Desktop preview: the wallpaper with a Noctalia-style bar and window on top
# ---------------------------------------------------------------------------


class DesktopOverlay(Gtk.DrawingArea):
    """Draws a bar, a window and a notification in the given colors
    ([surface, primary, secondary, tertiary, error]) over whatever is below."""

    def __init__(self, colors: list[str]) -> None:
        super().__init__()
        self.colors = colors
        self.set_can_target(False)
        self.set_draw_func(self._draw)

    def update(self, colors: list[str]) -> None:
        self.colors = colors
        self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        if not self.colors:
            return
        s = height / 150

        def color(index: int, alpha: float = 1.0) -> None:
            r, g, b = _rgb(self.colors[index] if index < len(self.colors) else "#888888")
            cr.set_source_rgba(r, g, b, alpha)

        # Bar
        color(0, 0.93)
        rounded(cr, 8 * s, 8 * s, width - 16 * s, 22 * s, 11 * s)
        cr.fill()
        color(1)
        rounded(cr, 14 * s, 12 * s, 54 * s, 14 * s, 7 * s)
        cr.fill()
        for index, x in ((2, 76), (3, 94)):
            color(index)
            cr.arc((x + 5) * s, 19 * s, 5 * s, 0, math.tau)
            cr.fill()
        color(1)
        rounded(cr, width / 2 - 18 * s, 15 * s, 36 * s, 8 * s, 4 * s)
        cr.fill()
        # Window
        wx, wy, ww, wh = width * 0.16, 42 * s, width * 0.5, height - 58 * s
        color(0, 0.95)
        rounded(cr, wx, wy, ww, wh, 10 * s)
        cr.fill()
        color(1)
        rounded(cr, wx + 12 * s, wy + 14 * s, ww * 0.36, 9 * s, 4.5 * s)
        cr.fill()
        for row in range(3):
            color(2 if row != 1 else 3, 0.6)
            rounded(cr, wx + 12 * s, wy + (32 + row * 14) * s, ww * (0.7 - 0.12 * row), 6 * s, 3 * s)
            cr.fill()
        color(1)
        rounded(cr, wx + ww - 64 * s, wy + wh - 26 * s, 52 * s, 16 * s, 8 * s)
        cr.fill()
        # Notification
        nx, ny, nw = width * 0.70, 42 * s, width * 0.26
        color(0, 0.95)
        rounded(cr, nx, ny, nw, 36 * s, 8 * s)
        cr.fill()
        color(3)
        cr.arc(nx + 16 * s, ny + 18 * s, 7 * s, 0, math.tau)
        cr.fill()
        color(2, 0.6)
        rounded(cr, nx + 30 * s, ny + 12 * s, nw - 42 * s, 5 * s, 2.5 * s)
        cr.fill()
        color(2, 0.4)
        rounded(cr, nx + 30 * s, ny + 21 * s, (nw - 42 * s) * 0.6, 5 * s, 2.5 * s)
        cr.fill()


def desktop_preview(
    wallpaper: data.Wallpaper, colors: list[str], radius: float = 14
) -> tuple[Gtk.Widget, DesktopOverlay]:
    overlay = Gtk.Overlay()
    overlay.set_child(ui.Thumb(wallpaper.thumb(960, 540), 560, 315, radius=radius, fill=True))
    drawing = DesktopOverlay(colors)
    overlay.add_overlay(drawing)
    overlay.set_overflow(Gtk.Overflow.HIDDEN)
    return overlay, drawing


# ---------------------------------------------------------------------------
# Default color scheme picker
# ---------------------------------------------------------------------------


class SchemeDialog(Adw.Dialog):
    """Pick the scheme inherited by wallpapers that don't choose their own.
    Previewed on the wallpaper that is on screen now."""

    def __init__(self, state, on_pick: Callable[[str], None]) -> None:
        super().__init__(title="Default color scheme", content_width=620, content_height=720)
        self.state = state
        self._on_pick = on_pick
        connector = (
            state.color_display
            if state.display_mode == "independent" and state.color_display in state.current
            else state.connectors()[0]
        )
        self.wallpaper = state.wallpaper(state.current[connector])
        self._dark = state.dark
        self._cards: dict[str, Gtk.Button] = {}

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_start=18,
            margin_end=18,
            margin_top=6,
            margin_bottom=24,
        )

        preview, self._overlay = desktop_preview(self.wallpaper, self._colors(state.default_scheme))
        box.append(preview)

        caption = Gtk.Box(spacing=12)
        text = Gtk.Label(label=f"Previewed on “{self.wallpaper.name}”", xalign=0, hexpand=True, ellipsize=3)
        text.add_css_class("dimmed")
        caption.append(text)
        mode = Adw.ToggleGroup()
        mode.add(Adw.Toggle(name="dark", label="Dark"))
        mode.add(Adw.Toggle(name="light", label="Light"))
        mode.set_active_name("dark" if self._dark else "light")
        mode.connect("notify::active-name", self._on_mode)
        caption.append(mode)
        box.append(caption)

        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            min_children_per_line=2,
            max_children_per_line=3,
            column_spacing=8,
            row_spacing=8,
            margin_top=4,
        )
        for key, name, description in data.SCHEMES:
            button = Gtk.Button()
            button.add_css_class("choice-card")
            button.add_css_class("flat")
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            swatches = ui.Swatches(self._colors(key), size=14)
            swatches.set_halign(Gtk.Align.START)
            content.append(swatches)
            label = Gtk.Label(label=name, xalign=0)
            label.add_css_class("heading")
            content.append(label)
            sub = Gtk.Label(label=description, xalign=0, ellipsize=3)
            sub.add_css_class("dimmed")
            sub.add_css_class("caption")
            content.append(sub)
            button.set_child(content)
            button.set_tooltip_text(description)
            button._swatches = swatches  # type: ignore[attr-defined]
            button.connect("clicked", lambda _b, k=key: self.pick(k))
            flow.append(button)
            self._cards[key] = button
        box.append(flow)
        note = ui.dim("Wallpapers with their own scheme keep it.")
        note.add_css_class("caption")
        box.append(note)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(Adw.Clamp(maximum_size=640, child=box))
        view.set_content(scroller)
        self.set_child(view)
        self._mark(state.default_scheme)

    def _colors(self, scheme: str) -> list[str]:
        return data.scheme_swatches(self.wallpaper, scheme, self._dark)[:5]

    def _mark(self, scheme: str) -> None:
        for key, card in self._cards.items():
            if key == scheme:
                card.add_css_class("selected")
            else:
                card.remove_css_class("selected")

    def _on_mode(self, group: Adw.ToggleGroup, _param) -> None:
        self._dark = group.get_active_name() == "dark"
        self._overlay.update(self._colors(self.state.default_scheme))
        for key, card in self._cards.items():
            card._swatches.set_colors(self._colors(key))  # type: ignore[attr-defined]

    def pick(self, scheme: str) -> None:
        self._mark(scheme)
        self._overlay.update(self._colors(scheme))
        self._on_pick(scheme)


# ---------------------------------------------------------------------------
# Runtime log
# ---------------------------------------------------------------------------

_LINE = re.compile(r"^(\d\d:\d\d:\d\d)\s+(\S+)\s+(.*)$")
# Absolute or home-relative paths with at least one separator after the root.
_PATH = re.compile(r"(?<![\w>])(?:~/|/)[\w.\-]+(?:/[\w.\-]+)*")
MAX_LINES = 200


@dataclass
class LogLine:
    time: str
    category: str
    message: str

    @property
    def level(self) -> str:
        text = self.message.lower()
        if any(word in text for word in ("exited", "failed", "crashed", "error")):
            return "error"
        if any(word in text for word in ("skipped", "not found", "missing")):
            return "warning"
        return "info"


def parse_log(lines: list[str]) -> list[LogLine]:
    parsed = []
    for line in lines:
        match = _LINE.match(line)
        if match:
            parsed.append(LogLine(*match.groups()))
    parsed.sort(key=lambda entry: entry.time)  # stable: keeps order within a second
    return parsed[-MAX_LINES:]


def hide_paths(text: str) -> str:
    return _PATH.sub("‹path›", text)


class LogView(Gtk.Box):
    """A bounded, read-only monospace list. Newest last, scrolled to the end."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("st-log")
        self._lines = parse_log(lines)
        self._hide = True
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=6, margin_bottom=6)
        # A fixed, modest window onto the log: enough to read, never the whole page.
        height = min(236, 20 * len(self._lines) + 12)
        self._scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=height, max_content_height=height
        )
        self._scroller.set_child(self._list)
        self.append(self._scroller)
        self._render()
        self.connect("map", lambda *_: GLib.idle_add(self._scroll_end))

    @property
    def problems(self) -> int:
        return sum(1 for line in self._lines if line.level != "info")

    def __len__(self) -> int:
        return len(self._lines)

    def set_hide_paths(self, hide: bool) -> None:
        self._hide = hide
        self._render()

    def text(self) -> str:
        rows = [f"{line.time}  {line.category:<9} {line.message}" for line in self._lines]
        joined = "\n".join(rows)
        return hide_paths(joined) if self._hide else joined

    def _scroll_end(self) -> bool:
        adjustment = self._scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        return False

    def _render(self) -> None:
        child = self._list.get_first_child()
        while child:
            self._list.remove(child)
            child = self._list.get_first_child()
        for line in self._lines:
            row = Gtk.Box(spacing=10)
            row.add_css_class("st-log-row")
            row.add_css_class(f"level-{line.level}")
            time = Gtk.Label(label=line.time, xalign=0, valign=Gtk.Align.START)
            time.add_css_class("st-log-time")
            category = Gtk.Label(label=line.category, xalign=0, width_chars=8, valign=Gtk.Align.START)
            category.add_css_class("st-log-cat")
            category.add_css_class(f"cat-{line.category}")
            message = Gtk.Label(
                label=hide_paths(line.message) if self._hide else line.message,
                xalign=0,
                wrap=True,
                hexpand=True,
                selectable=True,
                valign=Gtk.Align.START,
            )
            message.set_wrap_mode(2)  # Pango.WrapMode.WORD_CHAR
            message.add_css_class("st-log-message")
            message.set_focusable(False)
            row.append(time)
            row.append(category)
            row.append(message)
            if line.level != "info":
                icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                icon.set_valign(Gtk.Align.START)
                icon.add_css_class("st-log-icon")
                icon.set_tooltip_text("Problem" if line.level == "error" else "Warning")
                row.append(icon)
            self._list.append(row)
