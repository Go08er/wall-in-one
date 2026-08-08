"""Display assignments and ordered calendar overrides."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gtk

from wall_in_one.library import displays, schedules

if TYPE_CHECKING:
    from wall_in_one.session import Session
    from wall_in_one.ui.app import Application


def _connected_outputs() -> tuple[str, ...]:
    """Connector names already known by GTK, without blocking on a subprocess."""
    display = Gdk.Display.get_default()
    if display is None:
        return ()
    monitors = display.get_monitors()
    found: list[str] = []
    for index in range(monitors.get_n_items()):
        monitor = monitors.get_item(index)
        connector = monitor.get_connector() if isinstance(monitor, Gdk.Monitor) else None
        if connector:
            found.append(connector)
    return tuple(found)


class SchedulesPage(Gtk.ScrolledWindow):
    """Choose the default, per-screen playlists, and timed overrides."""

    def __init__(self, application: Application) -> None:
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._app = application
        self._session: Session | None = None
        self._loading = False
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._content.set_margin_top(18)
        self._content.set_margin_bottom(24)
        self._content.set_margin_start(24)
        self._content.set_margin_end(24)
        self.set_child(self._content)

    def refresh(self, session: Session) -> None:
        self._session = session
        self._loading = True
        try:
            while (child := self._content.get_first_child()) is not None:
                self._content.remove(child)
            title = Gtk.Label(label="Display schedules", xalign=0.0)
            title.add_css_class("title-1")
            self._content.append(title)
            intro = Gtk.Label(
                label=(
                    "Pick a normal rotation, then add calendar overrides. Rules are read from "
                    "top to bottom; the last matching rule wins."
                ),
                xalign=0.0,
                wrap=True,
            )
            intro.add_css_class("dim-label")
            self._content.append(intro)
            self._content.append(self._build_defaults(session))
            self._content.append(self._build_displays(session))
            self._content.append(self._build_rules(session))
            self._content.append(self._build_new_rule(session))
        finally:
            self._loading = False

    def _build_defaults(self, session: Session) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Default rotation",
            description="Used whenever no schedule rule matches.",
        )
        choices = session.playlists.all()
        model = Gtk.StringList.new(["Whole library", *(one.name for one in choices)])
        row = Adw.ComboRow(title="Playlist", model=model)
        selected = 0
        for index, playlist in enumerate(choices, start=1):
            if playlist.id == session.settings.active_playlist:
                selected = index
                break
        row.set_selected(selected)
        row.connect("notify::selected", self._make_default_changed(choices))
        group.add(row)
        return group

    def _build_displays(self, session: Session) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Screens",
            description=(
                "A screen can follow the default or pin one playlist. Detached screens stay "
                "visible so dock setups are not forgotten."
            ),
        )
        connected = set(_connected_outputs())
        assigned = dict(session.displays.all())
        connectors = tuple(sorted(connected | set(assigned)))
        if not connectors:
            group.add(
                Adw.ActionRow(
                    title="All outputs",
                    subtitle="No named connector is available; wallpapers apply everywhere.",
                )
            )
            return group
        choices = session.playlists.all()
        names = ["Follow default", *(one.name for one in choices)]
        for connector in connectors:
            row = Adw.ComboRow(
                title=connector,
                subtitle="Not attached" if connector not in connected else "Connected",
                model=Gtk.StringList.new(names),
            )
            wanted = assigned.get(connector, "")
            selected = 0
            for index, playlist in enumerate(choices, start=1):
                if playlist.id == wanted:
                    selected = index
                    break
            row.set_selected(selected)
            row.connect("notify::selected", self._make_display_changed(connector, choices))
            group.add(row)
        return group

    def _build_rules(self, session: Session) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Scheduled overrides",
            description="Lower rules have higher priority when times overlap.",
        )
        names = {playlist.id: playlist.name for playlist in session.playlists.all()}
        if not session.schedules.rules:
            group.add(Adw.ActionRow(title="No scheduled overrides"))
            return group
        total = len(session.schedules.rules)
        for index, rule in enumerate(session.schedules.rules):
            row = Adw.SwitchRow(
                title=names.get(rule.playlist, f"Missing playlist {rule.playlist}"),
                subtitle=f"Priority {index + 1} · {rule.describe()}",
                active=rule.enabled,
            )
            row.connect("notify::active", self._make_enabled(rule.id))
            up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Lower priority")
            up.add_css_class("flat")
            up.set_sensitive(index > 0)
            up.connect("clicked", self._make_move(rule.id, index - 1))
            row.add_suffix(up)
            down = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Higher priority")
            down.add_css_class("flat")
            down.set_sensitive(index + 1 < total)
            down.connect("clicked", self._make_move(rule.id, index + 1))
            row.add_suffix(down)
            remove = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove rule")
            remove.add_css_class("flat")
            remove.connect("clicked", self._make_remove(rule.id))
            row.add_suffix(remove)
            group.add(row)
        return group

    def _build_new_rule(self, session: Session) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Add an override",
            description=(
                "Leave months and weekdays empty for any. A time window needs both ends and "
                "may cross midnight."
            ),
        )
        choices = session.playlists.all()
        self._rule_playlist = Adw.ComboRow(
            title="Playlist",
            model=Gtk.StringList.new([one.name for one in choices] or ["Create a playlist first"]),
        )
        self._rule_playlist.set_sensitive(bool(choices))
        group.add(self._rule_playlist)
        self._months = Adw.EntryRow(title="Months, comma-separated (1-12)")
        group.add(self._months)
        self._weekdays = Adw.EntryRow(title="Weekdays (mon,tue,wed…)")
        group.add(self._weekdays)
        time_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._start = Gtk.Entry(placeholder_text="From 22:00", hexpand=True)
        self._end = Gtk.Entry(placeholder_text="To 06:00", hexpand=True)
        time_box.append(self._start)
        time_box.append(self._end)
        group.add(time_box)
        add = Gtk.Button(label="Add scheduled override")
        add.add_css_class("suggested-action")
        add.set_sensitive(bool(choices))
        add.connect("clicked", lambda _button: self._add_rule(choices))
        group.add(add)
        return group

    def _make_default_changed(self, choices: tuple[Any, ...]) -> Any:
        def changed(row: Adw.ComboRow, _property: object) -> None:
            if self._loading:
                return
            index = row.get_selected()
            wanted = choices[index - 1].id if 0 < index <= len(choices) else ""
            self._app.update_settings(active_playlist=wanted)
            self._app.schedule_edited()

        return changed

    def _make_display_changed(self, connector: str, choices: tuple[Any, ...]) -> Any:
        def changed(row: Adw.ComboRow, _property: object) -> None:
            if self._loading:
                return
            index = row.get_selected()
            try:
                if 0 < index <= len(choices):
                    self._app.session.displays.assign(connector, choices[index - 1].id)
                else:
                    self._app.session.displays.unassign(connector)
            except displays.DisplayError as error:
                self._app.window_report(str(error))
                return
            self._app.window_report(f"Updated {connector}")

        return changed

    def _make_enabled(self, rule_id: str) -> Any:
        def changed(row: Adw.SwitchRow, _property: object) -> None:
            if self._loading:
                return
            try:
                self._app.session.schedules.set_enabled(rule_id, row.get_active())
            except schedules.ScheduleError as error:
                self._app.window_report(str(error))
                return
            self._app.schedule_edited()

        return changed

    def _make_move(self, rule_id: str, position: int) -> Any:
        def move(_button: Gtk.Button) -> None:
            try:
                self._app.session.schedules.move(rule_id, position)
            except schedules.ScheduleError as error:
                self._app.window_report(str(error))
                return
            self._app.schedule_edited()

        return move

    def _make_remove(self, rule_id: str) -> Any:
        def remove(_button: Gtk.Button) -> None:
            self._app.session.schedules.remove(rule_id)
            self._app.schedule_edited()

        return remove

    def _add_rule(self, choices: tuple[Any, ...]) -> None:
        index = self._rule_playlist.get_selected()
        if index >= len(choices):
            return
        months = [one for one in self._months.get_text().split(",") if one.strip()]
        weekdays = [one for one in self._weekdays.get_text().split(",") if one.strip()]
        try:
            self._app.session.schedules.add(
                choices[index].id,
                months=months,
                weekdays=weekdays,
                start=self._start.get_text().strip(),
                end=self._end.get_text().strip(),
            )
        except schedules.ScheduleError as error:
            self._app.window_report(str(error))
            return
        self._app.schedule_edited()
