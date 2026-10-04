"""The rule editor: a dialog with calendar-style day, time, month and display controls.

It edits a draft (`RuleView`); nothing changes until Save, and the dialog
stays open until the save has settled, so a refused or failed save keeps
what was chosen. A window that starts and ends at the same minute, no day,
no month, or a playlist with nothing to play can't be saved, and the editor
says why instead.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Pango

from wall_in_one.ui.next import schedule_model as model
from wall_in_one.ui.next.schedule_calendar import WeekPreview
from wall_in_one.ui.next.schedule_state import MINUTES_IN_A_DAY, RuleView, Scheduling, Settled
from wall_in_one.ui.next.state import AppState

#: Times offered: every quarter hour, as calendar apps do.
TIMES: Final = tuple(range(0, MINUTES_IN_A_DAY, 15))
#: A playlist that is gone, as the dropdowns show it.
MISSING: Final = "Missing playlist"

#: ``(draft, original or None, settled)``.
SaveRule = Callable[[RuleView, RuleView | None, Settled], None]


def playlist_name(state: AppState, pid: str) -> str:
    return state.playlist(pid).name if state.has_playlist(pid) else MISSING


def rule_title(state: AppState, rule: RuleView) -> str:
    """What to call a rule: its own name, else its playlist's."""
    return rule.name or playlist_name(state, rule.playlist)


def playlist_factory(state: AppState, size: int = 26) -> Gtk.SignalListItemFactory:
    """List items for a model of playlist ids: a cover (or icon) and the name."""
    factory = Gtk.SignalListItemFactory()

    def setup(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        box = Gtk.Box(spacing=10)
        image = Gtk.Image(pixel_size=size)
        image.set_overflow(Gtk.Overflow.HIDDEN)
        image.add_css_class("schedule-cover")
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        box.append(image)
        box.append(label)
        item.set_child(box)

    def bind(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        string = item.get_item()
        box = item.get_child()
        if not isinstance(string, Gtk.StringObject) or not isinstance(box, Gtk.Box):
            return
        image, label = box.get_first_child(), box.get_last_child()
        if not isinstance(image, Gtk.Image) or not isinstance(label, Gtk.Label):
            return
        pid = string.get_string()
        if not state.has_playlist(pid):
            label.set_label(MISSING)
            image.set_from_icon_name("dialog-warning-symbolic")
            return
        playlist = state.playlist(pid)
        label.set_label(playlist.name)
        cover = None if playlist.automatic else state.playlist_cover(pid, 64)
        if cover is None:
            image.set_from_icon_name(playlist.icon or "view-list-symbolic")
            image.add_css_class("schedule-cover-icon")
        else:
            image.set_from_paintable(cover)
            image.remove_css_class("schedule-cover-icon")

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


class TimeRow(Adw.ActionRow):
    """``From 18:00 ▾``: a quarter-hour list, plus the rule's own time if it is between."""

    def __init__(self, title: str, value: int, on_change: Callable[[], None]) -> None:
        super().__init__(title=title)
        self._times = sorted({*TIMES, value})
        self.dropdown = Gtk.DropDown.new_from_strings([model.hhmm(t) for t in self._times])
        self.dropdown.set_valign(Gtk.Align.CENTER)
        self.dropdown.set_enable_search(True)
        self.dropdown.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.dropdown.set_selected(self._times.index(value))
        self.dropdown.add_css_class("numeric")
        self.dropdown.set_tooltip_text(f"{title} time")
        self.dropdown.connect("notify::selected", lambda *_args: on_change())
        self.add_suffix(self.dropdown)
        self.set_activatable_widget(self.dropdown)

    @property
    def value(self) -> int:
        return self._times[self.dropdown.get_selected()]

    def set_value(self, value: int) -> None:
        if value in self._times:
            self.dropdown.set_selected(self._times.index(value))


class RuleEditor(Adw.Dialog):
    """Edit a rule, or make one (``rule`` None, a ``prefill`` draft)."""

    def __init__(
        self,
        state: AppState,
        scheduling: Scheduling,
        rule: RuleView | None,
        *,
        prefill: RuleView | None = None,
        on_save: SaveRule,
        on_delete: Callable[[RuleView], None],
    ) -> None:
        super().__init__(content_width=500, content_height=760)
        self.state, self.scheduling, self.original = state, scheduling, rule
        self._on_save, self._on_delete = on_save, on_delete
        self._ready = False
        self._saving = False
        self.draft = (
            rule or prefill or RuleView("", state.playlists[0].id if state.playlists else "")
        )
        self._no_days = False
        self._no_months = False
        self.problem = ""
        self.set_title("Edit rule" if rule else "New rule")

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_args: self.close())
        header.pack_start(cancel)
        self.save_button = Gtk.Button(label="Save" if rule else "Add")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda *_args: self.commit())
        header.pack_end(self.save_button)

        page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=22,
            margin_start=18,
            margin_end=18,
            margin_top=6,
            margin_bottom=24,
        )
        page.append(self._summary_card())
        page.append(self._playlist_group())
        page.append(self._days_group())
        page.append(self._time_group())
        page.append(self._months_group())
        page.append(self._more_group())
        if rule is not None:
            delete = Gtk.Button(label="Delete rule", halign=Gtk.Align.CENTER)
            delete.add_css_class("pill")
            delete.add_css_class("destructive-action")
            delete.connect("clicked", lambda *_args: self._delete())
            delete.set_sensitive(not scheduling.schedule_blocked())
            page.append(delete)

        self._scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True
        )
        self._scroller.set_child(Adw.Clamp(maximum_size=520, child=page))
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self._scroller)
        self.set_child(view)
        self.set_default_widget(self.save_button)
        self._ready = True
        self._refresh()

    # -- sections -----------------------------------------------------------------
    def _summary_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("card")
        card.add_css_class("schedule-summary")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._name.add_css_class("title-4")
        self._summary = Gtk.Label(xalign=0, wrap=True)
        self._summary.add_css_class("dimmed")
        texts.append(self._name)
        texts.append(self._summary)
        card.append(texts)
        self._preview = WeekPreview()
        card.append(self._preview)
        self._overlap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        card.append(self._overlap)
        return card

    def _playlist_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        self._playlist_ids = [playlist.id for playlist in self.state.playlists]
        if self.draft.playlist not in self._playlist_ids:
            self._playlist_ids.append(self.draft.playlist)  # shown as "Missing playlist"
        self.playlist_row = Adw.ComboRow(title="Playlist")
        self.playlist_row.set_model(Gtk.StringList.new(self._playlist_ids))
        self.playlist_row.set_factory(playlist_factory(self.state))
        self.playlist_row.set_selected(self._playlist_ids.index(self.draft.playlist))
        self.playlist_row.connect("notify::selected", lambda *_args: self._changed())
        group.add(self.playlist_row)
        return group

    def _days_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Days")
        presets = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self._presets: dict[frozenset[int], Gtk.Button] = {}
        for label, days in (
            ("Every day", model.EVERY_DAY),
            ("Weekdays", model.WEEKDAYS),
            ("Weekend", model.WEEKEND),
        ):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.add_css_class("preset-chip")
            button.connect("clicked", lambda *_args, chosen=days: self.set_days(chosen))
            presets.append(button)
            self._presets[days] = button
        group.set_header_suffix(presets)
        chips = Gtk.Box(spacing=6, homogeneous=True)
        self.day_chips: list[Gtk.ToggleButton] = []
        active = self.draft.weekdays or model.EVERY_DAY
        for index, name in enumerate(model.DAYS):
            chip = Gtk.ToggleButton(label=name, active=index in active)
            chip.add_css_class("day-chip")
            chip.set_tooltip_text(model.DAYS_LONG[index])
            chip.connect("toggled", lambda *_args: self._changed())
            chips.append(chip)
            self.day_chips.append(chip)
        group.add(chips)
        return group

    def _time_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Time")
        draft = self.draft
        timed = model.has_window(draft)
        self.all_day = Adw.SwitchRow(title="All day", active=not timed)
        self.all_day.connect("notify::active", lambda *_args: self._changed())
        group.add(self.all_day)
        start = draft.start if timed and draft.start is not None else 9 * 60
        end = draft.end if timed and draft.end is not None else 17 * 60
        self.from_row = TimeRow("From", start, self._changed)
        self.to_row = TimeRow("To", end, self._changed)
        group.add(self.from_row)
        group.add(self.to_row)
        return group

    def _months_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Months")
        self.all_year = Adw.SwitchRow(title="All year", active=not self.draft.months)
        self.all_year.connect("notify::active", lambda *_args: self._changed())
        group.add(self.all_year)
        grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True, margin_top=10)
        self.month_chips: list[Gtk.ToggleButton] = []
        for index, name in enumerate(model.MONTHS):
            chip = Gtk.ToggleButton(label=name, active=index + 1 in self.draft.months)
            chip.add_css_class("day-chip")
            chip.set_tooltip_text(model.MONTHS_LONG[index])
            chip.connect("toggled", lambda *_args: self._changed())
            grid.attach(chip, index % 6, index // 6, 1, 1)
            self.month_chips.append(chip)
        self._months_revealer = Gtk.Revealer(
            child=grid,
            reveal_child=bool(self.draft.months),
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
        )
        group.add(self._months_revealer)
        return group

    def _more_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        known = [display.connector for display in self.scheduling.known_displays()]
        self._connectors = ["", *known]
        if self.draft.connector and self.draft.connector not in self._connectors:
            self._connectors.append(self.draft.connector)
        labels = ["All displays", *self._connectors[1:]]
        self.display_row = Adw.ComboRow(title="Display", model=Gtk.StringList.new(labels))
        self.display_row.set_selected(self._connectors.index(self.draft.connector))
        if self.scheduling.saved_display_mode() == "mirrored":
            self.display_row.set_subtitle(
                "A rule for one display applies only while displays run separately"
            )
        self.display_row.connect("notify::selected", lambda *_args: self._changed())
        group.add(self.display_row)
        self.enabled_row = Adw.SwitchRow(title="Use this rule", active=self.draft.enabled)
        self.enabled_row.connect("notify::active", lambda *_args: self._changed())
        group.add(self.enabled_row)
        if self.draft.name:
            named = Adw.ActionRow(title="Name", subtitle=self.draft.name)
            named.set_tooltip_text("Rules are named with “wall-in-one ctl schedule name”")
            group.add(named)
        return group

    # -- state ------------------------------------------------------------------------
    def set_days(self, days: frozenset[int]) -> None:
        self._ready = False
        for index, chip in enumerate(self.day_chips):
            chip.set_active(index in days)
        self._ready = True
        self._changed()

    def _changed(self) -> None:
        if not self._ready:
            return
        days = frozenset(i for i, chip in enumerate(self.day_chips) if chip.get_active())
        months = frozenset(i + 1 for i, chip in enumerate(self.month_chips) if chip.get_active())
        self._no_days = not days
        self._no_months = not self.all_year.get_active() and not months
        timed = not self.all_day.get_active()
        self.draft = replace(
            self.draft,
            playlist=self._playlist_ids[self.playlist_row.get_selected()],
            weekdays=frozenset() if len(days) == 7 else days,
            months=frozenset() if self.all_year.get_active() or len(months) == 12 else months,
            start=self.from_row.value if timed else None,
            end=self.to_row.value if timed else None,
            connector=self._connectors[self.display_row.get_selected()],
            enabled=self.enabled_row.get_active(),
        )
        self._refresh()

    def _refresh(self) -> None:
        draft = self.draft
        self._name.set_label(rule_title(self.state, draft))
        self.problem = (
            "Pick at least one day"
            if self._no_days
            else "Pick at least one month"
            if self._no_months
            else model.window_problem(draft.start, draft.end)
            or self.scheduling.playlist_blocked(draft.playlist)
            or self.scheduling.schedule_blocked()
        )
        self._summary.remove_css_class("warning")
        if self.problem:
            self._summary.set_label(self.problem)
            self._summary.add_css_class("warning")
        else:
            text = model.summary(draft)
            self._summary.set_label(text if draft.enabled else f"{text} · Off")
        self.save_button.set_sensitive(not self.problem and not self._saving)
        color = self._color(draft.playlist)
        applies = self.scheduling.rule_applies
        valid = not (self._no_days or model.window_problem(draft.start, draft.end))
        self._preview.set_coverage(
            model.coverage(draft, applies) if valid else [[] for _ in range(7)], color
        )
        timed = not self.all_day.get_active()
        self.from_row.set_visible(timed)
        self.to_row.set_visible(timed)
        if timed:
            start, end = self.from_row.value, self.to_row.value
            self.to_row.set_subtitle(
                "Ends next day" if end < start else "Same as the start" if end == start else ""
            )
        self._months_revealer.set_reveal_child(not self.all_year.get_active())
        current = draft.weekdays or (frozenset() if self._no_days else model.EVERY_DAY)
        for days, button in self._presets.items():
            if current == days:
                button.add_css_class("active")
            else:
                button.remove_css_class("active")
        self._show_overlaps()

    def _color(self, pid: str) -> str:
        colors = model.playlist_colors([(p.id, bool(p.automatic)) for p in self.state.playlists])
        return colors.get(pid, model.FALLBACK_COLOR)

    def _show_overlaps(self) -> None:
        """What this rule beats and what beats it, with it where it would sit in the list."""
        child = self._overlap.get_first_child()
        while child is not None:
            self._overlap.remove(child)
            child = self._overlap.get_first_child()
        draft = self.draft
        if self.problem or not draft.enabled:
            self._overlap.set_visible(False)
            return
        rules: Sequence[RuleView] = self.scheduling.schedule_rules
        listing = [draft if self.original and r.id == self.original.id else r for r in rules]
        if self.original is None:
            listing.append(draft)
        monday = model.week_start(self.scheduling.clock().date())
        beats, beaten = model.overlaps(
            listing,
            draft,
            monday,
            lambda pid: playlist_name(self.state, pid),
            self.scheduling.rule_applies,
        )
        for icon, prefix, names, css in (
            ("go-up-symbolic", "Overrides", beats, "success"),
            ("go-down-symbolic", "Overridden by", beaten, "dimmed"),
        ):
            if not names:
                continue
            row = Gtk.Box(spacing=6)
            image = Gtk.Image.new_from_icon_name(icon)
            image.set_pixel_size(12)
            image.add_css_class(css)
            row.append(image)
            label = Gtk.Label(label=f"{prefix} {', '.join(names)}", xalign=0, wrap=True)
            label.add_css_class("caption")
            row.append(label)
            row.set_tooltip_text("Where times overlap, the top rule wins")
            self._overlap.append(row)
        self._overlap.set_visible(bool(beats or beaten))

    def scroll_to_end(self) -> bool:
        adjustment = self._scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        return False

    def commit(self) -> None:
        """Save, and close once saved; a refused or failed save leaves the dialog open."""
        if self.problem or self._saving:
            return
        self._saving = True
        self.save_button.set_sensitive(False)
        self.save_button.set_label("Saving…")

        def settled(saved: bool) -> None:
            self._saving = False
            self.save_button.set_label("Save" if self.original else "Add")
            if saved:
                self.close()
            else:
                self._refresh()

        self._on_save(self.draft, self.original, settled)

    def _delete(self) -> None:
        if self.original is not None:
            self._on_delete(self.original)
        self.close()
