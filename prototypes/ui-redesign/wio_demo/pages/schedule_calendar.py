"""The week calendar (Mon–Sun × 24 h) and the small week preview used by the editor.

Both are plain cairo drawings. The calendar draws what *plays*: each block is
the rule that wins there; a hatched edge marks a rule it overrides. Click a
block to edit its rule, drag anywhere to add a rule for that day and time.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from dataclasses import dataclass
from html import escape

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo

from ..catalog import DAYS, DAYS_LONG, MONTHS
from ..models import Rule
from . import schedule_model as model

SNAP = 15  # minutes
NOW_RED = (0.88, 0.11, 0.14)  # calendars draw "now" in red; the accent may match a block


def _rounded(
    cr, x0: float, y0: float, x1: float, y1: float, radius: float, top: bool = True, bottom: bool = True
) -> None:
    """Rounded rectangle; ``top``/``bottom`` False keeps that edge square (a continuation)."""
    r = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    rt, rb = (r if top else 0), (r if bottom else 0)
    cr.new_sub_path()
    cr.move_to(x0 + rt, y0)
    cr.line_to(x1 - rt, y0)
    if rt:
        cr.arc(x1 - rt, y0 + rt, rt, -math.pi / 2, 0)
    cr.line_to(x1, y1 - rb)
    if rb:
        cr.arc(x1 - rb, y1 - rb, rb, 0, math.pi / 2)
    cr.line_to(x0 + rb, y1)
    if rb:
        cr.arc(x0 + rb, y1 - rb, rb, math.pi / 2, math.pi)
    cr.line_to(x0, y0 + rt)
    if rt:
        cr.arc(x0 + rt, y0 + rt, rt, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _hatch(cr, x0: float, y0: float, x1: float, y1: float, color, alpha: float, gap: float = 5) -> None:
    cr.save()
    cr.rectangle(x0, y0, x1 - x0, y1 - y0)
    cr.clip()
    cr.set_source_rgba(*color, alpha)
    cr.set_line_width(1.6)
    height = y1 - y0
    start = x0 - height
    offset = y0 % gap  # keep stripes continuous across neighboring pieces
    x = start - offset
    while x < x1 + gap:
        cr.move_to(x, y1)
        cr.line_to(x + height, y0)
        x += gap
    cr.stroke()
    cr.restore()


@dataclass
class Block:
    day: int
    start: int
    end: int
    rule: Rule | None


@dataclass
class Hit:
    x0: float
    y0: float
    x1: float
    y1: float
    kind: str  # block | fallback | loser | exception
    rule: Rule | None
    day: int
    start: int
    end: int
    over: list[Rule]  # what the block overrides (block) or what overrides it (loser)


class WeekCalendar(Gtk.DrawingArea):
    """Mon–Sun columns × 24 hours. Feed it with ``update(...)``."""

    def __init__(
        self,
        on_edit: Callable[[Rule], None],
        on_create: Callable[[list[int], int, int], None],
        name_of: Callable[[str], str],
    ) -> None:
        super().__init__()
        self._name_of = name_of  # playlist id -> name (state.playlist_name)
        self.add_css_class("week-calendar")
        self.set_content_height(470)
        self.set_content_width(300)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_has_tooltip(True)
        self.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Week schedule. Click a block to edit its rule, drag to add a rule."]
        )
        self._on_edit, self._on_create = on_edit, on_create
        self.spans: list[list[model.Span]] = [[] for _ in range(7)]
        self.exceptions: list[model.DisplayException] = []
        self.monday = dt.date(2026, 9, 28)
        self.today: int | None = None
        self.now_minute = 0
        self.colors: dict[str, str] = {}
        self.fallback_label = ""
        self.fallback_tip = ""
        self.mark_display = ""  # in a one-display view, the connector to tag
        self._hits: list[Hit] = []
        self._hover: Hit | None = None
        self._selection: tuple[int, int, int, int] | None = None  # day0, day1, start, end
        self._press: tuple[float, float] | None = None
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.add_controller(motion)
        self.connect("query-tooltip", self._tooltip)

    # -- data -----------------------------------------------------------------
    def update(
        self,
        spans,
        exceptions,
        monday: dt.date,
        colors: dict[str, str],
        fallback_label: str,
        fallback_tip: str,
        mark_display: str = "",
    ) -> None:
        self.spans, self.exceptions, self.monday = spans, exceptions, monday
        self.colors, self.mark_display = colors, mark_display
        self.fallback_label, self.fallback_tip = fallback_label, fallback_tip
        self._hover = None
        self._hits = []  # rebuilt on the next draw; never point at rules that changed
        self.queue_draw()

    def set_clock(self, today: int | None, now_minute: int) -> None:
        """Move the now line; cheap, for clock ticks."""
        if (today, now_minute) != (self.today, self.now_minute):
            self.today, self.now_minute = today, now_minute
            self.queue_draw()

    def clear_selection(self) -> None:
        self._selection = None
        self.queue_draw()

    def show_selection(self, day0: int, day1: int, start: int, end: int) -> None:
        """Demo hook: show a drag selection as if the pointer were still down."""
        self._selection = (day0, day1, start, end)
        self.queue_draw()

    # -- geometry -------------------------------------------------------------
    def _geometry(self) -> tuple[float, float, float, float]:
        width, height = self.get_width(), self.get_height()
        gutter = 36 if width < 520 else 46
        header = 46
        col_w = (width - gutter - 4) / 7
        hour_h = (height - header - 6) / 24
        return gutter, header, col_w, hour_h

    def _y(self, minute: float) -> float:
        _gutter, header, _col, hour_h = self._geometry()
        return header + minute / 60 * hour_h

    def _locate(self, x: float, y: float) -> tuple[int, int] | None:
        gutter, header, col_w, hour_h = self._geometry()
        if x < gutter or y < header:
            return None
        day = max(0, min(6, int((x - gutter) // col_w)))
        minute = max(0, min(model.DAY_MINUTES, (y - header) / hour_h * 60))
        return day, int(minute)

    # -- drawing --------------------------------------------------------------
    def _colors(self):
        dark = Adw.StyleManager.get_default().get_dark()
        parent = self.get_parent()
        fg_rgba = parent.get_color() if parent else self.get_color()
        fg = (fg_rgba.red, fg_rgba.green, fg_rgba.blue)
        accent_rgba = self.get_color()  # .week-calendar { color: accent }
        accent = (accent_rgba.red, accent_rgba.green, accent_rgba.blue)
        base = (0.13, 0.13, 0.15) if dark else (1.0, 1.0, 1.0)
        return dark, fg, accent, base

    def _layout(self, text: str, size: float, bold: bool = False, width: float | None = None, lines: int = 1):
        layout = self.create_pango_layout(text)
        desc = self.get_pango_context().get_font_description().copy()
        desc.set_size(int(size * Pango.SCALE))
        desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(desc)
        if width is not None:
            layout.set_width(int(max(1, width) * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            if lines > 1:
                layout.set_wrap(Pango.WrapMode.WORD_CHAR)
                layout.set_height(-lines)
        return layout

    def _text(
        self,
        cr,
        text: str,
        x: float,
        y: float,
        color,
        alpha: float,
        size: float,
        bold: bool = False,
        width: float | None = None,
        align: str = "left",
        lines: int = 1,
    ) -> tuple[int, int]:
        layout = self._layout(text, size, bold, width, lines)
        w, h = layout.get_pixel_size()
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2
        cr.move_to(x, y)
        cr.set_source_rgba(*color, alpha)
        PangoCairo.show_layout(cr, layout)
        return w, h

    def _draw(self, _area, cr, width: int, height: int) -> None:
        dark, fg, accent, base = self._colors()
        gutter, header, col_w, hour_h = self._geometry()
        narrow = col_w < 70
        self._hits = []
        top, bottom = header, header + 24 * hour_h

        # Today's column, then the grid.
        if self.today is not None:
            cx = gutter + self.today * col_w
            cr.set_source_rgba(*accent, 0.07 if dark else 0.06)
            _rounded(cr, cx + 1, 4, cx + col_w - 1, bottom + 2, 8)
            cr.fill()
        for hour in range(25):
            y = round(top + hour * hour_h) + 0.5
            cr.set_source_rgba(*fg, 0.13 if hour % 6 == 0 else 0.055)
            cr.set_line_width(1)
            cr.move_to(gutter - (4 if hour % 6 == 0 else 0), y)
            cr.line_to(width - 4, y)
            cr.stroke()
        for column in range(8):
            x = round(gutter + column * col_w) + 0.5
            cr.set_source_rgba(*fg, 0.07)
            cr.move_to(x, top)
            cr.line_to(x, bottom)
            cr.stroke()

        # Hour labels.
        step = 3 if hour_h >= 15 else 6
        for hour in range(step, 24, step):
            y = top + hour * hour_h
            if self.today is not None and abs(y - self._y(self.now_minute)) < 9:
                continue
            label = f"{hour:02d}:00" if not narrow else f"{hour:02d}"
            layout = self._layout(label, 7.5)
            _w, h = layout.get_pixel_size()
            self._text(cr, label, gutter - 7, y - h / 2, fg, 0.55, 7.5, align="right")

        # Day headings.
        for column in range(7):
            day = self.monday + dt.timedelta(days=column)
            cx = gutter + column * col_w + col_w / 2
            is_today = column == self.today
            name = DAYS[column] if col_w >= 44 else DAYS[column][:2]
            self._text(
                cr,
                name.upper(),
                cx,
                6,
                accent if is_today else fg,
                1.0 if is_today else 0.6,
                7,
                bold=True,
                align="center",
            )
            number = str(day.day)
            if is_today:
                cr.set_source_rgba(*accent, 1)
                cr.arc(cx, 30, 11, 0, math.tau)
                cr.fill()
                self._text(cr, number, cx, 21, (1, 1, 1), 1, 9.5, bold=True, align="center")
            else:
                self._text(cr, number, cx, 21, fg, 0.9, 9.5, bold=True, align="center")
            if day.day == 1:  # a new month starts inside this week
                self._text(cr, MONTHS[day.month - 1], cx + 13, 23, fg, 0.5, 7, align="left")

        # Blocks.
        for column, spans in enumerate(self.spans):
            self._draw_day(cr, column, spans, gutter, col_w, base, fg, dark, narrow)
        for exception in self.exceptions:
            self._draw_exception(cr, exception, gutter, col_w, base, fg, dark)

        # Hover outline.
        if self._hover and self._hover.kind != "fallback":
            hit = self._hover
            cr.set_source_rgba(*fg, 0.85)
            cr.set_line_width(2)
            _rounded(cr, hit.x0 - 1, hit.y0 - 1, hit.x1 + 1, hit.y1 + 1, 7)
            cr.stroke()

        # Drag selection.
        if self._selection:
            self._draw_selection(cr, gutter, col_w, accent, fg, base)

        # Now.
        if self.today is not None:
            y = self._y(self.now_minute)
            cx = gutter + self.today * col_w
            cr.set_source_rgba(*base, 1)
            cr.rectangle(cx, y - 2, col_w, 4)
            cr.fill()
            cr.set_source_rgba(*NOW_RED, 1)
            cr.rectangle(cx, y - 1, col_w, 2)
            cr.fill()
            cr.arc(cx + 2, y, 4.5, 0, math.tau)
            cr.fill()
            label = model.hhmm(self.now_minute)
            layout = self._layout(label, 7.5, bold=True)
            w, h = layout.get_pixel_size()
            pill_x1 = gutter - 3
            _rounded(cr, pill_x1 - w - 8, y - h / 2 - 2, pill_x1, y + h / 2 + 2, (h + 4) / 2)
            cr.fill()
            cr.move_to(pill_x1 - w - 4, y - h / 2)
            cr.set_source_rgba(1, 1, 1, 1)
            PangoCairo.show_layout(cr, layout)

    def _blocks(self, spans: list[model.Span]) -> list[Block]:
        blocks: list[Block] = []
        for span in spans:
            last = blocks[-1] if blocks else None
            if last and last.rule is span.rule and last.end == span.start:
                last.end = span.end
            else:
                blocks.append(Block(span.day, span.start, span.end, span.rule))
        return blocks

    def _draw_day(self, cr, column: int, spans, gutter, col_w, base, fg, dark, narrow) -> None:
        x0 = gutter + column * col_w + 3
        x1 = gutter + (column + 1) * col_w - 2
        for block in self._blocks(spans):
            rule = block.rule
            y0, y1 = self._y(block.start) + 1, self._y(block.end) - 1
            if y1 - y0 < 2:
                continue
            if rule is None:
                cr.set_source_rgba(*fg, 0.28)
                cr.set_line_width(1)
                cr.set_dash([3, 3])
                _rounded(cr, x0 + 0.5, y0 + 0.5, x1 - 0.5, y1 - 0.5, 6)
                cr.stroke()
                cr.set_dash([])
                if y1 - y0 >= 18:
                    name = self.fallback_label
                    self._text(
                        cr,
                        name,
                        x0 + 6,
                        y0 + 3,
                        fg,
                        0.6,
                        7.5 if narrow else 8,
                        width=x1 - x0 - 10,
                        lines=2 if y1 - y0 >= 30 else 1,
                    )
                self._hits.append(Hit(x0, y0, x1, y1, "fallback", None, column, block.start, block.end, []))
                continue
            color = model.rgb(self.colors.get(rule.playlist, model.FALLBACK_COLOR))
            continues = model.wraps(rule) and block.end == model.DAY_MINUTES
            tail = model.wraps(rule) and block.start == 0 and model.minutes(rule.end) > 0
            if continues:
                y1 += 1
            if tail:
                y0 -= 1
            _rounded(cr, x0, y0, x1, y1, 6, top=not tail, bottom=not continues)
            cr.set_source_rgba(*base, 1)
            cr.fill_preserve()
            cr.set_source_rgba(*color, 0.34 if dark else 0.22)
            cr.fill_preserve()
            cr.save()
            cr.clip()
            cr.set_source_rgba(*color, 1)
            cr.rectangle(x0, y0, 3, y1 - y0)
            cr.fill()
            # Overridden rules show as a hatched edge in their own color.
            inset = 0
            beaten: list[Rule] = []
            for span in spans:
                if span.rule is not rule or span.end <= block.start or span.start >= block.end:
                    continue
                loser = span.top_loser
                if not loser:
                    continue
                for other in span.losers:
                    if other not in beaten:
                        beaten.append(other)
                ly0, ly1 = self._y(span.start), self._y(span.end)
                lc = model.rgb(self.colors.get(loser.playlist, model.FALLBACK_COLOR))
                cr.set_source_rgba(*base, 1)
                cr.rectangle(x0, ly0, 9, ly1 - ly0)
                cr.fill()
                cr.set_source_rgba(*lc, 0.30 if dark else 0.22)
                cr.rectangle(x0, ly0, 7.5, ly1 - ly0)
                cr.fill()
                _hatch(cr, x0, ly0, x0 + 7.5, ly1, lc, 0.95)
                inset = 7
                self._hits.append(
                    Hit(x0, max(ly0, y0), x0 + 8, min(ly1, y1), "loser", loser, column, span.start, span.end, [rule])
                )
            cr.restore()
            self._hits.append(Hit(x0, y0, x1, y1, "block", rule, column, block.start, block.end, beaten))
            self._label_block(cr, rule, block, x0 + 3 + inset, x1, y0, y1, fg, narrow, column, tail, continues)
        # Keep loser strips above their blocks for hit-testing.
        self._hits.sort(key=lambda hit: hit.kind == "loser")

    def _label_block(
        self, cr, rule: Rule, block: Block, x0, x1, y0, y1, fg, narrow, column: int, tail: bool, continues: bool
    ) -> None:
        height = y1 - y0
        if height < 13:
            return
        limit = x1
        for exception in self.exceptions:
            if exception.day == column and exception.start < block.start + 90 and exception.end > block.start:
                limit = min(limit, self._exception_x(column)[0] - 2)
        width = limit - x0 - 6
        size = 7.5 if narrow else 8.5
        name = self._name_of(rule.playlist)
        lines = 2 if narrow and height >= 26 else 1
        _w, h = self._text(cr, name, x0 + 4, y0 + 2, fg, 0.95, size, bold=True, width=width, lines=lines)
        if rule.display and height >= 44:
            color = model.rgb(self.colors.get(rule.playlist, model.FALLBACK_COLOR))
            self._tag(cr, rule.display, x0 + 3, y1 - 16, width, color, True)
        if height >= 30 and not narrow:
            if tail:
                when = f"until {rule.end}"
            elif continues:
                when = f"{model.hhmm(block.start)}–{rule.end}"
            elif block.start == 0 and block.end == model.DAY_MINUTES:
                when = "All day"
            else:
                when = f"{model.hhmm(block.start)}–{model.hhmm(block.end)}"
            self._text(cr, when, x0 + 4, y0 + 2 + h, fg, 0.7, size - 0.75, width=width)

    def _exception_x(self, column: int) -> tuple[float, float]:
        gutter, _header, col_w, _hour_h = self._geometry()
        x1 = gutter + (column + 1) * col_w - 2
        return gutter + column * col_w + col_w * 0.40, x1

    def _draw_exception(self, cr, exception: model.DisplayException, gutter, col_w, base, fg, dark) -> None:
        rule = exception.rule
        x0, x1 = self._exception_x(exception.day)
        y0, y1 = self._y(exception.start) + 1, self._y(exception.end) - 1
        if y1 - y0 < 2:
            return
        color = model.rgb(self.colors.get(rule.playlist, model.FALLBACK_COLOR))
        # A ring of background separates it from the shared block underneath.
        _rounded(cr, x0 - 2, y0 - 1, x1 + 1, y1 + 1, 7)
        cr.set_source_rgba(*base, 1)
        cr.fill()
        _rounded(cr, x0, y0, x1, y1, 6)
        cr.set_source_rgba(*base, 1)
        cr.fill_preserve()
        if exception.wins:
            cr.set_source_rgba(*color, 0.42 if dark else 0.28)
            cr.fill_preserve()
            cr.save()
            cr.clip()
            cr.set_source_rgba(*color, 1)
            cr.rectangle(x0, y0, 3, y1 - y0)
            cr.fill()
            cr.restore()
        else:
            cr.save()
            cr.clip()
            _hatch(cr, x0, y0, x1, y1, color, 0.8)
            cr.restore()
        cr.new_path()
        width = x1 - x0 - 8
        if y1 - y0 >= 14 and width > 18:
            _w, h = self._text(
                cr,
                self._name_of(rule.playlist),
                x0 + 6,
                y0 + 2,
                fg,
                0.95,
                7.5,
                bold=True,
                width=width,
                lines=2 if y1 - y0 >= 48 else 1,
            )
            if y1 - y0 >= h + 22:
                self._tag(cr, rule.display, x0 + 4, y1 - 15, x1 - x0 - 4, color, dark)
        self._hits.append(
            Hit(
                x0,
                y0,
                x1,
                y1,
                "exception",
                rule,
                exception.day,
                exception.start,
                exception.end,
                [exception.over] if exception.over else [],
            )
        )

    def _tag(self, cr, text: str, x: float, y: float, width: float, color, dark: bool) -> None:
        layout = self._layout(text, 6, bold=True, width=width - 4)
        w, h = layout.get_pixel_size()
        _rounded(cr, x, y, x + w + 5, y + h + 1, (h + 1) / 2)
        cr.set_source_rgba(*color, 1)
        cr.fill()
        cr.move_to(x + 2.5, y + 0.5)
        cr.set_source_rgba(1, 1, 1, 1)
        PangoCairo.show_layout(cr, layout)

    def _draw_selection(self, cr, gutter, col_w, accent, fg, base) -> None:
        day0, day1, start, end = self._selection
        y0, y1 = self._y(start), self._y(end)
        for column in range(day0, day1 + 1):
            x0 = gutter + column * col_w + 3
            x1 = gutter + (column + 1) * col_w - 2
            _rounded(cr, x0, y0, x1, y1, 6)
            cr.set_source_rgba(*accent, 0.30)
            cr.fill_preserve()
            cr.set_source_rgba(*accent, 1)
            cr.set_line_width(2)
            cr.set_dash([5, 3])
            cr.stroke()
            cr.set_dash([])
        days = list(range(day0, day1 + 1))
        label = f"{model.days_text(days)} · {model.hhmm(start)}–{model.hhmm(end)}"
        layout = self._layout(label, 8, bold=True)
        w, h = layout.get_pixel_size()
        x = gutter + day0 * col_w + 4
        y = max(self._y(0), y0 - h - 10)
        _rounded(cr, x, y, x + w + 14, y + h + 6, (h + 6) / 2)
        cr.set_source_rgba(*accent, 1)
        cr.fill()
        cr.move_to(x + 7, y + 3)
        cr.set_source_rgba(1, 1, 1, 1)
        PangoCairo.show_layout(cr, layout)

    # -- interaction ----------------------------------------------------------
    def _hit(self, x: float, y: float) -> Hit | None:
        for hit in reversed(self._hits):
            if hit.x0 <= x <= hit.x1 and hit.y0 <= y <= hit.y1:
                return hit
        return None

    def _set_hover(self, hit: Hit | None) -> None:
        if hit is not self._hover:
            self._hover = hit
            self.set_cursor_from_name("pointer" if hit and hit.rule else None)
            self.queue_draw()

    def _motion(self, _ctrl, x: float, y: float) -> None:
        if self._selection is None:
            self._set_hover(self._hit(x, y))

    def _drag_begin(self, _gesture, x: float, y: float) -> None:
        self._press = (x, y)
        self._selection = None

    def _selection_from(self, x: float, y: float) -> tuple[int, int, int, int] | None:
        if not self._press:
            return None
        first = self._locate(*self._press)
        if not first:
            return None
        _gutter, header, _col_w, _hour_h = self._geometry()
        second = self._locate(max(x, _gutter + 1), max(y, header))
        if not second:
            return None
        day0, day1 = sorted((first[0], second[0]))
        low, high = sorted((first[1], second[1]))
        start = (low // SNAP) * SNAP
        end = min(model.DAY_MINUTES, -(-high // SNAP) * SNAP)
        if end - start < SNAP:
            end = min(model.DAY_MINUTES, start + SNAP)
        return day0, day1, start, end

    def _drag_update(self, gesture, dx: float, dy: float) -> None:
        if not self._press:
            return
        if self._selection is None and abs(dy) < 10 and abs(dx) < 16:
            return
        x, y = self._press[0] + dx, self._press[1] + dy
        self._selection = self._selection_from(x, y)
        self._hover = None
        self.queue_draw()

    def _drag_end(self, _gesture, dx: float, dy: float) -> None:
        press, selection = self._press, self._selection
        self._press = None
        if selection:
            self._selection = None
            self.queue_draw()
            day0, day1, start, end = selection
            self._on_create(list(range(day0, day1 + 1)), start, end % model.DAY_MINUTES)
            return
        if not press:
            return
        hit = self._hit(*press)
        if hit and hit.rule:
            self._on_edit(hit.rule)
            return
        spot = self._locate(*press)
        if spot:
            day, minute = spot
            start = min(23 * 60, (minute // 60) * 60)
            self._on_create([day], start, (start + 60) % model.DAY_MINUTES)

    def _tooltip(self, _widget, x: int, y: int, _keyboard: bool, tooltip: Gtk.Tooltip) -> bool:
        hit = self._hit(x, y)
        if not hit:
            return False
        day = DAYS_LONG[hit.day]
        when = f"{day} {model.hhmm(hit.start)}–{model.hhmm(hit.end)}"
        if hit.kind == "fallback":
            tooltip.set_markup(
                f"<b>Nothing scheduled</b>\n{when} · {escape(self.fallback_tip)}\nClick or drag to add a rule"
            )
        elif hit.kind == "loser":
            winner = escape(self._name_of(hit.over[0].playlist))
            tooltip.set_markup(
                f"<b>{escape(self._name_of(hit.rule.playlist))}</b> · overridden\n{when} · {winner} wins (higher rule)"
            )
        else:
            rule = hit.rule
            lines = [f"<b>{escape(self._name_of(rule.playlist))}</b>", escape(model.summary(rule))]
            if hit.over:
                names = ", ".join(dict.fromkeys(self._name_of(r.playlist) for r in hit.over))
                lines.append(f"Overrides {escape(names)}")
            if hit.kind == "exception":
                lines.append(f"Only on {escape(rule.display)}")
            lines.append("Click to edit")
            tooltip.set_markup("\n".join(lines))
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(hit.x0), int(hit.y0)
        rect.width, rect.height = max(1, int(hit.x1 - hit.x0)), max(1, int(hit.y1 - hit.y0))
        tooltip.set_tip_area(rect)
        return True


class WeekPreview(Gtk.DrawingArea):
    """Seven thin tracks (Mon–Sun) showing where one rule lands. Used in the editor."""

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("week-calendar")
        self.set_content_height(7 * 11 + 16)
        self.set_hexpand(True)
        self._coverage: list[list[tuple[int, int]]] = [[] for _ in range(7)]
        self._color = model.FALLBACK_COLOR
        self._valid = True
        self.set_draw_func(self._draw)

    def set_rule(self, rule: Rule, color: str, valid: bool = True) -> None:
        self._coverage = model.coverage(rule) if valid else [[] for _ in range(7)]
        self._color, self._valid = color, valid
        self.queue_draw()

    def _draw(self, _area, cr, width: int, _height: int) -> None:
        parent = self.get_parent()
        fg_rgba = parent.get_color() if parent else self.get_color()
        fg = (fg_rgba.red, fg_rgba.green, fg_rgba.blue)
        color = model.rgb(self._color)
        label_w = 34
        track_w = width - label_w - 4
        for column in range(7):
            y = column * 11
            layout = self.create_pango_layout(DAYS[column])
            desc = self.get_pango_context().get_font_description().copy()
            desc.set_size(int(7 * Pango.SCALE))
            layout.set_font_description(desc)
            _w, h = layout.get_pixel_size()
            cr.move_to(0, y + 4 - h / 2)
            active = bool(self._coverage[column])
            cr.set_source_rgba(*fg, 0.8 if active else 0.4)
            PangoCairo.show_layout(cr, layout)
            _rounded(cr, label_w, y, label_w + track_w, y + 8, 4)
            cr.set_source_rgba(*fg, 0.08)
            cr.fill()
            for start, end in self._coverage[column]:
                x0 = label_w + track_w * start / model.DAY_MINUTES
                x1 = label_w + track_w * end / model.DAY_MINUTES
                _rounded(
                    cr,
                    x0,
                    y,
                    max(x0 + 3, x1),
                    y + 8,
                    4,
                )
                cr.set_source_rgba(*color, 1)
                cr.fill()
        # Hour ticks.
        for hour in (0, 6, 12, 18, 24):
            x = label_w + track_w * hour / 24
            text = f"{hour:02d}:00" if hour < 24 else "24:00"
            layout = self.create_pango_layout(text)
            desc = self.get_pango_context().get_font_description().copy()
            desc.set_size(int(6.5 * Pango.SCALE))
            layout.set_font_description(desc)
            w, _h = layout.get_pixel_size()
            tx = min(max(label_w, x - w / 2), label_w + track_w - w)
            cr.move_to(tx, 7 * 11 + 2)
            cr.set_source_rgba(*fg, 0.5)
            PangoCairo.show_layout(cr, layout)
