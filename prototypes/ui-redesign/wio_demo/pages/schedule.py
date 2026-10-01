"""Schedule: what plays when, as a week calendar plus an ordered list of rules.

Semantics are the runtime's (see ``state.rule_matches``): a later rule wins
where rules overlap, the end time is exclusive, a window may wrap past
midnight (its tail belongs to the day it started), empty days = every day,
empty months = all year. The list shows the *highest* priority on top, which
is simply the stored order reversed — the data keeps "last match wins".
"""

from __future__ import annotations

import datetime as dt
import math
from html import escape

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk

from .. import reorder, ui
from ..catalog import DAYS_LONG, MONTHS
from ..models import Rule
from . import Page
from . import schedule_model as model
from .schedule_calendar import WeekCalendar
from .schedule_editor import RuleEditor, playlist_factory

CSS = """
.week-calendar { color: var(--accent-bg-color); }
.schedule-calendar-card {
  background-color: var(--view-bg-color);
  border-radius: 14px;
  box-shadow: 0 0 0 1px alpha(currentColor, 0.09), 0 1px 3px alpha(black, 0.06);
  padding: 4px 6px 4px 0;
}
.schedule-now { padding: 12px 14px 12px 12px; border-radius: 14px; }
.schedule-now .now-name { font-weight: 800; font-size: 1.15em; }
.schedule-now .now-display { min-width: 74px; }
.schedule-cover { border-radius: 8px; }
.schedule-cover-icon { background-color: alpha(currentColor, 0.08); }
.schedule-summary { padding: 14px; }
.preset-chip { border-radius: 999px; padding: 2px 10px; min-height: 0; font-size: 0.9em; font-weight: 600; }
.preset-chip.active { background-color: alpha(var(--accent-bg-color), 0.16); color: var(--accent-color); }
.day-chip { padding-left: 0; padding-right: 0; font-weight: 600; }
.rules-list > row.rule-row { padding: 0; }
.rule-row .rule-body { padding: 8px 8px 8px 4px; }
.rule-row.off .rule-dim { opacity: 0.45; }
.rule-handle { min-width: 20px; }
.rule-title { font-weight: 700; }
.legend-label { font-size: 0.85em; }
.why-popover contents { padding: 0; }
.why-row { padding: 6px 10px; }
.why-row.winner { background-color: alpha(var(--accent-bg-color), 0.12); }
.why-reason { font-size: 0.8em; font-weight: 700; padding: 1px 8px; border-radius: 999px;
  background-color: alpha(currentColor, 0.08); }
.why-reason.playing { background-color: var(--accent-bg-color); color: var(--accent-fg-color); }
.why-reason.overridden { background-color: alpha(#e5a50a, 0.22); }
.why-reason.off { opacity: 0.7; }
.pick-note { color: var(--warning-color); font-weight: 600; }
"""


class Dot(Gtk.DrawingArea):
    """A playlist's calendar color: a filled circle with a ring in the background color."""

    def __init__(self, color: str, size: int = 12, ring: bool = True) -> None:
        super().__init__()
        self._color, self._size, self._ring = color, size, ring
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.set_can_target(False)
        self.set_draw_func(self._draw)

    def _draw(self, _area, cr, width, height) -> None:
        r = min(width, height) / 2
        if self._ring:
            dark = Adw.StyleManager.get_default().get_dark()
            cr.set_source_rgb(*((0.18, 0.18, 0.2) if dark else (1, 1, 1)))
            cr.arc(width / 2, height / 2, r, 0, math.tau)
            cr.fill()
            r -= 2
        cr.set_source_rgb(*model.rgb(self._color))
        cr.arc(width / 2, height / 2, r, 0, math.tau)
        cr.fill()


class Hatch(Gtk.DrawingArea):
    """Legend swatch for 'overridden'."""

    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(16)
        self.set_content_height(12)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def _draw(self, _area, cr, width, height) -> None:
        color = self.get_color()
        cr.rectangle(0, 0, width, height)
        cr.clip()
        cr.set_source_rgba(color.red, color.green, color.blue, 0.75)
        cr.set_line_width(1.5)
        for x in range(-height, width + 4, 4):
            cr.move_to(x, height)
            cr.line_to(x + height, 0)
        cr.stroke()


def cover(state, pid: str, size: int) -> Gtk.Widget:
    playlist = state.playlist(pid) if state.has_playlist(pid) else None
    image = Gtk.Image(pixel_size=size)
    image.set_overflow(Gtk.Overflow.HIDDEN)
    image.add_css_class("schedule-cover")
    image.set_valign(Gtk.Align.CENTER)
    if playlist and not playlist.automatic:
        image.set_from_paintable(state.playlist_cover(pid, 96))
    else:
        image.set_from_icon_name(playlist.icon if playlist and playlist.icon else "view-grid-symbolic")
        image.add_css_class("schedule-cover-icon")
    return image


class SchedulePage(Page):
    name = "schedule"
    title = "Schedule"

    def __init__(self, state) -> None:
        super().__init__(state)
        ui.add_css(CSS)
        self._week = 0  # weeks from the current one
        self._view = "all"  # "all" or a connector
        self._rule_rows: list[Gtk.ListBoxRow] = []
        self._building = False
        self._editor: RuleEditor | None = None
        self._demo_assigned: list[str] = []
        self._layout: tuple | None = None
        self._now_key: tuple | None = None
        self._refresh_pending = False
        self._force_pending = False
        self._demo_pick = False

        # -- header ----------------------------------------------------------
        self._title = Adw.WindowTitle(title="Schedule")
        nav = Gtk.Box()
        nav.add_css_class("linked")
        nav.append(ui.icon_button("go-previous-symbolic", "Previous week", lambda *_: self._move_week(-1)))
        nav.append(ui.icon_button("go-next-symbolic", "Next week", lambda *_: self._move_week(1)))
        self._nav = nav
        self._today = Gtk.Button(label="Today")
        self._today.set_tooltip_text("Back to this week")
        self._today.connect("clicked", lambda *_: self._set_week(0))
        self._add = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Add rule"))
        self._add.set_tooltip_text("Add a rule")
        self._add.connect("clicked", lambda *_: self.new_rule())
        self._add_small = ui.icon_button("list-add-symbolic", "Add rule", lambda *_: self.new_rule())
        self._add_small.set_visible(False)

        # -- now -------------------------------------------------------------
        self._now_card = Gtk.Box(spacing=14)
        self._now_card.add_css_class("card")
        self._now_card.add_css_class("schedule-now")
        self._now_cover = Gtk.Box(valign=Gtk.Align.CENTER)
        self._now_card.append(self._now_cover)
        self._now_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, valign=Gtk.Align.CENTER)
        self._eyebrow = Gtk.Label(xalign=0)
        self._eyebrow.add_css_class("section-label")
        self._now_text.append(self._eyebrow)
        self._now_lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self._now_text.append(self._now_lines)
        self._now_card.append(self._now_text)
        self._resume = Gtk.Button(label="Resume schedule", valign=Gtk.Align.CENTER, visible=False)
        self._resume.add_css_class("pill")
        self._resume.add_css_class("suggested-action")
        self._resume.set_tooltip_text("Stop your pick and play what the schedule says")
        self._resume.connect("clicked", lambda *_: self.state.resume_schedule("all"))
        self._now_card.append(self._resume)
        self._why_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._why_popover = Gtk.Popover(child=self._why_box)
        self._why_popover.add_css_class("why-popover")
        self._why_popover.set_position(Gtk.PositionType.BOTTOM)
        self._why_popover.set_offset(-130, 0)
        self._why_popover.connect("show", lambda *_: self._build_why())
        self._why = Gtk.MenuButton(popover=self._why_popover, valign=Gtk.Align.CENTER)
        self._why.set_child(Adw.ButtonContent(icon_name="help-about-symbolic", label="Why?"))
        self._why.set_tooltip_text("Why this playlist now?")
        self._now_card.append(self._why)

        # -- calendar ----------------------------------------------------------
        toolbar = Adw.WrapBox(child_spacing=12, line_spacing=8)
        self._views = Adw.ToggleGroup()
        self._build_view_toggles()
        self._views.connect("notify::active-name", self._on_view)
        toolbar.append(self._views)
        legend = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        legend.append(Hatch())
        legend_label = Gtk.Label(label="Overridden")
        legend_label.add_css_class("dimmed")
        legend_label.add_css_class("legend-label")
        legend.append(legend_label)
        legend.set_tooltip_text("Hatched edge: a lower rule that the block above it replaces")
        toolbar.append(legend)
        self._seasonal = Gtk.Button()
        self._seasonal.add_css_class("flat")
        self._seasonal.set_valign(Gtk.Align.CENTER)
        self._seasonal.connect("clicked", lambda *_: self._show_seasonal())
        toolbar.append(self._seasonal)
        self.calendar = WeekCalendar(
            on_edit=self.edit_rule, on_create=self._create_from_calendar, name_of=state.playlist_name
        )
        calendar_card = Gtk.Box(vexpand=True)
        calendar_card.add_css_class("schedule-calendar-card")
        calendar_card.append(self.calendar)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        left.append(toolbar)
        left.append(calendar_card)

        # -- rules ---------------------------------------------------------------
        rules = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_bottom=12)
        heading = Gtk.Box(spacing=6, margin_top=6)
        label = Gtk.Label(label="RULES", xalign=0)
        label.add_css_class("section-label")
        heading.append(label)
        heading.append(Gtk.Box(hexpand=True))
        order = Gtk.Box(spacing=4)
        order_icon = Gtk.Image.new_from_icon_name("go-up-symbolic")
        order_icon.set_pixel_size(12)
        order.append(order_icon)
        order.append(Gtk.Label(label="Top rule wins"))
        order.add_css_class("dimmed")
        order.add_css_class("caption")
        order.set_tooltip_text("Where rules overlap, the higher one plays. Drag a rule up or down to reorder.")
        heading.append(order)
        rules.append(heading)
        # Rules lift and roll when dragged (reorder.ReorderList); top wins.
        self._list = reorder.ReorderList()
        self._list.add_css_class("rules-list")
        self._list.connect("row-activated", self._on_row_activated)
        self._list.connect("reordered", self._on_rules_reordered)
        self._list.connect("settled", lambda *_: self._rules_deferred and self._refresh_rules())
        self._rules_deferred = False
        empty = Adw.StatusPage(
            icon_name="x-office-calendar-symbolic", title="No rules", description="Drag on the calendar or add one"
        )
        empty.add_css_class("compact")
        self._list.set_placeholder(empty)
        rules.append(self._list)

        fallback_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=4)
        fallback_list.add_css_class("boxed-list")
        self._fallback_ids = [p.id for p in state.playlists]
        self._fallback = Adw.ComboRow(title="When nothing is scheduled", model=Gtk.StringList.new(self._fallback_ids))
        self._fallback.set_title_lines(2)
        self._fallback.set_factory(playlist_factory(state, 22))
        self._fallback.connect("notify::selected", self._on_fallback)
        fallback_list.append(self._fallback)
        rules.append(fallback_list)
        # A clamp keeps the column at a steady width whatever the labels say.
        self._rules_clamp = Adw.Clamp(maximum_size=380, tightening_threshold=380, child=rules)
        self._rules_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, hexpand=False)
        self._rules_scroller.set_child(self._rules_clamp)
        self._rules_scroller.set_size_request(360, -1)

        # -- layout ----------------------------------------------------------------
        self._columns = Gtk.Box(spacing=18)
        self._columns.append(left)
        self._columns.append(self._rules_scroller)
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_start=18,
            margin_end=18,
            margin_top=12,
            margin_bottom=14,
        )
        body.append(self._now_card)
        body.append(self._columns)
        self._body = body
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(body)
        breakpoint_bin = Adw.BreakpointBin(width_request=340, height_request=300)
        breakpoint_bin.set_child(scroller)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 880sp"))
        narrow.add_setter(self._columns, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self._rules_scroller, "vscrollbar-policy", Gtk.PolicyType.NEVER)
        narrow.add_setter(self._rules_scroller, "width-request", -1)
        narrow.add_setter(self._rules_clamp, "maximum-size", 860)
        narrow.add_setter(self._rules_clamp, "tightening-threshold", 860)
        narrow.add_setter(body, "margin-start", 12)
        narrow.add_setter(body, "margin-end", 12)
        narrow.add_setter(self._add, "visible", False)
        narrow.add_setter(self._add_small, "visible", True)
        narrow.connect("apply", lambda *_: self._set_narrow(True))
        narrow.connect("unapply", lambda *_: self._set_narrow(False))
        self._narrow = False
        breakpoint_bin.add_breakpoint(narrow)
        self.widget = breakpoint_bin
        self._install_actions()

        state.connect("changed", self._on_changed)
        self.refresh()

    # -- Page API ------------------------------------------------------------------
    def title_widget(self) -> Gtk.Widget:
        return self._title

    def header_start(self) -> list[Gtk.Widget]:
        return [self._nav, self._today]

    def header_end(self) -> list[Gtk.Widget]:
        return [self._add, self._add_small]

    def activate(self, argument: str | None) -> None:
        self.refresh()
        what, _, rest = (argument or "").partition(":")
        if what == "why":
            self._set_week(0)
            GLib.timeout_add(250, lambda: (self._why.popup(), False)[1])
        elif what == "new":
            # e.g. "schedule:new:HDMI-A-1" from the Displays page.
            prefill = {"days": [], "start": "09:00", "end": "17:00"}
            if rest in self.state.connectors():
                prefill["display"] = rest
                if self.state.display_mode == "independent":
                    self._views.set_active_name(rest)
            GLib.idle_add(lambda: (self.new_rule(prefill), False)[1])

    def demo(self, scene: str) -> None:
        # Each scene starts from the demo data, so scenes don't leak into each other.
        if self._editor is not None:
            self._editor.force_close()
        self._why_popover.popdown()
        self.calendar.clear_selection()
        self.state.demo_reset_schedule(clear_manual=self._demo_pick, unassign=self._demo_assigned)
        self._demo_pick = False
        self._demo_assigned = []
        self._week = 0
        self._views.set_active_name("all")
        self.refresh()
        for part in scene.split(","):
            self._demo_one(part)

    def _demo_one(self, scene: str) -> None:
        what, _, arg = scene.partition(":")
        rules = {rule.id: rule for rule in self.state.rules}
        if what in ("edit", "editend"):

            def edit() -> bool:
                self.edit_rule(rules[arg or "weekend-days"])
                if what == "editend":
                    GLib.timeout_add(350, self._editor.scroll_to_end)
                return False

            GLib.timeout_add(150, edit)
        elif what == "new":
            GLib.timeout_add(150, lambda: (self._create_from_calendar([3], 20 * 60, 23 * 60), False)[1])
        elif what == "why":
            GLib.timeout_add(250, lambda: (self._why.popup(), False)[1])
        elif what == "drag":
            self.calendar.show_selection(0, 4, 12 * 60, 14 * 60)
        elif what == "enable":
            self.state.set_rule_enabled(arg or "work-screen", True)
            self.refresh()
        elif what == "disable":
            self.state.set_rule_enabled(arg, False)
            self.refresh()
        elif what == "display":
            self._views.set_active_name(arg)
        elif what == "december":
            rule = rules["december"]
            self._set_week(self._weeks_until(model.next_week_for(rule, self._monday(0))))
        elif what == "week":
            self._set_week(int(arg))
        elif what == "menu":

            def popup() -> bool:
                for row in self._rule_rows:
                    if row.rule.id == arg:
                        row.menu_button.popup()
                return False

            GLib.timeout_add(250, popup)
        elif what == "rules":
            GLib.timeout_add(300, self._scroll_to_rules)
        elif what == "assign":  # assign:HDMI-A-1=cozy-rain — a display's own playlist
            connector, _, pid = arg.partition("=")
            self.state.set_display_playlist(connector, pid or "cozy-rain")
            self._demo_assigned.append(connector)
            self.refresh()
        elif what == "pick":
            self._demo_pick = True
            self.state.play_playlist("mc-night")
        elif what == "delete":
            self.delete_rule(rules[arg])
        elif what == "move":
            self.move_rule(rules[arg], 1)
        elif what == "reorder":  # reorder[:rule] — freeze a drag lifting that rule above its neighbor
            GLib.timeout_add(300, self._demo_reorder, arg)

    def _demo_reorder(self, rule_id: str) -> bool:
        rows = self._list.get_rows()
        if len(rows) < 2 or self._list.busy:
            return False
        index = next((i for i, r in enumerate(rows) if r.rule.id == rule_id), len(rows) - 1)
        index = max(1, index)
        heights = self._list.row_heights()
        self._list.begin_drag(rows[index])
        # Carry its middle a little past the middle of the rule above.
        self._list.update_drag(-(heights[index - 1] / 2 + heights[index] / 2) * 1.24)
        return False

    def _scroll_to_rules(self) -> bool:
        scroller = self.widget.get_child()
        ok, point = self._rules_scroller.compute_point(self._body, Graphene.Point())
        if ok:
            scroller.get_vadjustment().set_value(point.y - 8)
        return False

    # -- dates -------------------------------------------------------------------
    def _monday(self, offset: int | None = None) -> dt.date:
        offset = self._week if offset is None else offset
        return model.week_start(self.state.now.date()) + dt.timedelta(weeks=offset)

    def _weeks_until(self, monday: dt.date) -> int:
        return (monday - self._monday(0)).days // 7

    def _set_narrow(self, narrow: bool) -> None:
        self._narrow = narrow
        self._today.set_visible(self._week != 0 and not narrow)

    def _move_week(self, step: int) -> None:
        self._set_week(self._week + step)

    def _set_week(self, offset: int) -> None:
        self._week = offset
        self.refresh()

    # -- refresh -------------------------------------------------------------------
    def _on_changed(self, _state, topic: str) -> None:
        # Topics arrive in bursts (a clock tick sends schedule, now, playback, clock):
        # refresh once, right after the burst.
        if topic in ("playlists", "displays", "theme"):
            if topic == "displays":
                self._build_view_toggles()
            self._force_pending = True
        elif topic not in ("schedule", "now", "clock", "system"):
            return
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.idle_add(self._idle_refresh, priority=GLib.PRIORITY_HIGH_IDLE)

    def _idle_refresh(self) -> bool:
        force, self._force_pending, self._refresh_pending = self._force_pending, False, False
        self.refresh(force=force)
        return False

    def _layout_key(self, monday: dt.date, view: str) -> tuple:
        """Everything the calendar and the list are drawn from — but not the clock."""
        state = self.state
        return (
            monday,
            view,
            state.display_mode,
            state.fallback,
            tuple(sorted(state.assigned.items())),
            tuple(
                (r.id, r.playlist, tuple(r.days), r.start, r.end, tuple(r.months), r.display, r.enabled)
                for r in state.rules
            ),
            tuple((p.id, p.name) for p in state.playlists),
        )

    def refresh(self, force: bool = False) -> None:
        """Cheap when only the clock moved (state.advance ticks): the calendar spans and
        the rules list are rebuilt only when what they show has changed."""
        state = self.state
        monday = self._monday()
        end = monday + dt.timedelta(days=6)
        span_text = (
            f"{monday.day} {MONTHS[monday.month - 1]} – {end.day} {MONTHS[end.month - 1]}"
            if monday.month != end.month
            else f"{monday.day}–{end.day} {MONTHS[end.month - 1]}"
        )
        if self._week == 0:
            self._title.set_subtitle(f"This week · {span_text}")
        else:
            self._title.set_subtitle(f"{span_text} {end.year}" if end.year != state.now.year else span_text)
        self._today.set_visible(self._week != 0 and not self._narrow)

        independent = state.display_mode == "independent"
        self._views.set_visible(independent)
        view = self._view if independent else "all"
        key = self._layout_key(monday, view)
        if force or key != self._layout:
            self._layout = key
            self._rebuild(monday, view, independent)
        today = state.now.weekday() if self._week == 0 else None
        self.calendar.set_clock(today, state.now.hour * 60 + state.now.minute)
        outcomes = self._outcomes()
        self._refresh_now(outcomes, force)
        self._refresh_playing(outcomes)

    def _rebuild(self, monday: dt.date, view: str, independent: bool) -> None:
        state = self.state
        colors = model.playlist_colors(state)
        if view == "all":
            spans = model.week_spans(state.rules, monday, None)
            exceptions = model.week_exceptions(state.rules, monday) if independent else []
            targets = state.connectors() if independent else state.connectors()[:1]
            unscheduled = {c: state.unscheduled_playlist(c) for c in targets}
            if len(set(unscheduled.values())) == 1:
                label = self.state.playlist_name(next(iter(unscheduled.values())))
                tip = f"{label} plays"
            else:
                label = "Per display"
                tip = " · ".join(f"{c}: {self.state.playlist_name(p)}" for c, p in unscheduled.items())
        else:
            spans = model.week_spans(state.rules, monday, view)
            exceptions = []
            label = self.state.playlist_name(state.unscheduled_playlist(view))
            tip = f"{label} plays on {view}"
        self.calendar.update(spans, exceptions, monday, colors, label, tip, "" if view == "all" else view)

        seasonal = model.seasonal_off(state.rules, monday)
        self._seasonal.set_visible(bool(seasonal))
        if seasonal:
            months = model.months_text(sorted({m for rule in seasonal for m in rule.months}))
            text = (
                "1 seasonal rule not active this week"
                if len(seasonal) == 1
                else f"{len(seasonal)} seasonal rules not active this week"
            )
            self._seasonal.set_child(Adw.ButtonContent(icon_name="x-office-calendar-symbolic", label=text))
            self._seasonal.set_tooltip_text(f"{months} — show the next week it applies")

        self._refresh_rules()
        self._building = True
        if state.fallback in self._fallback_ids:
            self._fallback.set_selected(self._fallback_ids.index(state.fallback))
        own = [(c, p) for c, p in sorted(state.assigned.items()) if p and independent]
        self._fallback.set_subtitle(" · ".join(f"{c} uses {self.state.playlist_name(p)}" for c, p in own))
        self._building = False

    def _build_view_toggles(self) -> None:
        connectors = self.state.connectors()
        if getattr(self, "_view_connectors", None) == connectors:
            return
        self._view_connectors = connectors
        self._building = True
        self._views.remove_all()
        self._views.add(Adw.Toggle(name="all", label="All displays"))
        for display in self.state.displays:
            toggle = Adw.Toggle(name=display.connector, label=display.connector)
            toggle.set_tooltip(f"What {display.connector} ({display.model}) plays")
            self._views.add(toggle)
        if self._view not in connectors:
            self._view = "all"
        self._views.set_active_name(self._view)
        self._building = False

    # -- now -----------------------------------------------------------------------
    def _outcomes(self) -> list[tuple[list[str], str, object, str]]:
        """[(connectors, kind, payload, playlist)] grouped where displays agree.

        kind is "pick" (a manual choice) or "schedule". A schedule outcome without a
        rule plays the display's own playlist or the app default (state.resolution).
        """
        state = self.state
        connectors = state.connectors() if state.display_mode == "independent" else state.connectors()[:1]
        groups: list[tuple[list[str], str, object, str]] = []
        for connector in connectors:
            if connector in state.manual:
                outcome = ("pick", None, state.manual[connector])
            else:
                resolution = state.resolution(connector)
                outcome = ("schedule", resolution, resolution.playlist)
            for group in groups:
                kind, payload, pid = group[1], group[2], group[3]
                if (
                    kind == outcome[0]
                    and pid == outcome[2]
                    and (
                        kind != "schedule"
                        or (
                            payload.rule is outcome[1].rule
                            and payload.next_at == outcome[1].next_at
                            and payload.next_playlist == outcome[1].next_playlist
                        )
                    )
                ):
                    group[0].append(connector)
                    break
            else:
                groups.append(([connector], outcome[0], outcome[1], outcome[2]))
        return groups

    def _refresh_now(self, groups, force: bool = False) -> None:
        state = self.state
        self._eyebrow.set_label(f"NOW · {state.now.strftime('%a').upper()} {state.now.strftime('%H:%M')}")
        key = (
            self._layout,
            tuple(
                (
                    tuple(c),
                    kind,
                    pid,
                    getattr(p, "rule", None) and p.rule.id,
                    getattr(p, "next_at", ""),
                    getattr(p, "next_playlist", ""),
                )
                for c, kind, p, pid in groups
            ),
            tuple(sorted(state.current.items())),
        )
        if key == self._now_key and not force:
            return
        self._now_key = key
        for box in (self._now_cover, self._now_lines):
            child = box.get_first_child()
            while child:
                box.remove(child)
                child = box.get_first_child()
        first_pid = groups[0][3]
        self._now_cover.append(
            cover(self.state, first_pid, 48)
            if first_pid != "quick"
            else ui.thumbnail(state.wallpaper(state.current[groups[0][0][0]]), 48, 48, 8)
        )
        multi = len(groups) > 1
        for connectors, kind, payload, pid in groups:
            line = Gtk.Box(spacing=8)
            if multi:
                tag = ui.pill(" · ".join(connectors), "video-display-symbolic", "subtle")
                tag.set_valign(Gtk.Align.CENTER)
                tag.add_css_class("now-display")
                line.append(tag)
            name = "Your pick" if pid == "quick" else self.state.playlist_name(pid)
            title = Gtk.Label(xalign=0, wrap=True)
            title.add_css_class("now-name")
            if kind == "schedule":
                resolution = payload
                markup = f"<span weight='800'>{escape(name)}</span>"
                if resolution.next_at:
                    then = escape(state.playlist_name(resolution.next_playlist))
                    rest = f"until {resolution.next_at} · then {then}"
                    markup += f"<span weight='400' alpha='70%'>  {rest}</span>"
                title.set_markup(markup)
                line.append(title)
                self._now_lines.append(line)
                if not multi:
                    detail = Gtk.Box(spacing=6)
                    if resolution.rule:
                        detail.append(Dot(model.color_for(state, resolution.rule.playlist), 10, ring=False))
                        text = model.summary(resolution.rule)
                    elif state.assigned.get(connectors[0]) and state.display_mode == "independent":
                        text = f"Nothing scheduled · {connectors[0]}'s own playlist"
                    else:
                        text = "Nothing scheduled right now"
                    label = Gtk.Label(label=text, xalign=0, ellipsize=3)
                    label.add_css_class("dimmed")
                    detail.append(label)
                    self._now_lines.append(detail)
            else:
                title.set_markup(f"<span weight='800'>{escape(name)}</span>")
                line.append(title)
                note = Gtk.Label(label="your pick", xalign=0)
                note.add_css_class("pick-note")
                line.append(note)
                self._now_lines.append(line)
                if not multi:
                    resolution = state.resolution(connectors[0])
                    self._now_lines.append(
                        ui.dim(f"The schedule says {self.state.playlist_name(resolution.playlist)}", wrap=False)
                    )
        self._resume.set_visible(any(kind == "pick" for _c, kind, _p, _pid in groups))
        if self._why_popover.get_visible():
            self._build_why()

    def _refresh_playing(self, groups) -> None:
        playing = {group[2].rule.id for group in groups if group[1] == "schedule" and group[2].rule}
        for row in self._rule_rows:
            now = row.rule.id in playing
            row.now_pill.set_visible(now)
            row.warning.set_visible(row.never and not now)

    # -- why -------------------------------------------------------------------------
    def _build_why(self) -> None:
        """Names what plays, the rule that decides it, the rules it beats right now,
        and — briefly — why every other rule isn't in play."""
        state = self.state
        box = self._why_box
        child = box.get_first_child()
        while child:
            box.remove(child)
            child = box.get_first_child()
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_start=12,
            margin_end=12,
            margin_top=12,
            margin_bottom=12,
        )
        content.set_size_request(340, -1)
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=560
        )
        scroller.set_child(content)
        box.append(scroller)
        groups = self._outcomes()
        at = state.now
        first = groups[0]
        if len(groups) == 1:
            heading = f"Why {('your pick' if first[3] == 'quick' else self.state.playlist_name(first[3]))}?"
        else:
            heading = "Why these playlists?"
        title = Gtk.Label(label=heading, xalign=0, wrap=True)
        title.add_css_class("title-4")
        content.append(title)
        content.append(ui.dim(f"{DAYS_LONG[at.weekday()]} {at.strftime('%H:%M')} · top rule wins", wrap=False))
        in_play: set[str] = set()
        for connectors, kind, payload, _pid in groups:
            if len(groups) > 1:
                label = Gtk.Label(label=" · ".join(connectors).upper(), xalign=0, margin_top=4)
                label.add_css_class("section-label")
                content.append(label)
            if kind != "schedule":
                content.append(ui.dim("Your pick replaces the schedule until you resume it."))
                continue
            display = connectors[0]
            winner = payload.rule
            active = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            active.add_css_class("boxed-list")
            active.connect("row-activated", lambda _l, row: self._why_edit(row))
            for rule in reversed(state.rules):
                text, rkind = model.reason(rule, at, display, winner)
                if rkind in ("playing", "overridden"):
                    active.append(self._why_row(rule, text, rkind))
                    in_play.add(rule.id)
            if winner is None:
                active.append(self._why_unscheduled(display))
            content.append(active)
        idle = [rule for rule in reversed(state.rules) if rule.id not in in_play]
        if idle:
            label = Gtk.Label(label="NOT NOW", xalign=0, margin_top=2)
            label.add_css_class("section-label")
            content.append(label)
            quiet = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            quiet.add_css_class("boxed-list")
            quiet.connect("row-activated", lambda _l, row: self._why_edit(row))
            for rule in idle:
                display = rule.display or first[0][0]
                text, rkind = model.reason(rule, at, display, None)
                quiet.append(self._why_row(rule, text, rkind, compact=True))
            content.append(quiet)

    def _why_unscheduled(self, display: str) -> Gtk.ListBoxRow:
        state = self.state
        own = bool(state.assigned.get(display)) and state.display_mode == "independent"
        unscheduled = state.unscheduled_playlist(display)
        row = Gtk.ListBoxRow(activatable=False)
        inner = Gtk.Box(spacing=10)
        inner.add_css_class("why-row")
        inner.add_css_class("winner")
        inner.append(cover(self.state, unscheduled, 28))
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        name = Gtk.Label(label=self.state.playlist_name(unscheduled), xalign=0)
        name.add_css_class("rule-title")
        texts.append(name)
        sub = Gtk.Label(
            label=f"No rule now · {display}'s own playlist" if own else "No rule now · the default", xalign=0
        )
        sub.add_css_class("caption")
        sub.add_css_class("dimmed")
        texts.append(sub)
        inner.append(texts)
        pill = Gtk.Label(label="Playing", valign=Gtk.Align.CENTER)
        pill.add_css_class("why-reason")
        pill.add_css_class("playing")
        inner.append(pill)
        row.set_child(inner)
        return row

    def _why_row(self, rule: Rule, reason: str, kind: str, compact: bool = False) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=True)
        row.rule = rule
        row.set_tooltip_text("Edit this rule")
        inner = Gtk.Box(spacing=10)
        inner.add_css_class("why-row")
        if kind == "playing":
            inner.add_css_class("winner")
        if compact:
            inner.append(Dot(model.color_for(self.state, rule.playlist), 10, ring=False))
        else:
            inner.append(cover(self.state, rule.playlist, 28))
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        name = Gtk.Label(label=self.state.playlist_name(rule.playlist), xalign=0, ellipsize=3)
        if not compact:
            name.add_css_class("rule-title")
        texts.append(name)
        if not compact:
            sub = Gtk.Label(label=model.summary(rule), xalign=0, ellipsize=3)
            sub.add_css_class("caption")
            sub.add_css_class("dimmed")
            texts.append(sub)
        else:
            name.set_tooltip_text(model.summary(rule))
        inner.append(texts)
        pill = Gtk.Label(label=reason, valign=Gtk.Align.CENTER)
        pill.add_css_class("why-reason")
        pill.add_css_class(kind)
        inner.append(pill)
        if compact:
            row.set_tooltip_text(f"{model.summary(rule)} — click to edit")
        row.set_child(inner)
        return row

    def _why_edit(self, row: Gtk.ListBoxRow) -> None:
        rule = getattr(row, "rule", None)
        if rule:
            self._why_popover.popdown()
            self.edit_rule(rule)

    # -- rules list --------------------------------------------------------------------
    def _refresh_rules(self) -> None:
        if self._list.dragging_by_hand:
            self._rules_deferred = True  # never pull rows out from under the pointer
            return
        if self._list.flush():
            return  # committing the pending move already rebuilt the list
        self._rules_deferred = False
        state = self.state
        focused = self._focused_rule()
        self._list.remove_all()
        self._rule_rows = []
        monday = self._monday()
        seasonal = {rule.id for rule in model.seasonal_off(state.rules, monday)}
        for rule in reversed(state.rules):
            row = self._rule_row(rule, rule.id in seasonal, monday)
            self._list.append(row, row.handle)
            self._rule_rows.append(row)
        if focused:
            GLib.idle_add(self._focus_rule, focused[0], focused[1])

    def _focused_rule(self) -> tuple[Rule, bool] | None:
        """The rule whose row (or its switch) has keyboard focus, to restore after a rebuild."""
        root = self.widget.get_root()
        widget = root.get_focus() if root else None
        on_switch = isinstance(widget, Gtk.Switch)
        while widget is not None:
            if isinstance(widget, Gtk.ListBoxRow) and hasattr(widget, "rule") and widget in self._rule_rows:
                return widget.rule, on_switch
            widget = widget.get_parent()
        return None

    def _rule_row(self, rule: Rule, seasonal: bool, monday: dt.date) -> Gtk.ListBoxRow:
        state = self.state
        row = Gtk.ListBoxRow(activatable=True)
        row.rule = rule
        row.add_css_class("rule-row")
        if not rule.enabled:
            row.add_css_class("off")
        row.set_tooltip_text("Edit rule")
        body = Gtk.Box(spacing=8)
        body.add_css_class("rule-body")
        handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        handle.add_css_class("rule-handle")
        handle.set_tooltip_text("Drag up or down to change priority (Ctrl+↑ / Ctrl+↓)")
        handle.update_property([Gtk.AccessibleProperty.LABEL], [f"Reorder {self.state.playlist_name(rule.playlist)}"])
        body.append(handle)
        row.handle = handle

        art_box = Gtk.Overlay(valign=Gtk.Align.CENTER)
        frame = Gtk.Box(width_request=42, height_request=42)
        picture = cover(self.state, rule.playlist, 38)
        picture.set_halign(Gtk.Align.START)
        picture.set_valign(Gtk.Align.START)
        frame.append(picture)
        art_box.set_child(frame)
        dot = Dot(model.color_for(state, rule.playlist), 14)
        dot.set_halign(Gtk.Align.END)
        dot.set_valign(Gtk.Align.END)
        art_box.add_overlay(dot)
        art_box.add_css_class("rule-dim")
        body.append(art_box)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        texts.add_css_class("rule-dim")
        top = Gtk.Box(spacing=6)
        title = Gtk.Label(label=self.state.playlist_name(rule.playlist), xalign=0, ellipsize=3)
        title.add_css_class("rule-title")
        top.append(title)
        pill = ui.pill("Now", "media-playback-start-symbolic", "accent")
        pill.set_valign(Gtk.Align.CENTER)
        pill.set_visible(False)
        top.append(pill)
        warn = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        warn.add_css_class("warning")
        warn.set_pixel_size(14)
        warn.set_tooltip_text("Never plays this week: rules above cover all of its times")
        warn.set_visible(False)
        top.append(warn)
        row.now_pill, row.warning = pill, warn
        row.never = model.never_wins(state.rules, rule, monday, state.connectors())
        texts.append(top)
        summary = Gtk.Label(label=model.summary(rule), xalign=0, wrap=True, lines=2)
        summary.set_ellipsize(3)
        summary.add_css_class("caption")
        summary.add_css_class("dimmed")
        if seasonal:
            summary.set_tooltip_text("Not active this week")
        texts.append(summary)
        body.append(texts)

        switch = Gtk.Switch(active=rule.enabled, valign=Gtk.Align.CENTER)
        switch.set_tooltip_text("Rule on" if rule.enabled else "Rule off")
        switch.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"Use rule for {self.state.playlist_name(rule.playlist)}"]
        )
        switch.connect("notify::active", lambda s, _p, r=rule: self._toggle(r, s.get_active()))
        body.append(switch)
        row.switch = switch

        menu = Gio.Menu()
        first = Gio.Menu()
        first.append("Edit…", f"sched.edit::{rule.id}")
        first.append("Duplicate", f"sched.duplicate::{rule.id}")
        menu.append_section(None, first)
        moves = Gio.Menu()
        moves.append("Move up", f"sched.up::{rule.id}")
        moves.append("Move down", f"sched.down::{rule.id}")
        menu.append_section(None, moves)
        last = Gio.Menu()
        last.append("Delete", f"sched.delete::{rule.id}")
        menu.append_section(None, last)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER)
        more.add_css_class("flat")
        more.add_css_class("circular")
        more.set_tooltip_text("More")
        more.get_popover().set_offset(-48, 0)
        body.append(more)
        row.menu_button = more
        row.set_child(body)

        # Dragging is the list's (by the handle or the row); Ctrl+Up/Down too.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._row_key, rule)
        row.add_controller(keys)
        return row

    def _row_key(self, _ctrl, keyval: int, _code: int, modifiers: Gdk.ModifierType, rule: Rule) -> bool:
        if modifiers & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            self.move_rule(rule, 1 if keyval == Gdk.KEY_Up else -1)
            GLib.idle_add(self._focus_rule, rule, False)
            return True
        if keyval == Gdk.KEY_Delete:
            self.delete_rule(rule)
            return True
        return False

    def _focus_rule(self, rule: Rule, on_switch: bool = False) -> bool:
        for row in self._rule_rows:
            if row.rule.id == rule.id:
                (row.switch if on_switch else row).grab_focus()
        return False

    # -- actions -------------------------------------------------------------------------
    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()

        def find(rule_id: str) -> Rule | None:
            return next((r for r in self.state.rules if r.id == rule_id), None)

        def add(name: str, callback) -> None:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))

            def run(_action, value) -> None:
                rule = find(value.get_string())
                if rule:
                    callback(rule)

            action.connect("activate", run)
            group.add_action(action)

        add("edit", self.edit_rule)
        add("duplicate", self.duplicate_rule)
        add("up", lambda rule: self.move_rule(rule, 1))
        add("down", lambda rule: self.move_rule(rule, -1))
        add("delete", self.delete_rule)
        self.widget.insert_action_group("sched", group)

    def _toggle(self, rule: Rule, value: bool) -> None:
        if rule.enabled == value:
            return
        # After the switch has finished toggling: the list is rebuilt around it.
        GLib.idle_add(lambda: (self.state.set_rule_enabled(rule.id, value), self.refresh(), False)[-1])

    def _undo(self, undo) -> object:
        """An Undo that also redraws this page at once."""
        return lambda: (undo(), self.refresh())

    def _present(self, editor: RuleEditor) -> None:
        if self._editor is not None:
            self._editor.force_close()
        self._editor = editor
        editor.connect("closed", lambda dialog: setattr(self, "_editor", None) if self._editor is dialog else None)
        editor.present(self.widget)

    def edit_rule(self, rule: Rule) -> None:
        self._present(RuleEditor(self.state, rule, on_save=self._save, on_delete=self.delete_rule))

    def new_rule(self, prefill: dict | None = None) -> None:
        if prefill is None:
            prefill = {"days": [], "start": "09:00", "end": "17:00"}
        prefill.setdefault("playlist", self._suggest_playlist())
        if self._view != "all" and self.state.display_mode == "independent":
            prefill.setdefault("display", self._view)
        self._present(RuleEditor(self.state, None, prefill=prefill, on_save=self._save, on_delete=self.delete_rule))

    def _suggest_playlist(self) -> str:
        used = {rule.playlist for rule in self.state.rules}
        for playlist in self.state.playlists:
            if not playlist.automatic and playlist.id not in used:
                return playlist.id
        return next(p.id for p in self.state.playlists if not p.automatic)

    def _create_from_calendar(self, days: list[int], start: int, end: int) -> None:
        self.new_rule({"days": days, "start": model.hhmm(start), "end": model.hhmm(end)})

    def _save(self, draft: Rule, original: Rule | None) -> None:
        if original is None:
            rule, undo = self.state.add_rule(draft)
            self.refresh()
            self.state.toast(
                f"Added “{self.state.playlist_name(rule.playlist)}” · {model.summary(rule, overnight=False)}",
                self._undo(undo),
            )
            return
        undo = self.state.update_rule(original.id, draft)
        if undo is None:
            return
        self.refresh()
        self.state.toast("Rule saved", self._undo(undo))

    def delete_rule(self, rule: Rule) -> None:
        undo = self.state.delete_rule(rule.id)
        if undo is None:
            return
        self.refresh()
        self.state.toast(f"Deleted “{self.state.playlist_name(rule.playlist)}” rule", self._undo(undo))

    def duplicate_rule(self, rule: Rule) -> None:
        copy, undo = self.state.duplicate_rule(rule.id)
        self.refresh()
        self.state.toast("Rule duplicated", self._undo(undo))
        self.edit_rule(copy)

    def move_rule(self, rule: Rule, step: int) -> None:
        """step +1 = higher priority (later in the stored order).

        A rule on screen rolls into place first and is committed once it settles.
        """
        rows = self._list.get_rows()
        row = next((r for r in rows if r.rule.id == rule.id), None)
        if row is not None and not self._list.dragging:
            self._list.move(row, rows.index(row) - step)  # the list shows the top priority first
            return
        rules = list(self.state.rules)
        index = rules.index(rule)
        target = index + step
        if not 0 <= target < len(rules):
            return
        rules.insert(target, rules.pop(index))
        self._reorder(rules, rule)

    def _reorder(self, order: list[Rule], moved: Rule) -> None:
        before = [rule.id for rule in self.state.rules]
        after = [rule.id for rule in order]
        undo = self.state.reorder_rules(after)
        if undo is None:
            return
        self.refresh()
        higher = before.index(moved.id) < after.index(moved.id)
        self.state.toast(
            f"“{self.state.playlist_name(moved.playlist)}” moved {'up' if higher else 'down'}", self._undo(undo)
        )

    def _on_rules_reordered(self, _list, row: Gtk.ListBoxRow) -> None:
        """A drag or keyboard move settled: store the new priority once, with Undo."""
        self._reorder([r.rule for r in reversed(self._list.get_rows())], row.rule)

    def _on_row_activated(self, _list, row: Gtk.ListBoxRow) -> None:
        rule = getattr(row, "rule", None)
        if rule:
            self.edit_rule(rule)

    def _on_fallback(self, row: Adw.ComboRow, _param) -> None:
        if self._building:
            return
        pid = self._fallback_ids[row.get_selected()]
        undo = self.state.set_fallback(pid)
        if undo is None:
            return
        self.refresh()
        self.state.toast(f"“{self.state.playlist_name(pid)}” plays when nothing is scheduled", self._undo(undo))

    # -- calendar ---------------------------------------------------------------------------
    def _on_view(self, group: Adw.ToggleGroup, _param) -> None:
        if self._building:
            return
        self._view = group.get_active_name() or "all"
        self.refresh()

    def _show_seasonal(self) -> None:
        monday = self._monday()
        seasonal = model.seasonal_off(self.state.rules, monday)
        if not seasonal:
            return
        target = min(model.next_week_for(rule, monday) for rule in seasonal)
        self._set_week(self._weeks_until(target))


def create(state) -> Page:
    return SchedulePage(state)
