"""Rule editor: an Adw.Dialog with calendar-style day, time, month and display controls.

It edits a draft; nothing changes until Save. The page owns saving, deleting
and the Undo toasts.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from .. import art, data
from . import schedule_model as model
from .schedule_calendar import WeekPreview

TIMES = [model.hhmm(m) for m in range(0, model.DAY_MINUTES, 15)]


def playlist_factory(size: int = 26) -> Gtk.SignalListItemFactory:
    """List items for a model of playlist ids: mosaic cover + name."""
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, item) -> None:
        box = Gtk.Box(spacing=10)
        image = Gtk.Image(pixel_size=size)
        image.set_overflow(Gtk.Overflow.HIDDEN)
        image.add_css_class("schedule-cover")
        label = Gtk.Label(xalign=0, ellipsize=3)
        box.append(image)
        box.append(label)
        item.set_child(box)

    def bind(_factory, item) -> None:
        pid = item.get_item().get_string()
        playlist = data.PLAYLIST_BY_ID.get(pid)
        box = item.get_child()
        image, label = box.get_first_child(), box.get_last_child()
        if playlist is None:
            label.set_label("Missing playlist")
            image.set_from_icon_name("dialog-warning-symbolic")
            return
        label.set_label(playlist.name)
        if playlist.automatic:
            image.set_from_icon_name(playlist.icon or "view-grid-symbolic")
            image.add_css_class("schedule-cover-icon")
        else:
            image.set_from_paintable(art.mosaic(playlist.cover_keys, 64))
            image.remove_css_class("schedule-cover-icon")

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


class TimeRow(Adw.ActionRow):
    """'From 18:00 ▾' — a 15-minute time list, like calendar apps use."""

    def __init__(self, title: str, value: str, on_change: Callable[[], None]) -> None:
        super().__init__(title=title)
        times = sorted(set(TIMES) | {value})
        self._times = times
        self.dropdown = Gtk.DropDown.new_from_strings(times)
        self.dropdown.set_valign(Gtk.Align.CENTER)
        self.dropdown.set_enable_search(True)
        self.dropdown.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.dropdown.set_selected(times.index(value))
        self.dropdown.add_css_class("numeric")
        self.dropdown.set_tooltip_text(f"{title} time")
        self.dropdown.connect("notify::selected", lambda *_: on_change())
        self.add_suffix(self.dropdown)
        self.set_activatable_widget(self.dropdown)

    @property
    def value(self) -> str:
        return self._times[self.dropdown.get_selected()]


class RuleEditor(Adw.Dialog):
    def __init__(
        self,
        state,
        rule: data.Rule | None,
        *,
        prefill: dict | None = None,
        on_save: Callable[[data.Rule, data.Rule | None], None],
        on_delete: Callable[[data.Rule], None],
    ) -> None:
        super().__init__(content_width=500, content_height=760)
        self.state, self.original = state, rule
        self._on_save, self._on_delete = on_save, on_delete
        self._ready = False
        base = rule or data.Rule("new", "cozy-rain")
        prefill = prefill or {}
        self.draft = data.Rule(
            base.id,
            prefill.get("playlist", base.playlist),
            list(prefill.get("days", base.days)),
            prefill.get("start", base.start),
            prefill.get("end", base.end),
            list(prefill.get("months", base.months)),
            prefill.get("display", base.display),
            base.enabled,
        )
        self.set_title("Edit rule" if rule else "New rule")

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self._save = Gtk.Button(label="Save" if rule else "Add")
        self._save.add_css_class("suggested-action")
        self._save.connect("clicked", lambda *_: self._commit())
        header.pack_end(self._save)

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
        if rule:
            delete = Gtk.Button(label="Delete rule", halign=Gtk.Align.CENTER)
            delete.add_css_class("pill")
            delete.add_css_class("destructive-action")
            delete.connect("clicked", lambda *_: self._delete())
            page.append(delete)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        scroller.set_child(Adw.Clamp(maximum_size=520, child=page))
        self._scroller = scroller
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroller)
        self.set_child(view)
        self.set_default_widget(self._save)
        self._ready = True
        self._refresh()

    # -- sections --------------------------------------------------------------
    def _summary_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("card")
        card.add_css_class("schedule-summary")
        top = Gtk.Box(spacing=12)
        self._cover = Gtk.Image(pixel_size=44)
        self._cover.set_overflow(Gtk.Overflow.HIDDEN)
        self._cover.add_css_class("schedule-cover")
        self._cover.set_valign(Gtk.Align.CENTER)
        top.append(self._cover)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self._name = Gtk.Label(xalign=0, ellipsize=3)
        self._name.add_css_class("title-4")
        self._summary = Gtk.Label(xalign=0, wrap=True)
        self._summary.add_css_class("dimmed")
        texts.append(self._name)
        texts.append(self._summary)
        top.append(texts)
        card.append(top)
        self._preview = WeekPreview()
        card.append(self._preview)
        self._overlap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        card.append(self._overlap)
        return card

    def _group(self, title: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        if title:
            group.set_title(title)
        return group

    def _playlist_group(self) -> Gtk.Widget:
        group = self._group()
        self._playlist_ids = [p.id for p in self.state.playlists]
        if self.draft.playlist not in self._playlist_ids:
            self._playlist_ids.append(self.draft.playlist)
        self._playlist = Adw.ComboRow(title="Playlist")
        self._playlist.set_model(Gtk.StringList.new(self._playlist_ids))
        self._playlist.set_factory(playlist_factory())
        self._playlist.set_selected(self._playlist_ids.index(self.draft.playlist))
        self._playlist.connect("notify::selected", lambda *_: self._changed())
        group.add(self._playlist)
        return group

    def _days_group(self) -> Gtk.Widget:
        group = self._group("Days")
        presets = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self._presets: dict[str, Gtk.Button] = {}
        for key, label, days in (
            ("every", "Every day", list(range(7))),
            ("weekdays", "Weekdays", model.WEEKDAYS),
            ("weekend", "Weekend", model.WEEKEND),
        ):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.add_css_class("preset-chip")
            button.connect("clicked", lambda *_, d=days: self._set_days(d))
            presets.append(button)
            self._presets[key] = button
        group.set_header_suffix(presets)
        chips = Gtk.Box(spacing=6, homogeneous=True)
        self._day_chips: list[Gtk.ToggleButton] = []
        active = set(self.draft.days) if self.draft.days else set(range(7))
        for index, name in enumerate(data.DAYS):
            chip = Gtk.ToggleButton(label=name, active=index in active)
            chip.add_css_class("chip")
            chip.add_css_class("day-chip")
            chip.set_tooltip_text(model.DAY_LONG[index])
            chip.connect("toggled", lambda *_: self._changed())
            chips.append(chip)
            self._day_chips.append(chip)
        group.add(chips)
        return group

    def _time_group(self) -> Gtk.Widget:
        group = self._group("Time")
        timed = model.has_window(self.draft)
        self._all_day = Adw.SwitchRow(title="All day", active=not timed)
        self._all_day.connect("notify::active", lambda *_: self._changed())
        group.add(self._all_day)
        self._from = TimeRow("From", self.draft.start if timed else "09:00", self._changed)
        self._to = TimeRow("To", self.draft.end if timed else "17:00", self._changed)
        group.add(self._from)
        group.add(self._to)
        return group

    def _months_group(self) -> Gtk.Widget:
        group = self._group("Months")
        self._all_year = Adw.SwitchRow(title="All year", active=not self.draft.months)
        self._all_year.connect("notify::active", lambda *_: self._changed())
        group.add(self._all_year)
        grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True, margin_top=10)
        self._month_chips: list[Gtk.ToggleButton] = []
        for index, name in enumerate(data.MONTHS):
            chip = Gtk.ToggleButton(label=name, active=index in self.draft.months)
            chip.add_css_class("chip")
            chip.add_css_class("day-chip")
            chip.set_tooltip_text(model.MONTH_LONG[index])
            chip.connect("toggled", lambda *_: self._changed())
            grid.attach(chip, index % 6, index // 6, 1, 1)
            self._month_chips.append(chip)
        self._months_revealer = Gtk.Revealer(
            child=grid, reveal_child=bool(self.draft.months), transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        group.add(self._months_revealer)
        return group

    def _more_group(self) -> Gtk.Widget:
        group = self._group()
        self._displays = ["", *self.state.connectors()]
        if self.draft.display and self.draft.display not in self._displays:
            self._displays.append(self.draft.display)
        labels = ["All displays", *[c for c in self._displays[1:]]]
        self._display = Adw.ComboRow(title="Display", model=Gtk.StringList.new(labels))
        self._display.set_selected(self._displays.index(self.draft.display))
        if self.state.display_mode == "mirrored":
            self._display.set_subtitle("Used when displays run separately")
        self._display.connect("notify::selected", lambda *_: self._changed())
        group.add(self._display)
        self._enabled = Adw.SwitchRow(title="Use this rule", active=self.draft.enabled)
        self._enabled.connect("notify::active", lambda *_: self._changed())
        group.add(self._enabled)
        return group

    # -- state -----------------------------------------------------------------
    def _set_days(self, days: list[int]) -> None:
        self._ready = False
        for index, chip in enumerate(self._day_chips):
            chip.set_active(index in days)
        self._ready = True
        self._changed()

    def _changed(self) -> None:
        if not self._ready:
            return
        draft = self.draft
        draft.playlist = self._playlist_ids[self._playlist.get_selected()]
        days = [i for i, chip in enumerate(self._day_chips) if chip.get_active()]
        draft.days = [] if len(days) == 7 else days
        self._no_days = not days
        if self._all_day.get_active():
            draft.start = draft.end = None
        else:
            draft.start, draft.end = self._from.value, self._to.value
        months = [i for i, chip in enumerate(self._month_chips) if chip.get_active()]
        self._no_months = not self._all_year.get_active() and not months
        draft.months = [] if self._all_year.get_active() or len(months) == 12 else months
        draft.display = self._displays[self._display.get_selected()]
        draft.enabled = self._enabled.get_active()
        self._refresh()

    def _refresh(self) -> None:
        if not hasattr(self, "_no_days"):
            self._no_days = False
            self._no_months = False
        draft = self.draft
        playlist = data.PLAYLIST_BY_ID.get(draft.playlist)
        self._name.set_label(model.playlist_name(draft.playlist))
        if playlist and not playlist.automatic:
            self._cover.set_from_paintable(art.mosaic(playlist.cover_keys, 96))
        else:
            self._cover.set_from_icon_name("view-grid-symbolic")
        problem = "Pick at least one day" if self._no_days else ("Pick at least one month" if self._no_months else "")
        self._summary.remove_css_class("warning")
        if problem:
            self._summary.set_label(problem)
            self._summary.add_css_class("warning")
        else:
            text = model.summary(draft)
            if not draft.enabled:
                text += " · Off"
            self._summary.set_label(text)
        self._save.set_sensitive(not problem)
        color = model.color_for(self.state, draft.playlist)
        self._preview.set_rule(draft, color, valid=not self._no_days)

        timed = not self._all_day.get_active()
        self._from.set_visible(timed)
        self._to.set_visible(timed)
        if timed:
            start, end = model.minutes(self._from.value), model.minutes(self._to.value)
            hint = "Ends next day" if end < start else ("Same as start: all day" if end == start else "")
            self._to.set_subtitle(hint)
        self._months_revealer.set_reveal_child(not self._all_year.get_active())
        for key, days in (("every", list(range(7))), ("weekdays", model.WEEKDAYS), ("weekend", model.WEEKEND)):
            current = sorted(draft.days) if draft.days else ([] if self._no_days else list(range(7)))
            if current == days:
                self._presets[key].add_css_class("active")
            else:
                self._presets[key].remove_css_class("active")

        # Overlaps, as they'd be with this rule where it sits in the list.
        child = self._overlap.get_first_child()
        while child:
            self._overlap.remove(child)
            child = self._overlap.get_first_child()
        if problem or not draft.enabled:
            self._overlap.set_visible(False)
            return
        listing = [draft if r is self.original else r for r in self.state.rules]
        if self.original is None:
            listing.append(draft)
        monday = model.week_start(self.state.now.date())
        beats, beaten = model.overlaps(listing, draft, monday)
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

    def _commit(self) -> None:
        if self._no_days or self._no_months:
            return
        draft = self.draft
        if draft.start and draft.start == draft.end:
            draft.start = draft.end = None
        self._on_save(draft, self.original)
        self.close()

    def _delete(self) -> None:
        self._on_delete(self.original)
        self.close()
