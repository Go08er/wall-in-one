"""Display assignments and ordered calendar overrides."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gtk

from wall_in_one import config
from wall_in_one.library import displays, schedules
from wall_in_one.ui import runtime_truth

if TYPE_CHECKING:
    from wall_in_one.session import Session
    from wall_in_one.ui.app import Application


MONTH_LABELS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Stage-one authoring support deliberately cannot emit a schema-3 runtime
# document for independent routing. Keep controls which would claim realtime
# per-display behavior unavailable until schema 4 and the Rust routes land.
INDEPENDENT_RUNTIME_READY = False


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
        self._editing_rule = ""
        self._built = False
        self._fingerprint: object = None
        self._rule_rows: list[Gtk.Widget] = []
        self._playback_row: Adw.ComboRow | None = None
        self._playback_choices: tuple[Any, ...] = ()
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._content.set_margin_top(18)
        self._content.set_margin_bottom(24)
        self._content.set_margin_start(24)
        self._content.set_margin_end(24)
        self.set_child(self._content)

    def refresh(self, session: Session) -> None:
        fingerprint = self._state_fingerprint(session)
        self._session = session
        if self._built and fingerprint == self._fingerprint:
            return
        scroll = self.get_vadjustment().get_value()
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
                    "top to bottom; the last applicable matching rule wins."
                ),
                xalign=0.0,
                wrap=True,
            )
            intro.add_css_class("dim-label")
            self._content.append(intro)
            self._content.append(self._build_playback(session))
            self._content.append(self._build_defaults(session))
            if session.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT:
                self._content.append(self._build_displays(session))
            else:
                dormant = len(session.displays) + sum(
                    bool(rule.connector) for rule in session.schedules.rules
                )
                if dormant:
                    self._content.append(self._build_dormant_displays(dormant))
            self._content.append(self._build_rules(session))
            self._content.append(self._build_new_rule(session))
            self._built = True
            self._fingerprint = fingerprint
        finally:
            self._loading = False
        self.get_vadjustment().set_value(scroll)

    def runtime_status_changed(self, session: Session) -> None:
        """Update the live selector without rebuilding schedule authoring."""
        row = self._playback_row
        if not self._built or row is None:
            return
        truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
        self._loading = True
        try:
            if (
                session.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT
                and not INDEPENDENT_RUNTIME_READY
            ):
                row.set_sensitive(False)
                row.set_subtitle(
                    "Independent authoring is saved, but realtime display routing needs the "
                    "schema-4 service update."
                )
            else:
                row.set_sensitive(True)
                row.set_subtitle(self._playback_description(session, truth))
                row.set_selected(self._playback_selected(session, self._playback_choices, truth))
        finally:
            self._loading = False
        self._fingerprint = self._state_fingerprint(session)

    def _state_fingerprint(self, session: Session) -> object:
        truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
        return (
            session.settings.active_playlist,
            session.settings.display_mode,
            session.settings.theme_source_connector,
            truth if truth is not None else session.manual_playlist,
            session.playlists.all(),
            session.displays.all(),
            session.schedules.rules,
            _connected_outputs(),
        )

    def _build_playback(self, session: Session) -> Gtk.Widget:
        """The on-demand switch, kept above calendar configuration."""
        group = Adw.PreferencesGroup(
            title="Playing now",
            description=(
                "Choose a playlist immediately, or return control to the schedule. "
                "A manual choice lasts until you resume the schedule or restart the service."
            ),
        )
        choices = session.playlists.all()
        truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
        row = Adw.ComboRow(
            title="Active playlist",
            subtitle=self._playback_description(session, truth),
            model=Gtk.StringList.new(["Follow schedule", *(one.name for one in choices)]),
        )
        row.set_selected(self._playback_selected(session, choices, truth))
        if (
            session.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT
            and not INDEPENDENT_RUNTIME_READY
        ):
            row.set_sensitive(False)
            row.set_subtitle(
                "Independent authoring is saved, but realtime display routing needs the "
                "schema-4 service update."
            )
        self._playback_row = row
        self._playback_choices = choices
        row.connect("notify::selected", self._make_playback_changed(choices))
        group.add(row)
        return group

    @staticmethod
    def _build_dormant_displays(records: int) -> Gtk.Widget:
        """One honest row instead of mirrored mode's inactive connector UI."""
        group = Adw.PreferencesGroup(title="Independent display setup")
        noun = "saved item" if records == 1 else "saved items"
        group.add(
            Adw.ActionRow(
                title="Saved and inactive",
                subtitle=(
                    f"{records} {noun} are preserved. Enable independent displays in "
                    "Settings to use them."
                ),
            )
        )
        return group

    @staticmethod
    def _playback_description(session: Session, truth: runtime_truth.RuntimeTruth | None) -> str:
        if truth is None:
            return (
                "Manual override is active. Choose Follow schedule to return calendar control."
                if session.manual_playlist is not None
                else "Following the authored schedule."
            )
        if truth.is_manual:
            return f"Manual override · {truth.playlist} is playing."
        if truth.is_multi_display:
            return "Following schedule · screens use their assigned or default playlists."
        if truth.schedule_rule_id is not None:
            return (
                f"Following schedule · rule {truth.schedule_rule_id} selects "
                f"{truth.scheduled_playlist or truth.playlist}."
            )
        return f"Following schedule · default selects {truth.scheduled_playlist or truth.playlist}."

    @staticmethod
    def _playback_selected(
        session: Session,
        choices: tuple[Any, ...],
        truth: runtime_truth.RuntimeTruth | None,
    ) -> int:
        manual_id = (
            truth.playlist_id
            if truth is not None and truth.is_manual
            else session.manual_playlist
            if truth is None
            else None
        )
        if manual_id is not None:
            for index, playlist in enumerate(choices, start=1):
                if playlist.id == manual_id:
                    return index
        return 0

    def _build_defaults(self, session: Session) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Default rotation",
            description="Used whenever no schedule rule matches and no manual choice is active.",
        )
        choices = session.playlists.all()
        model = Gtk.StringList.new(
            ["All media (built-in playlist)", *(one.name for one in choices)]
        )
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
        connectors = self._schedule_connectors(session)
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
        self._rules_group = Adw.PreferencesGroup(
            title="Scheduled overrides",
            description="Lower rules have higher priority when times overlap.",
        )
        self._populate_rules(session)
        return self._rules_group

    def _populate_rules(self, session: Session) -> None:
        self._rule_rows.clear()
        names = {playlist.id: playlist.name for playlist in session.playlists.all()}
        if not session.schedules.rules:
            empty = Adw.ActionRow(title="No scheduled overrides")
            self._rules_group.add(empty)
            self._rule_rows.append(empty)
            return
        total = len(session.schedules.rules)
        for index, rule in enumerate(session.schedules.rules):
            target = rule.connector or "All displays"
            row = Adw.SwitchRow(
                title=names.get(rule.playlist, f"Missing playlist {rule.playlist}"),
                subtitle=f"{target} · priority {index + 1} · {rule.describe()}",
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
            edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit rule")
            edit.add_css_class("flat")
            edit.connect("clicked", lambda _button, chosen=rule: self._edit_rule(chosen))
            row.add_suffix(edit)
            self._rules_group.add(row)
            self._rule_rows.append(row)

    def _refresh_rules(self) -> None:
        session = self._session
        if session is None:
            return
        for row in self._rule_rows:
            self._rules_group.remove(row)
        self._populate_rules(session)
        self._fingerprint = self._state_fingerprint(session)

    def _build_new_rule(self, session: Session) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Add an override",
            description=(
                "Leave months and weekdays empty for any. A time window needs both ends and "
                "may cross midnight."
            ),
        )
        choices = session.playlists.all()
        self._rule_connectors = self._schedule_connectors(session)
        self._rule_target = Adw.ComboRow(
            title="Displays",
            subtitle=(
                "All displays shares this rule; a connector makes it an independent exception"
            ),
            model=Gtk.StringList.new(
                [
                    "All displays",
                    *(self._target_label(connector) for connector in self._rule_connectors),
                ]
            ),
        )
        self._rule_target.set_sensitive(
            session.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT
        )
        group.add(self._rule_target)
        self._rule_playlist = Adw.ComboRow(
            title="Playlist",
            model=Gtk.StringList.new([one.name for one in choices] or ["Create a playlist first"]),
        )
        self._rule_playlist.set_sensitive(bool(choices))
        group.add(self._rule_playlist)
        group.add(self._label("Months (none selected means all year)"))
        month_grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        self._months: list[Gtk.ToggleButton] = []
        for index, label in enumerate(MONTH_LABELS):
            button = Gtk.ToggleButton(label=label, hexpand=True)
            month_grid.attach(button, index % 6, index // 6, 1, 1)
            self._months.append(button)
        group.add(month_grid)

        group.add(self._label("Days of week (none selected means every day)"))
        weekday_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        weekday_box.set_homogeneous(True)
        self._weekdays: list[Gtk.ToggleButton] = []
        for label in WEEKDAY_LABELS:
            button = Gtk.ToggleButton(label=label)
            weekday_box.append(button)
            self._weekdays.append(button)
        group.add(weekday_box)

        self._time_enabled = Adw.SwitchRow(
            title="Use a time window",
            subtitle="The end is exclusive; an earlier end time wraps past midnight",
        )
        self._time_enabled.connect("notify::active", self._time_window_changed)
        group.add(self._time_enabled)
        self._time_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._start_hour = self._number_picker(24)
        self._start_minute = self._number_picker(60)
        self._end_hour = self._number_picker(24)
        self._end_minute = self._number_picker(60)
        self._time_box.append(self._label("From"))
        self._time_box.append(self._start_hour)
        self._time_box.append(Gtk.Label(label=":"))
        self._time_box.append(self._start_minute)
        self._time_box.append(self._label("Until"))
        self._time_box.append(self._end_hour)
        self._time_box.append(Gtk.Label(label=":"))
        self._time_box.append(self._end_minute)
        self._time_box.set_sensitive(False)
        group.add(self._time_box)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._rule_commit = Gtk.Button(label="Add scheduled override")
        self._rule_commit.add_css_class("suggested-action")
        self._rule_commit.set_sensitive(bool(choices))
        self._rule_commit.connect("clicked", lambda _button: self._save_rule(choices))
        buttons.append(self._rule_commit)
        self._rule_cancel = Gtk.Button(label="Cancel editing")
        self._rule_cancel.set_visible(False)
        self._rule_cancel.connect("clicked", lambda _button: self._clear_rule_editor())
        buttons.append(self._rule_cancel)
        group.add(buttons)
        return group

    def _schedule_connectors(self, session: Session) -> tuple[str, ...]:
        connected = set(_connected_outputs())
        saved = {connector for connector, _playlist in session.displays.all()}
        targeted = {rule.connector for rule in session.schedules.rules if rule.connector}
        if session.settings.theme_source_connector:
            targeted.add(session.settings.theme_source_connector)
        return tuple(sorted(connected | saved | targeted))

    @staticmethod
    def _target_label(connector: str) -> str:
        return connector

    def _selected_rule_connector(self) -> str:
        index = self._rule_target.get_selected()
        return self._rule_connectors[index - 1] if 0 < index <= len(self._rule_connectors) else ""

    @staticmethod
    def _label(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0.0, wrap=True)
        label.add_css_class("dim-label")
        return label

    @staticmethod
    def _number_picker(limit: int) -> Gtk.DropDown:
        return Gtk.DropDown.new_from_strings([f"{value:02d}" for value in range(limit)])

    def _time_window_changed(self, row: Adw.SwitchRow, _property: object) -> None:
        self._time_box.set_sensitive(row.get_active())

    @staticmethod
    def _clock_value(hour: Gtk.DropDown, minute: Gtk.DropDown) -> str:
        return f"{hour.get_selected():02d}:{minute.get_selected():02d}"

    def _make_playback_changed(self, choices: tuple[Any, ...]) -> Any:
        def changed(row: Adw.ComboRow, _property: object) -> None:
            if self._loading:
                return
            if (
                self._app.session.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT
                and not INDEPENDENT_RUNTIME_READY
            ):
                return
            index = row.get_selected()
            started = (
                self._app.activate_playlist_async(choices[index - 1].id)
                if 0 < index <= len(choices)
                else self._app.resume_schedule_async()
            )
            # A ComboRow changes before its callback runs. Put it immediately
            # back on the last service-owned snapshot; the subsequent status
            # response is what makes a successful command visible. A failure
            # therefore cannot leave an optimistic manual/schedule claim.
            self._loading = True
            try:
                truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
                row.set_selected(self._playback_selected(self._app.session, choices, truth))
            finally:
                self._loading = False
            if started:
                # Force the next accepted status to reconcile this row even
                # when the runtime rejected the requested transition.
                self._fingerprint = None

        return changed

    def _make_default_changed(self, choices: tuple[Any, ...]) -> Any:
        def changed(row: Adw.ComboRow, _property: object) -> None:
            if self._loading:
                return
            index = row.get_selected()
            wanted = choices[index - 1].id if 0 < index <= len(choices) else ""
            try:
                self._app.update_settings(active_playlist=wanted)
            except config.ConfigError as error:
                self._app.window_report(f"Default playlist was not saved; nothing changed: {error}")
                self._fingerprint = None
                self.refresh(self._app.session)
                return
            self._app.schedule_edited()
            self._fingerprint = self._state_fingerprint(self._app.session)

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
            self._app.runtime_config_changed()
            self._app.window_report(f"Updated {connector}")
            self._fingerprint = self._state_fingerprint(self._app.session)

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
            self._fingerprint = self._state_fingerprint(self._app.session)

        return changed

    def _make_move(self, rule_id: str, position: int) -> Any:
        def move(_button: Gtk.Button) -> None:
            try:
                self._app.session.schedules.move(rule_id, position)
            except schedules.ScheduleError as error:
                self._app.window_report(str(error))
                return
            self._app.schedule_edited()
            self._refresh_rules()

        return move

    def _make_remove(self, rule_id: str) -> Any:
        def remove(_button: Gtk.Button) -> None:
            self._app.session.schedules.remove(rule_id)
            self._app.schedule_edited()
            self._refresh_rules()

        return remove

    def _save_rule(self, choices: tuple[Any, ...]) -> None:
        index = self._rule_playlist.get_selected()
        if index >= len(choices):
            return
        months = [index for index, button in enumerate(self._months, 1) if button.get_active()]
        weekdays = [
            schedules.WEEKDAY_NAMES[index]
            for index, button in enumerate(self._weekdays)
            if button.get_active()
        ]
        try:
            start = (
                self._clock_value(self._start_hour, self._start_minute)
                if self._time_enabled.get_active()
                else ""
            )
            end = (
                self._clock_value(self._end_hour, self._end_minute)
                if self._time_enabled.get_active()
                else ""
            )
            if self._editing_rule:
                self._app.session.schedules.update(
                    self._editing_rule,
                    choices[index].id,
                    months=months,
                    weekdays=weekdays,
                    start=start,
                    end=end,
                    connector=self._selected_rule_connector(),
                )
            else:
                self._app.session.schedules.add(
                    choices[index].id,
                    months=months,
                    weekdays=weekdays,
                    start=start,
                    end=end,
                    connector=self._selected_rule_connector(),
                )
        except schedules.ScheduleError as error:
            self._app.window_report(str(error))
            return
        self._editing_rule = ""
        self._app.schedule_edited()
        self._clear_rule_editor()
        self._refresh_rules()

    def _edit_rule(self, rule: schedules.Rule) -> None:
        choices = self._session.playlists.all() if self._session is not None else ()
        for index, playlist in enumerate(choices):
            if playlist.id == rule.playlist:
                self._rule_playlist.set_selected(index)
                break
        target_index = (
            self._rule_connectors.index(rule.connector) + 1
            if rule.connector in self._rule_connectors
            else 0
        )
        self._rule_target.set_selected(target_index)
        for index, button in enumerate(self._months, 1):
            button.set_active(index in rule.months)
        for index, button in enumerate(self._weekdays):
            button.set_active(index in rule.weekdays)
        timed = rule.start is not None and rule.end is not None
        self._time_enabled.set_active(timed)
        if timed:
            assert rule.start is not None and rule.end is not None
            self._start_hour.set_selected(rule.start // 60)
            self._start_minute.set_selected(rule.start % 60)
            self._end_hour.set_selected(rule.end // 60)
            self._end_minute.set_selected(rule.end % 60)
        self._editing_rule = rule.id
        self._rule_commit.set_label("Save scheduled override")
        self._rule_cancel.set_visible(True)

    def _clear_rule_editor(self) -> None:
        self._editing_rule = ""
        for button in (*self._months, *self._weekdays):
            button.set_active(False)
        self._time_enabled.set_active(False)
        self._rule_target.set_selected(0)
        self._start_hour.set_selected(0)
        self._start_minute.set_selected(0)
        self._end_hour.set_selected(0)
        self._end_minute.set_selected(0)
        self._rule_commit.set_label("Add scheduled override")
        self._rule_cancel.set_visible(False)
