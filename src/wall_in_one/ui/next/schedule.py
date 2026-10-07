"""Schedule: what plays when, as a week calendar and an ordered list of rules.

The list shows the highest priority on top: the stored order reversed, since
the store keeps "the last applicable rule wins" (`schedule_model.shown_index`).
The calendar projects the week with the app's own matching
(`Scheduling.rule_applies`). The "Now" card and its "Why?" are the runtime's
answer (`Scheduling.routes`, its rule id and "until"), never recomputed: the
other rules only get words for why they are not it.

Rules move by their handle (the shared `reorder.ReorderList`, which never
gains or loses a row mid-drag: a rebuild waits for it to settle), by their
menu, or by Ctrl+Up/Down; each move is one relative step in stored order
from where the rule is when it is saved.

While displays are mirrored, a rule aimed at one display is kept but does not
apply; its row says so. A playlist that would play nothing can't be chosen
for a rule or the default (the adapter refuses it and says why).

Every change is a write that settles later (`Scheduling`); a control shows
its queued value until the last write queued for it settles, then what is
durable (`pending_edits`). There is no Undo yet; deleting asks first.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any, Final

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from wall_in_one.ui.next import schedule_model as model
from wall_in_one.ui.next import widgets
from wall_in_one.ui.next.page import Page, not_ported
from wall_in_one.ui.next.reorder import ReorderList
from wall_in_one.ui.next.schedule_calendar import WeekCalendar
from wall_in_one.ui.next.schedule_editor import (
    MISSING,
    RuleEditor,
    playlist_factory,
    playlist_name,
    rule_title,
)
from wall_in_one.ui.next.schedule_state import RouteView, RuleView, Scheduling, Settled
from wall_in_one.ui.next.state import AppState
from wall_in_one.ui.pending_edits import queue_intent, settle_intent
from wall_in_one.ui.plain_text import plain

#: Rows built at a time, like the classic Schedules page.
RULE_PAGE_SIZE: Final = 48
#: The calendar's "now" line moves this often while the page is shown.
CLOCK_SECONDS: Final = 30
#: The intent key of the "When nothing is scheduled" row.
_DEFAULT: Final = "default"

CSS: Final = """
.week-calendar { color: var(--accent-bg-color); }
.schedule-calendar-card {
  background-color: var(--view-bg-color);
  border-radius: 14px;
  box-shadow: 0 0 0 1px alpha(currentColor, 0.09), 0 1px 3px alpha(black, 0.06);
  padding: 4px 6px 4px 0;
}
.schedule-now { padding: 12px 14px 12px 12px; border-radius: 14px; }
.schedule-now .now-name { font-weight: 800; font-size: 1.15em; }
.schedule-cover { border-radius: 8px; }
.schedule-cover-icon { background-color: alpha(currentColor, 0.08); }
.schedule-summary { padding: 14px; }
.preset-chip { border-radius: 999px; padding: 2px 10px; min-height: 0; font-size: 0.9em;
  font-weight: 600; }
.preset-chip.active { background-color: alpha(var(--accent-bg-color), 0.16);
  color: var(--accent-color); }
.day-chip { padding-left: 0; padding-right: 0; font-weight: 600; }
.rules-list > row.rule-row { padding: 0; }
.rule-row .rule-body { padding: 8px 8px 8px 10px; }
.rule-row.off .rule-dim { opacity: 0.45; }
.rule-title { font-weight: 700; }
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
_styled = False


def _style() -> None:
    global _styled
    if not _styled:
        widgets.add_css(CSS)
        _styled = True


class Dot(Gtk.DrawingArea):
    """A playlist's calendar color."""

    def __init__(self, color: str, size: int = 12) -> None:
        super().__init__()
        self._color = color
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_can_target(False)
        self.set_draw_func(self._draw)

    def _draw(
        self, _area: Gtk.DrawingArea, cr: cairo.Context[Any], width: int, height: int
    ) -> None:
        cr.set_source_rgb(*model.rgb(self._color))
        cr.arc(width / 2, height / 2, min(width, height) / 2, 0, math.tau)
        cr.fill()


@dataclass(frozen=True)
class Outcome:
    """Displays that agree on what plays and why (one line of the "Now" card)."""

    connectors: tuple[str, ...]
    route: RouteView


class RuleRow(Gtk.ListBoxRow):
    """One rule in the list."""

    def __init__(self, rule: RuleView) -> None:
        super().__init__(activatable=True)
        self.rule = rule
        self.switch = Gtk.Switch(active=rule.enabled, valign=Gtk.Align.CENTER)
        self.now_pill = widgets.pill("Now", "media-playback-start-symbolic", "accent")
        self.warning = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self.note = Gtk.Label(xalign=0, wrap=True)
        self.menu_button = Gtk.MenuButton(icon_name="view-more-symbolic")
        #: What a drag grabs (`ReorderList`): a picture, never a button.
        self.handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")


class SchedulePage(Page):
    name = "schedule"
    title = "Schedule"

    def __init__(self, state: AppState, scheduling: Scheduling) -> None:
        super().__init__(state)
        _style()
        self.scheduling = scheduling
        self._week = 0  # weeks from the current one
        self._view = "all"  # "all" or a connector
        self.rule_rows: list[RuleRow] = []
        self._limit = RULE_PAGE_SIZE
        self._loading = False
        self._editor: RuleEditor | None = None
        self._layout_key: object = None
        self._refresh_pending = False
        self._force_pending = False
        self._clock_source = 0
        self._narrow = False
        #: Writes still queued, per control (`pending_edits`).
        self._pending_enabled: dict[str, tuple[bool, int]] = {}
        self._pending_default: dict[str, tuple[str, int]] = {}
        #: What "Why?" listed last: (rule id or "", reason, kind).
        self.why_entries: list[tuple[str, str, str]] = []
        self._fallback_ids: list[str] = []
        self._view_connectors: tuple[str, ...] | None = None

        # -- header -------------------------------------------------------------
        self._title = Adw.WindowTitle(title="Schedule")
        nav = Gtk.Box()
        nav.add_css_class("linked")
        nav.append(
            widgets.icon_button("go-previous-symbolic", "Previous week", lambda *_a: self._move(-1))
        )
        nav.append(widgets.icon_button("go-next-symbolic", "Next week", lambda *_a: self._move(1)))
        self._nav = nav
        self._today = Gtk.Button(label="Today")
        self._today.set_tooltip_text("Back to this week")
        self._today.connect("clicked", lambda *_args: self.set_week(0))
        self.add_button = Gtk.Button(
            child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Add rule")
        )
        self.add_button.set_tooltip_text("Add a rule")
        self.add_button.connect("clicked", lambda *_args: self.new_rule())
        self._add_small = widgets.icon_button("list-add-symbolic", "Add rule", self._add_clicked)
        self._add_small.set_visible(False)

        # -- now ------------------------------------------------------------------
        self._now_card = Gtk.Box(spacing=14)
        self._now_card.add_css_class("card")
        self._now_card.add_css_class("schedule-now")
        self._now_icon = Gtk.Image(pixel_size=32, valign=Gtk.Align.CENTER)
        self._now_icon.set_from_icon_name("x-office-calendar-symbolic")
        self._now_card.append(self._now_icon)
        texts = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, valign=Gtk.Align.CENTER
        )
        self._eyebrow = Gtk.Label(xalign=0)
        self._eyebrow.add_css_class("section-label")
        texts.append(self._eyebrow)
        self._now_lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        texts.append(self._now_lines)
        self._now_card.append(texts)
        self.resume_button = Gtk.Button(
            label="Resume schedule", valign=Gtk.Align.CENTER, visible=False
        )
        self.resume_button.add_css_class("pill")
        self.resume_button.add_css_class("suggested-action")
        self.resume_button.set_tooltip_text("Stop your pick and play what the schedule says")
        self.resume_button.connect("clicked", lambda *_args: self._resume())
        self._now_card.append(self.resume_button)
        self._why_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.why_popover = Gtk.Popover(child=self._why_box)
        self.why_popover.add_css_class("why-popover")
        self.why_popover.set_position(Gtk.PositionType.BOTTOM)
        self.why_popover.connect("show", lambda *_args: self.build_why())
        self.why_button = Gtk.MenuButton(popover=self.why_popover, valign=Gtk.Align.CENTER)
        self.why_button.set_child(Adw.ButtonContent(icon_name="help-about-symbolic", label="Why?"))
        self.why_button.set_tooltip_text("Why this playlist now?")
        self._now_card.append(self.why_button)

        # -- calendar -------------------------------------------------------------
        toolbar = Adw.WrapBox(child_spacing=12, line_spacing=8)
        self._views = Adw.ToggleGroup()
        self._views.connect("notify::active-name", self._on_view)
        toolbar.append(self._views)
        self._seasonal = Gtk.Button()
        self._seasonal.add_css_class("flat")
        self._seasonal.set_valign(Gtk.Align.CENTER)
        self._seasonal.connect("clicked", lambda *_args: self._show_seasonal())
        toolbar.append(self._seasonal)
        self._too_many = widgets.dim("", wrap=True)
        self._too_many.set_visible(False)
        toolbar.append(self._too_many)
        self.calendar = WeekCalendar(
            on_edit=self.edit_rule,
            on_create=self.create_from_calendar,
            name_of=lambda rule: rule_title(self.state, rule),
        )
        calendar_card = Gtk.Box(vexpand=True)
        calendar_card.add_css_class("schedule-calendar-card")
        calendar_card.append(self.calendar)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        left.append(toolbar)
        left.append(calendar_card)

        # -- rules ----------------------------------------------------------------
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
        order.set_tooltip_text(
            "Where rules overlap, the higher one plays. Drag a rule by its handle, "
            "use its menu, or Ctrl+↑ / Ctrl+↓."
        )
        heading.append(order)
        rules.append(heading)
        self.blocked_label = Gtk.Label(xalign=0, wrap=True)
        self.blocked_label.add_css_class("warning")
        self.blocked_label.set_visible(False)
        rules.append(self.blocked_label)
        self.rule_list = ReorderList()
        self.rule_list.add_css_class("rules-list")
        self.rule_list.connect("row-activated", self._on_row_activated)
        self.rule_list.connect("reordered", self._on_reordered)
        self.rule_list.connect("settled", self._on_settled)
        self._rules_deferred = False
        empty = Adw.StatusPage(
            icon_name="x-office-calendar-symbolic",
            title="No rules",
            description="Drag on the calendar or add one",
        )
        empty.add_css_class("compact")
        self.rule_list.set_placeholder(empty)
        rules.append(self.rule_list)
        self.more_button = Gtk.Button(halign=Gtk.Align.CENTER, visible=False)
        self.more_button.add_css_class("pill")
        self.more_button.connect("clicked", lambda *_args: self.show_more())
        rules.append(self.more_button)

        fallback_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=4)
        fallback_list.add_css_class("boxed-list")
        self.fallback_row = plain(Adw.ComboRow, title="When nothing is scheduled")
        self.fallback_row.set_title_lines(2)
        self.fallback_row.set_factory(playlist_factory(state, 22))
        self.fallback_row.connect("notify::selected", self._on_fallback)
        fallback_list.append(self.fallback_row)
        rules.append(fallback_list)
        self._rules_clamp = Adw.Clamp(maximum_size=380, tightening_threshold=380, child=rules)
        self._rules_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, hexpand=False
        )
        self._rules_scroller.set_child(self._rules_clamp)
        self._rules_scroller.set_size_request(360, -1)
        self.rule_list.attach(self._rules_scroller)

        # -- layout ---------------------------------------------------------------
        columns = Gtk.Box(spacing=18)
        columns.append(left)
        columns.append(self._rules_scroller)
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_start=18,
            margin_end=18,
            margin_top=12,
            margin_bottom=14,
        )
        body.append(self._now_card)
        body.append(columns)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(body)
        breakpoint_bin = Adw.BreakpointBin(width_request=340, height_request=300)
        breakpoint_bin.set_child(scroller)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 880sp"))
        narrow.add_setter(columns, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self._rules_scroller, "vscrollbar-policy", Gtk.PolicyType.NEVER)
        narrow.add_setter(self._rules_scroller, "width-request", -1)
        narrow.add_setter(self._rules_clamp, "maximum-size", 860)
        narrow.add_setter(self._rules_clamp, "tightening-threshold", 860)
        narrow.add_setter(body, "margin-start", 12)
        narrow.add_setter(body, "margin-end", 12)
        narrow.add_setter(self.add_button, "visible", False)
        narrow.add_setter(self._add_small, "visible", True)
        narrow.connect("apply", lambda *_args: self._set_narrow(True))
        narrow.connect("unapply", lambda *_args: self._set_narrow(False))
        breakpoint_bin.add_breakpoint(narrow)
        breakpoint_bin.connect("map", lambda *_args: self._start_clock())
        breakpoint_bin.connect("unmap", lambda *_args: self._stop_clock())
        self.widget = breakpoint_bin
        self._install_actions()

        state.connect("changed", self._on_changed)
        self.refresh(force=True)

    def _add_clicked(self, _button: Gtk.Button) -> None:
        self.new_rule()

    # -- Page API -------------------------------------------------------------------
    def title_widget(self) -> Gtk.Widget:
        return self._title

    def header_start(self) -> list[Gtk.Widget]:
        return [self._nav, self._today]

    def header_end(self) -> list[Gtk.Widget]:
        return [self.add_button, self._add_small]

    def activate(self, argument: str | None) -> None:
        self.refresh()
        what, _, rest = (argument or "").partition(":")
        if what == "why":
            self.set_week(0)
            GLib.idle_add(self._popup_why, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]
        elif what == "new":
            # "schedule:new:HDMI-A-1" from the Displays page.
            connector = rest if rest in self._connectors() else ""
            shown = self._view_connectors or ()
            if connector in shown and self.scheduling.saved_display_mode() == "independent":
                self._views.set_active_name(connector)

            def open_editor() -> bool:
                self.new_rule(connector=connector)
                return False

            GLib.idle_add(open_editor, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def demo(self, scene: str) -> None:
        """Screenshot and smoke scenes; every one goes through `Scheduling` only."""
        if self._editor is not None:
            self._editor.force_close()
        self.why_popover.popdown()
        self.calendar.clear_selection()
        self.rule_list.cancel_drag()
        what, _, argument = scene.partition(":")
        rules = {rule.id: rule for rule in self.scheduling.schedule_rules}
        if what in ("edit", "editend") and argument in rules:
            self.edit_rule(rules[argument])
            if what == "editend" and self._editor is not None:
                GLib.timeout_add(350, self._editor.scroll_to_end)
        elif what == "new":
            self.create_from_calendar([3], 20 * 60, 23 * 60)
        elif what == "why":
            self._popup_why()
        elif what == "drag":
            self.calendar.show_selection(0, 4, 12 * 60, 14 * 60)
        elif what == "display" and argument in (self._view_connectors or ()):
            self._views.set_active_name(argument)
        elif what == "week":
            self.set_week(int(argument or 0))
        elif what == "seasonal":
            self._show_seasonal()
        elif what == "menu":
            for row in self.rule_rows:
                if row.rule.id == argument:
                    row.menu_button.popup()
        elif what == "reorder":
            # Hold a drag: the last rule lifted a little past the middle of the one above.
            rows = self.rule_list.get_rows()
            heights = self.rule_list.row_heights()
            if len(rows) >= 2 and all(heights):
                self.rule_list.begin_drag(rows[-1])
                self.rule_list.update_drag(-(heights[-2] / 2 + heights[-1] / 2) * 1.24)

    # -- dates ------------------------------------------------------------------------
    def _monday(self, offset: int | None = None) -> dt.date:
        weeks = self._week if offset is None else offset
        return model.week_start(self.scheduling.clock().date()) + dt.timedelta(weeks=weeks)

    def _move(self, step: int) -> None:
        self.set_week(self._week + step)

    def set_week(self, offset: int) -> None:
        self._week = offset
        self.refresh()

    def _set_narrow(self, narrow: bool) -> None:
        self._narrow = narrow
        self._today.set_visible(self._week != 0 and not narrow)

    def _start_clock(self) -> None:
        if not self._clock_source:
            self._clock_source = GLib.timeout_add_seconds(CLOCK_SECONDS, self._tick)

    def _stop_clock(self) -> None:
        if self._clock_source:
            GLib.source_remove(self._clock_source)
            self._clock_source = 0

    def _tick(self) -> bool:
        self._refresh_clock()
        return GLib.SOURCE_CONTINUE

    # -- reading ----------------------------------------------------------------------
    def _connectors(self) -> list[str]:
        return [display.connector for display in self.scheduling.known_displays()]

    def _independent(self) -> bool:
        return self.scheduling.saved_display_mode() == "independent"

    def _colors(self) -> dict[str, str]:
        return model.playlist_colors([(p.id, bool(p.automatic)) for p in self.state.playlists])

    def _name(self, pid: str) -> str:
        return playlist_name(self.state, pid)

    # -- refresh ----------------------------------------------------------------------
    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic in ("playlists", "displays", "theme", "settings", "schedule"):
            self._force_pending = True
        elif topic not in ("routes", "now", "clock", "system"):
            return
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.idle_add(self._idle_refresh, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _idle_refresh(self) -> bool:
        force, self._force_pending, self._refresh_pending = self._force_pending, False, False
        self.refresh(force=force)
        return False

    def flush(self) -> None:
        """Run a refresh that is waiting (tests, and before reading what is shown)."""
        if self._refresh_pending:
            self._idle_refresh()

    def refresh(self, force: bool = False) -> None:
        """Cheap when only the clock or the routes moved; rebuilds what changed."""
        scheduling = self.scheduling
        monday = self._monday()
        end = monday + dt.timedelta(days=6)
        first, last = model.MONTHS[monday.month - 1], model.MONTHS[end.month - 1]
        span = (
            f"{monday.day} {first} \u2013 {end.day} {last}"
            if monday.month != end.month
            else f"{monday.day}\u2013{end.day} {last}"
        )
        now = scheduling.clock()
        if self._week == 0:
            self._title.set_subtitle(f"This week · {span}")
        else:
            self._title.set_subtitle(f"{span} {end.year}" if end.year != now.year else span)
        self._today.set_visible(self._week != 0 and not self._narrow)
        independent = self._independent()
        self._build_view_toggles(independent)
        view = self._view if independent else "all"
        key = (
            monday,
            view,
            independent,
            scheduling.default_playlist,
            tuple(sorted(scheduling.display_assignments().items())),
            tuple(scheduling.schedule_rules),
            tuple((p.id, p.name) for p in self.state.playlists),
        )
        if force or key != self._layout_key:
            self._layout_key = key
            self._rebuild(monday, view, independent)
        self._refresh_clock()
        self._refresh_now()

    def _refresh_clock(self) -> None:
        now = self.scheduling.clock()
        self._eyebrow.set_label(f"NOW · {now.strftime('%a').upper()} {now.strftime('%H:%M')}")
        today = now.weekday() if self._week == 0 else None
        self.calendar.set_clock(today, now.hour * 60 + now.minute)

    def _build_view_toggles(self, independent: bool) -> None:
        connectors = tuple(d.connector for d in self.scheduling.known_displays() if d.connected)
        self._views.set_visible(independent and bool(connectors))
        if connectors == self._view_connectors:
            return
        self._view_connectors = connectors
        self._loading = True
        self._views.remove_all()
        self._views.add(Adw.Toggle(name="all", label="All displays"))
        for connector in connectors:
            toggle = Adw.Toggle(name=connector, label=connector)
            toggle.set_tooltip(f"What {connector} plays")
            self._views.add(toggle)
        if self._view not in connectors:
            self._view = "all"
        self._views.set_active_name(self._view)
        self._loading = False

    def _rebuild(self, monday: dt.date, view: str, independent: bool) -> None:
        scheduling = self.scheduling
        rules = list(scheduling.schedule_rules)
        applies = scheduling.rule_applies
        colors = self._colors()
        projected = [rule for rule in rules if rule.enabled]
        too_many = len(projected) > model.MAX_PROJECTED_RULES
        self._too_many.set_visible(too_many)
        self._too_many.set_label(
            f"{len(projected)} rules are more than the calendar draws; the list is complete"
        )
        assignments = scheduling.display_assignments()
        connected = [d.connector for d in scheduling.known_displays() if d.connected]
        if too_many:
            spans: list[list[model.Span]] = [[] for _ in range(7)]
            exceptions: list[model.DisplayException] = []
        elif view == "all":
            spans = model.week_spans(rules, monday, "", applies)
            exceptions = model.week_exceptions(rules, monday, applies) if independent else []
        else:
            own = rules
            if scheduling.beats_global_rules(view):
                # Its own playlist beats the rules for every display.
                own = [rule for rule in rules if rule.connector == view]
            spans = model.week_spans(own, monday, view, applies)
            exceptions = []
        if view == "all":
            targets = connected if independent else connected[:1]
            unscheduled = {c: assignments.get(c) or scheduling.default_playlist for c in targets}
            if len(set(unscheduled.values())) > 1:
                label = "Per display"
                tip = " · ".join(f"{c}: {self._name(p)}" for c, p in unscheduled.items())
            else:
                label = self._name(scheduling.default_playlist)
                if unscheduled:
                    label = self._name(next(iter(unscheduled.values())))
                tip = f"{label} plays"
        else:
            label = self._name(assignments.get(view) or scheduling.default_playlist)
            tip = f"{label} plays on {view}"
        self.calendar.update(spans, exceptions, monday, colors, label, tip)

        seasonal = model.seasonal_off(rules, monday)
        self._seasonal.set_visible(bool(seasonal))
        if seasonal:
            months = model.months_text({m for rule in seasonal for m in rule.months})
            count = len(seasonal)
            text = (
                "1 seasonal rule not active this week"
                if count == 1
                else f"{count} seasonal rules not active this week"
            )
            self._seasonal.set_child(
                Adw.ButtonContent(icon_name="x-office-calendar-symbolic", label=text)
            )
            self._seasonal.set_tooltip_text(f"{months} — show the next week it applies")
        never = (
            set()
            if too_many
            else model.never_winning(rules, monday, connected if independent else [], applies)
        )
        self._rebuild_rules(rules, never, independent)
        self._rebuild_fallback(independent)

    # -- now --------------------------------------------------------------------------
    def outcomes(self) -> list[Outcome]:
        """The runtime's routes, grouped where displays agree (one group while mirrored)."""
        groups: list[Outcome] = []
        for route in self.scheduling.routes():
            key = (route.route, route.playlist_id, route.rule_id, route.until)
            for index, group in enumerate(groups):
                other = group.route
                if (other.route, other.playlist_id, other.rule_id, other.until) == key:
                    groups[index] = Outcome((*group.connectors, route.connector), other)
                    break
            else:
                groups.append(Outcome((route.connector,), route))
        return groups

    def _refresh_now(self) -> None:
        child = self._now_lines.get_first_child()
        while child is not None:
            self._now_lines.remove(child)
            child = self._now_lines.get_first_child()
        groups = self.outcomes()
        player = self.state.player()
        if not groups:
            text = (
                "Checking the wallpaper service…"
                if player.service == "checking"
                else "The wallpaper service isn\u2019t running, so nothing changes on schedule"
            )
            self._now_lines.append(widgets.dim(text))
            self.resume_button.set_visible(False)
            self.why_button.set_sensitive(False)
            return
        self.why_button.set_sensitive(True)
        multi = len(groups) > 1
        for group in groups:
            route = group.route
            line = Gtk.Box(spacing=8)
            if multi:
                tag = widgets.pill(" · ".join(group.connectors), "video-display-symbolic", "subtle")
                tag.set_valign(Gtk.Align.CENTER)
                line.append(tag)
            title = Gtk.Label(xalign=0, wrap=True)
            title.add_css_class("now-name")
            name = "" if route.quick else route.playlist
            if route.route == "pick":
                title.set_markup(f"<span weight='800'>{escape(name or 'Your pick')}</span>")
                line.append(title)
                note = Gtk.Label(label="your pick", xalign=0)
                note.add_css_class("pick-note")
                line.append(note)
                self._now_lines.append(line)
                continue
            markup = f"<span weight='800'>{escape(name)}</span>"
            if route.until:
                markup += f"<span weight='400' alpha='70%'>  until {escape(route.until)}</span>"
            title.set_markup(markup)
            line.append(title)
            self._now_lines.append(line)
            if multi:
                continue
            detail = Gtk.Box(spacing=6)
            rule = self.scheduling.schedule_rule(route.rule_id) if route.rule_id else None
            if route.route == "schedule" and rule is not None:
                detail.append(Dot(self._colors().get(rule.playlist, model.FALLBACK_COLOR), 10))
                text = f"{rule.name} · {model.summary(rule)}" if rule.name else model.summary(rule)
            elif route.route == "schedule":
                text = "From the schedule"
            elif route.route == "display":
                text = f"Nothing scheduled · {group.connectors[0]}\u2019s own playlist"
            else:
                text = "Nothing scheduled right now"
            label = Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            label.add_css_class("dimmed")
            detail.append(label)
            self._now_lines.append(detail)
        self.resume_button.set_visible(any(g.route.route == "pick" for g in groups))
        if self.why_popover.get_visible():
            self.build_why()
        playing = {
            g.route.rule_id for g in groups if g.route.route == "schedule" and g.route.rule_id
        }
        for row in self.rule_rows:
            row.now_pill.set_visible(row.rule.id in playing)

    def _resume(self) -> None:
        controls = self.state.controls
        if controls is not None:
            controls.resume_schedule("all")

    # -- why --------------------------------------------------------------------------
    def _popup_why(self) -> bool:
        if self.why_button.get_sensitive():
            self.why_button.popup()
        return False

    def build_why(self) -> None:
        """What plays, the rule the runtime says decides it, what it beats, and the rest."""
        child = self._why_box.get_first_child()
        while child is not None:
            self._why_box.remove(child)
            child = self._why_box.get_first_child()
        self.why_entries = []
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
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            max_content_height=560,
        )
        scroller.set_child(content)
        self._why_box.append(scroller)
        groups = self.outcomes()
        if not groups:
            content.append(widgets.dim("The wallpaper service hasn\u2019t said."))
            return
        scheduling = self.scheduling
        rules = list(reversed(scheduling.schedule_rules))
        at = scheduling.clock()
        mirrored = not self._independent()
        first = groups[0].route
        heading = (
            f"Why {('your pick' if first.route == 'pick' and first.quick else first.playlist)}?"
            if len(groups) == 1
            else "Why these playlists?"
        )
        title = Gtk.Label(label=heading, xalign=0, wrap=True)
        title.add_css_class("title-4")
        content.append(title)
        content.append(
            widgets.dim(
                f"{model.DAYS_LONG[at.weekday()]} {at.strftime('%H:%M')} · top rule wins",
                wrap=False,
            )
        )
        in_play: set[str] = set()
        for group in groups:
            route = group.route
            if len(groups) > 1:
                label = Gtk.Label(label=" · ".join(group.connectors).upper(), xalign=0)
                label.add_css_class("section-label")
                content.append(label)
            if route.route == "pick":
                content.append(widgets.dim("Your pick replaces the schedule until you resume it."))
                self.why_entries.append(("", "Your pick", "playing"))
                continue
            connector = "" if mirrored else group.connectors[0]
            active = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            active.add_css_class("boxed-list")
            active.connect("row-activated", self._why_edit)
            winner = route.rule_id if route.route == "schedule" else None
            for rule in rules:
                text, kind = model.reason(
                    rule, at, connector, winner, scheduling.rule_applies, mirrored=mirrored
                )
                if kind in ("playing", "overridden"):
                    active.append(self._why_row(rule, text, kind))
                    self.why_entries.append((rule.id, text, kind))
                    in_play.add(rule.id)
            if winner is None or winner not in {rule.id for rule in rules}:
                active.append(self._why_unscheduled(route, group.connectors[0]))
            content.append(active)
        idle = [rule for rule in rules if rule.id not in in_play]
        if idle:
            label = Gtk.Label(label="NOT NOW", xalign=0, margin_top=2)
            label.add_css_class("section-label")
            content.append(label)
            quiet = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            quiet.add_css_class("boxed-list")
            quiet.connect("row-activated", self._why_edit)
            connector = "" if mirrored else groups[0].connectors[0]
            for rule in idle:
                text, kind = model.reason(
                    rule,
                    at,
                    rule.connector or connector,
                    None,
                    scheduling.rule_applies,
                    mirrored=mirrored,
                )
                if kind == "overridden":
                    # It applies but is not the runtime's answer for this display.
                    text = "Not chosen now"
                quiet.append(self._why_row(rule, text, kind, compact=True))
                self.why_entries.append((rule.id, text, kind))
            content.append(quiet)

    def _why_unscheduled(self, route: RouteView, connector: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False)
        inner = Gtk.Box(spacing=10)
        inner.add_css_class("why-row")
        inner.add_css_class("winner")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        name = Gtk.Label(label=route.playlist, xalign=0)
        name.add_css_class("rule-title")
        texts.append(name)
        why = (
            f"No rule now · {connector}\u2019s own playlist"
            if route.route == "display"
            else "From the schedule"
            if route.route == "schedule"
            else "No rule now · the default"
        )
        sub = Gtk.Label(label=why, xalign=0)
        sub.add_css_class("caption")
        sub.add_css_class("dimmed")
        texts.append(sub)
        inner.append(texts)
        pill = Gtk.Label(label="Playing", valign=Gtk.Align.CENTER)
        pill.add_css_class("why-reason")
        pill.add_css_class("playing")
        inner.append(pill)
        row.set_child(inner)
        self.why_entries.append(("", why, "playing"))
        return row

    def _why_row(
        self, rule: RuleView, reason: str, kind: str, compact: bool = False
    ) -> Gtk.ListBoxRow:
        row = RuleRow(rule)
        row.set_tooltip_text("Edit this rule")
        inner = Gtk.Box(spacing=10)
        inner.add_css_class("why-row")
        if kind == "playing":
            inner.add_css_class("winner")
        inner.append(Dot(self._colors().get(rule.playlist, model.FALLBACK_COLOR), 10))
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        name = Gtk.Label(
            label=rule_title(self.state, rule), xalign=0, ellipsize=Pango.EllipsizeMode.END
        )
        if not compact:
            name.add_css_class("rule-title")
        texts.append(name)
        if not compact:
            sub = Gtk.Label(label=model.summary(rule), xalign=0, ellipsize=Pango.EllipsizeMode.END)
            sub.add_css_class("caption")
            sub.add_css_class("dimmed")
            texts.append(sub)
        else:
            row.set_tooltip_text(f"{model.summary(rule)} — click to edit")
        inner.append(texts)
        pill = Gtk.Label(label=reason, valign=Gtk.Align.CENTER)
        pill.add_css_class("why-reason")
        pill.add_css_class(kind)
        inner.append(pill)
        row.set_child(inner)
        return row

    def _why_edit(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if isinstance(row, RuleRow):
            self.why_popover.popdown()
            self.edit_rule(row.rule)

    # -- the rules list ---------------------------------------------------------------
    def _rebuild_rules(self, rules: list[RuleView], never: set[str], independent: bool) -> None:
        if self.rule_list.busy:
            # Never pull rows out from under a drag or a settling row.
            self._rules_deferred = True
            return
        self._rules_deferred = False
        focused = self._focused_rule()
        blocked = self.scheduling.schedule_blocked()
        self.blocked_label.set_label(blocked)
        self.blocked_label.set_visible(bool(blocked))
        self.add_button.set_sensitive(not blocked)
        self._add_small.set_sensitive(not blocked)
        self.rule_list.remove_all()
        self.rule_rows = []
        shown = list(reversed(rules))
        pin = focused.id if focused is not None else ""
        visible = shown[: self._limit]
        if pin and not any(rule.id == pin for rule in visible):
            visible += [rule for rule in shown if rule.id == pin]
        monday = self._monday()
        seasonal = {rule.id for rule in model.seasonal_off(rules, monday)}
        for rule in visible:
            index = shown.index(rule)
            row = self._rule_row(
                rule,
                first=index == 0,
                last=index == len(shown) - 1,
                seasonal=rule.id in seasonal,
                never=rule.id in never,
                dormant=bool(rule.connector) and not independent,
                missing=self._name(rule.playlist) == MISSING,
                blocked=blocked,
            )
            self.rule_list.append(row, None if blocked else row.handle)
            self.rule_rows.append(row)
        remaining = len(shown) - len(visible)
        self.more_button.set_visible(remaining > 0)
        if remaining > 0:
            amount = min(RULE_PAGE_SIZE, remaining)
            self.more_button.set_label(f"Show {amount} more · {len(visible)} of {len(shown)}")
        if focused is not None:
            GLib.idle_add(  # type: ignore[call-arg]
                self._focus_rule, focused.id, priority=GLib.PRIORITY_DEFAULT
            )

    def show_more(self) -> None:
        self._limit += RULE_PAGE_SIZE
        self.refresh(force=True)

    def _focused_rule(self) -> RuleView | None:
        root = self.widget.get_root()
        widget = root.get_focus() if isinstance(root, Gtk.Window) else None
        while widget is not None:
            if isinstance(widget, RuleRow) and widget in self.rule_rows:
                return widget.rule
            widget = widget.get_parent()
        return None

    def _focus_rule(self, rule_id: str) -> bool:
        for row in self.rule_rows:
            if row.rule.id == rule_id:
                row.grab_focus()
        return False

    def row(self, rule_id: str) -> RuleRow | None:
        return next((row for row in self.rule_rows if row.rule.id == rule_id), None)

    def _rule_row(
        self,
        rule: RuleView,
        *,
        first: bool,
        last: bool,
        seasonal: bool,
        never: bool,
        dormant: bool,
        missing: bool,
        blocked: str,
    ) -> RuleRow:
        row = RuleRow(rule)
        row.add_css_class("rule-row")
        pending = self._pending_enabled.get(rule.id)
        enabled = pending[0] if pending is not None else rule.enabled
        if not enabled or dormant:
            row.add_css_class("off")
        row.set_tooltip_text("Edit rule")
        body = Gtk.Box(spacing=8)
        body.add_css_class("rule-body")
        row.handle.set_tooltip_text("Drag up or down to change priority (Ctrl+↑ / Ctrl+↓)")
        row.handle.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"Reorder {rule_title(self.state, rule)}"]
        )
        row.handle.set_visible(not blocked)
        body.append(row.handle)
        dot = Dot(self._colors().get(rule.playlist, model.FALLBACK_COLOR), 14)
        dot.add_css_class("rule-dim")
        body.append(dot)
        texts = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER
        )
        texts.add_css_class("rule-dim")
        top = Gtk.Box(spacing=6)
        title = Gtk.Label(
            label=rule_title(self.state, rule), xalign=0, ellipsize=Pango.EllipsizeMode.END
        )
        title.add_css_class("rule-title")
        top.append(title)
        row.now_pill.set_valign(Gtk.Align.CENTER)
        row.now_pill.set_visible(False)
        top.append(row.now_pill)
        row.warning.add_css_class("warning")
        row.warning.set_pixel_size(14)
        row.warning.set_tooltip_text("Never plays this week: rules above cover all of its times")
        row.warning.set_visible(never and not dormant)
        top.append(row.warning)
        texts.append(top)
        summary = model.summary(rule)
        if rule.name:
            summary = f"{self._name(rule.playlist)} · {summary}"
        line = Gtk.Label(label=summary, xalign=0, wrap=True, lines=2)
        line.set_ellipsize(Pango.EllipsizeMode.END)
        line.add_css_class("caption")
        line.add_css_class("dimmed")
        if seasonal:
            line.set_tooltip_text("Not active this week")
        texts.append(line)
        notes = []
        if missing:
            notes.append(f"Missing playlist {rule.playlist}")
        if dormant:
            notes.append("Only while displays run separately")
        row.note.set_label(" · ".join(notes))
        row.note.add_css_class("caption")
        row.note.add_css_class("warning" if missing else "dimmed")
        row.note.set_visible(bool(notes))
        texts.append(row.note)
        body.append(texts)

        row.switch.set_active(enabled)
        row.switch.set_tooltip_text(blocked or ("Rule on" if enabled else "Rule off"))
        row.switch.set_sensitive(not blocked)
        row.switch.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"Use rule for {rule_title(self.state, rule)}"]
        )
        row.switch.connect("notify::active", self._on_switch, rule.id)
        body.append(row.switch)

        menu = Gio.Menu()
        edit = Gio.Menu()
        edit.append("Edit…", f"sched.edit::{rule.id}")
        edit.append("Duplicate", f"sched.duplicate::{rule.id}")
        menu.append_section(None, edit)
        moves = Gio.Menu()
        if not first:
            moves.append("Move up", f"sched.up::{rule.id}")
        if not last:
            moves.append("Move down", f"sched.down::{rule.id}")
        menu.append_section(None, moves)
        delete = Gio.Menu()
        delete.append("Delete…", f"sched.delete::{rule.id}")
        menu.append_section(None, delete)
        row.menu_button.set_menu_model(menu)
        row.menu_button.set_valign(Gtk.Align.CENTER)
        row.menu_button.add_css_class("flat")
        row.menu_button.add_css_class("circular")
        row.menu_button.set_tooltip_text("More")
        row.menu_button.set_sensitive(not blocked)
        body.append(row.menu_button)
        row.set_child(body)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._row_key, rule.id)
        row.add_controller(keys)
        return row

    def _row_key(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        modifiers: Gdk.ModifierType,
        rule_id: str,
    ) -> bool:
        if self.scheduling.schedule_blocked():
            return False
        if modifiers & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            self.move_rule(rule_id, 1 if keyval == Gdk.KEY_Up else -1)
            return True
        if keyval == Gdk.KEY_Delete:
            self.confirm_delete(rule_id)
            return True
        return False

    def _on_reordered(self, _list: ReorderList, row: object, origin: int, target: int) -> None:
        """A drag settled: one relative move in stored order, from where the rule is saved."""
        if not isinstance(row, RuleRow) or origin == target:
            return
        now = [shown.rule.id for shown in self.rule_list.get_rows() if isinstance(shown, RuleRow)]
        before = list(now)
        before.insert(origin, before.pop(target))
        order = [rule.id for rule in reversed(self.scheduling.schedule_rules)]
        if row.rule.id not in order or before[target] not in order:
            return
        step = model.stored_step(order.index(row.rule.id), order.index(before[target]))
        if step:
            self.scheduling.shift_rule(row.rule.id, step)

    def _on_settled(self, _list: ReorderList) -> None:
        if self._rules_deferred:
            self.refresh(force=True)

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if isinstance(row, RuleRow):
            self.edit_rule(row.rule)

    # -- "When nothing is scheduled" ---------------------------------------------------
    def _rebuild_fallback(self, independent: bool) -> None:
        scheduling = self.scheduling
        ids = [playlist.id for playlist in self.state.playlists]
        pending = self._pending_default.get(_DEFAULT)
        wanted = pending[0] if pending is not None else scheduling.default_playlist
        if wanted not in ids:
            ids.append(wanted)
        self._loading = True
        if ids != self._fallback_ids:
            self._fallback_ids = ids
            self.fallback_row.set_model(Gtk.StringList.new(ids))
        self.fallback_row.set_selected(ids.index(wanted))
        self._loading = False
        own = sorted(scheduling.display_assignments().items()) if independent else []
        blocked = scheduling.settings_blocked()
        self.fallback_row.set_sensitive(not blocked)
        self.fallback_row.set_subtitle(
            blocked or " · ".join(f"{c} uses {self._name(p)}" for c, p in own)
        )

    def _on_fallback(self, row: Adw.ComboRow, _param: object) -> None:
        if self._loading:
            return
        index = row.get_selected()
        if not 0 <= index < len(self._fallback_ids):
            return
        pid = self._fallback_ids[index]
        if pid == self.scheduling.default_playlist and _DEFAULT not in self._pending_default:
            return
        queue_intent(self._pending_default, _DEFAULT, pid)

        def settled(saved: bool) -> None:
            # The queued choice first, before the toast (it reaches the window).
            if settle_intent(self._pending_default, _DEFAULT):
                self._rebuild_fallback(self._independent())
            if saved:
                self.state.toast(f"“{self._name(pid)}” plays when nothing is scheduled")

        self.scheduling.choose_default_playlist(pid, settled)

    # -- actions ------------------------------------------------------------------------
    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()

        def add(name: str, callback: Callable[[str], None]) -> None:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, value: callback(value.get_string()))
            group.add_action(action)

        def rule(callback: Callable[[RuleView], None]) -> Callable[[str], None]:
            def run(rule_id: str) -> None:
                found = self.scheduling.schedule_rule(rule_id)
                if found is not None:
                    callback(found)

            return run

        add("edit", rule(self.edit_rule))
        add("duplicate", lambda rule_id: self.duplicate_rule(rule_id))
        add("up", lambda rule_id: self.move_rule(rule_id, 1))
        add("down", lambda rule_id: self.move_rule(rule_id, -1))
        add("delete", self.confirm_delete)
        self.widget.insert_action_group("sched", group)

    def _on_switch(self, switch: Gtk.Switch, _param: object, rule_id: str) -> None:
        if self._loading:
            return
        wanted = switch.get_active()
        queue_intent(self._pending_enabled, rule_id, wanted)

        def settled(_saved: bool) -> None:
            if settle_intent(self._pending_enabled, rule_id):
                self._reconcile_switch(rule_id)

        self.scheduling.enable_rule(rule_id, wanted, settled)

    def _reconcile_switch(self, rule_id: str) -> None:
        """Show whether the rule is durably on, on the switch now on screen."""
        rule = self.scheduling.schedule_rule(rule_id)
        row = self.row(rule_id)
        if row is None or rule is None:
            return
        self._loading = True
        row.switch.set_active(rule.enabled)
        self._loading = False
        if rule.enabled:
            row.remove_css_class("off")
        else:
            row.add_css_class("off")

    def _present(self, editor: RuleEditor) -> None:
        if self._editor is not None:
            self._editor.force_close()
        self._editor = editor

        def closed(dialog: Adw.Dialog) -> None:
            if self._editor is dialog:
                self._editor = None

        editor.connect("closed", closed)
        editor.present(self.widget)

    @property
    def editor(self) -> RuleEditor | None:
        return self._editor

    def edit_rule(self, rule: RuleView) -> None:
        self._present(
            RuleEditor(
                self.state, self.scheduling, rule, on_save=self.save, on_delete=self._delete_rule
            )
        )

    def new_rule(
        self,
        *,
        weekdays: frozenset[int] = frozenset(),
        start: int | None = 9 * 60,
        end: int | None = 17 * 60,
        connector: str = "",
    ) -> bool:
        if not connector and self._view != "all" and self._independent():
            connector = self._view
        draft = RuleView(
            "",
            self._suggest_playlist(),
            weekdays=weekdays,
            start=start,
            end=end,
            connector=connector,
        )
        self._present(
            RuleEditor(
                self.state,
                self.scheduling,
                None,
                prefill=draft,
                on_save=self.save,
                on_delete=self._delete_rule,
            )
        )
        return False

    def _suggest_playlist(self) -> str:
        used = {rule.playlist for rule in self.scheduling.schedule_rules}
        mine = [p for p in self.state.playlists if not p.automatic]
        for playlist in mine:
            if playlist.id not in used and not self.scheduling.playlist_blocked(playlist.id):
                return playlist.id
        for playlist in self.state.playlists:
            if not self.scheduling.playlist_blocked(playlist.id):
                return playlist.id
        return self.state.playlists[0].id if self.state.playlists else ""

    def create_from_calendar(self, days: Sequence[int], start: int, end: int) -> None:
        """A drag or click on the calendar: a new rule for those days and times.

        A drag with no width is refused here; a whole-day drag is "all day",
        never a window that starts and ends at midnight.
        """
        window = model.window_from_drag(start, end)
        if window is None:
            self.state.toast("Drag down over the times the rule should cover")
            return
        self.new_rule(weekdays=frozenset(days), start=window[0], end=window[1])

    def save(self, draft: RuleView, original: RuleView | None, settled: Settled) -> None:
        def done(saved: bool) -> None:
            # The editor first: it holds "Saving…" until it hears, and the
            # toast below reaches the window, which can raise (AA-3).
            settled(saved)
            if saved:
                if original is None:
                    self.state.toast(
                        f"Added “{rule_title(self.state, draft)}” · "
                        f"{model.summary(draft, overnight=False)}"
                    )
                else:
                    self.state.toast("Rule saved")

        self.scheduling.save_rule(original.id if original is not None else "", draft, done)

    def confirm_delete(self, rule_id: str) -> None:
        rule = self.scheduling.schedule_rule(rule_id)
        if rule is None:
            return
        dialog = Adw.AlertDialog(
            heading="Delete this rule?",
            body=(
                f"“{rule_title(self.state, rule)}” · {model.summary(rule)}. "
                "This can\u2019t be undone."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def done(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "delete":
                self.delete_rule(rule_id)

        dialog.connect("response", done)
        dialog.present(self.widget)

    def _delete_rule(self, rule: RuleView) -> None:
        """From the editor's own Delete button, which the person already confirmed there."""
        self.confirm_delete(rule.id)

    def delete_rule(self, rule_id: str) -> None:
        rule = self.scheduling.schedule_rule(rule_id)

        def done(saved: bool) -> None:
            if saved and rule is not None:
                self.state.toast(f"Deleted “{rule_title(self.state, rule)}” rule")

        self.scheduling.remove_rule(rule_id, done)

    def duplicate_rule(self, rule_id: str) -> None:
        def done(saved: bool) -> None:
            if saved:
                self.state.toast("Rule duplicated")

        self.scheduling.copy_rule(rule_id, done)

    def move_rule(self, rule_id: str, step: int) -> None:
        """``step`` +1 is one place up the list: higher priority, later in stored order."""
        rules = list(self.scheduling.schedule_rules)
        index = next((at for at, rule in enumerate(rules) if rule.id == rule_id), None)
        if index is None or not 0 <= index + step < len(rules):
            return

        def done(saved: bool) -> None:
            if saved:
                GLib.idle_add(  # type: ignore[call-arg]
                    self._focus_rule, rule_id, priority=GLib.PRIORITY_DEFAULT
                )

        self.scheduling.shift_rule(rule_id, step, done)

    # -- calendar ---------------------------------------------------------------------
    def _on_view(self, group: Adw.ToggleGroup, _param: object) -> None:
        if self._loading:
            return
        self._view = group.get_active_name() or "all"
        self.refresh()

    def _show_seasonal(self) -> None:
        monday = self._monday()
        seasonal = model.seasonal_off(self.scheduling.schedule_rules, monday)
        if not seasonal:
            return
        target = min(model.next_week_for(rule, monday) for rule in seasonal)
        self.set_week((target - self._monday(0)).days // 7)


def create(state: AppState) -> Page:
    scheduling = state.scheduling
    if scheduling is None:
        return not_ported("schedule", "Schedule", "x-office-calendar-symbolic")(state)
    return SchedulePage(state, scheduling)
