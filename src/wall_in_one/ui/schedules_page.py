"""Display assignments and ordered calendar overrides."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class _DisplayControls:
    row: Adw.ExpanderRow
    playlist: Adw.ComboRow
    play: Gtk.Button
    stop: Gtk.Button
    modes: Adw.ActionRow
    shuffle: Gtk.ToggleButton
    cycle: Gtk.ToggleButton
    mode_defaults: Gtk.Button


@dataclass(frozen=True)
class _RuleEditorState:
    editing_rule: str
    connector: str
    playlist_id: str
    months: tuple[bool, ...]
    weekdays: tuple[bool, ...]
    time_enabled: bool
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    focus: str | None


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
        self._rule_choices: tuple[Any, ...] = ()
        self._playback_row: Adw.ComboRow | None = None
        self._playback_choices: tuple[Any, ...] = ()
        self._playback_group: Adw.PreferencesGroup | None = None
        self._playback_widgets: list[Gtk.Widget] = []
        self._display_playback_rows: dict[str, _DisplayControls] = {}
        self._display_selected_echo: dict[str, int] = {}
        self._playback_truth: runtime_truth.RuntimeTruth | None = None
        self._palette_playback_row: Adw.ActionRow | None = None
        self._playback_placeholder: Adw.ActionRow | None = None
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._content.set_margin_top(18)
        self._content.set_margin_bottom(24)
        self._content.set_margin_start(24)
        self._content.set_margin_end(24)
        self.set_child(self._content)

    def refresh(self, session: Session) -> None:
        fingerprint = self._authoring_fingerprint(session)
        self._session = session
        if self._built and fingerprint == self._fingerprint:
            return
        editor_state = self._capture_rule_editor()
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
            self._restore_rule_editor(editor_state, session)
            self._built = True
            self._fingerprint = fingerprint
        finally:
            self._loading = False
        self.get_vadjustment().set_value(scroll)

    def runtime_status_changed(self, session: Session) -> None:
        """Update the live selector without rebuilding schedule authoring."""
        truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
        if session.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT:
            # Taboo inventory is intentionally not part of RuntimeTruth's
            # authoring/playback identity, but it changes whether a failed
            # route can be retried. Reconcile these stable widgets even when
            # the route fields themselves are unchanged.
            self._loading = True
            try:
                self._populate_independent_playback(session, truth)
            finally:
                self._loading = False
            return
        row = self._playback_row
        if not self._built or row is None:
            return
        self._loading = True
        try:
            row.set_sensitive(True)
            row.set_subtitle(self._playback_description(session, truth))
            row.set_selected(self._playback_selected(session, self._playback_choices, truth))
        finally:
            self._loading = False

    def _authoring_fingerprint(self, session: Session) -> object:
        """State whose widgets are rebuilt; live playback truth is separate."""
        return (
            session.settings.active_playlist,
            session.settings.display_mode,
            session.settings.theme_source_connector,
            session.playlists.all(),
            session.displays.all(),
            session.schedules.rules,
            self._live_connectors(),
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
        self._playback_group = group
        self._playback_widgets = []
        self._display_playback_rows = {}
        self._display_selected_echo.clear()
        self._palette_playback_row = None
        self._playback_placeholder = None
        truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
        if session.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT:
            self._populate_independent_playback(session, truth)
            return group
        choices = session.playlists.all()
        row = Adw.ComboRow(
            title="Active playlist",
            subtitle=self._playback_description(session, truth),
            model=Gtk.StringList.new(["Follow schedule", *(one.name for one in choices)]),
        )
        row.set_selected(self._playback_selected(session, choices, truth))
        self._playback_row = row
        self._playback_choices = choices
        row.connect("notify::selected", self._make_playback_changed(choices))
        group.add(row)
        self._playback_widgets.append(row)
        self._playback_truth = truth
        return group

    def _populate_independent_playback(
        self,
        session: Session,
        truth: runtime_truth.RuntimeTruth | None,
    ) -> None:
        """Reconcile only live playback rows; authoring widgets stay untouched."""
        group = self._playback_group
        if group is None:
            return
        self._playback_row = None
        self._playback_choices = ()
        self._playback_truth = truth

        if truth is None or truth.status_version != 2 or truth.display_mode != "independent":
            self._remove_independent_live_rows(group)
            if self._playback_placeholder is None:
                self._playback_placeholder = Adw.ActionRow()
                self._playback_placeholder.add_prefix(
                    Gtk.Image(icon_name="dialog-warning-symbolic")
                )
                group.add(self._playback_placeholder)
            self._playback_placeholder.set_title("Per-display playback unavailable")
            self._playback_placeholder.set_subtitle(
                "Waiting for a version-2 runtime snapshot. No authored Session state is "
                "shown as if it were live."
            )
            self._playback_widgets = [self._playback_placeholder]
            return

        if self._playback_placeholder is not None:
            group.remove(self._playback_placeholder)
            self._playback_placeholder = None
        palette = truth.theme_source
        if palette is not None:
            configured = palette.configured or "Automatic"
            if palette.effective is None:
                palette_subtitle = f"{configured} is designated; no live source is available"
            elif palette.fallback:
                palette_subtitle = (
                    f"{configured} is detached; colours temporarily follow {palette.effective}"
                )
            else:
                palette_subtitle = f"Noctalia colours currently follow {palette.effective}"
            if self._palette_playback_row is None:
                self._palette_playback_row = Adw.ActionRow(title="Shell-wide colours")
                self._palette_playback_row.add_prefix(
                    Gtk.Image(icon_name="applications-graphics-symbolic")
                )
                group.add(self._palette_playback_row)
            self._palette_playback_row.set_subtitle(palette_subtitle)

        choices = session.playlists.all()
        live = tuple(record for record in truth.displays if record.connected)
        if not live:
            for controls in self._display_playback_rows.values():
                group.remove(controls.row)
            self._display_playback_rows = {}
            if self._playback_placeholder is None:
                self._playback_placeholder = Adw.ActionRow()
                group.add(self._playback_placeholder)
            self._playback_placeholder.set_title("No connected displays")
            self._playback_placeholder.set_subtitle(
                "Saved detached assignments remain below and will return on reconnect."
            )
            self._playback_widgets = [
                widget
                for widget in (self._palette_playback_row, self._playback_placeholder)
                if widget is not None
            ]
            return

        wanted = {display.connector for display in live}
        for connector in tuple(self._display_playback_rows):
            if connector not in wanted:
                group.remove(self._display_playback_rows.pop(connector).row)
                self._display_selected_echo.pop(connector, None)
        for display in live:
            found_controls = self._display_playback_rows.get(display.connector)
            if found_controls is None:
                found_controls = self._new_display_controls(display.connector, choices)
                self._display_playback_rows[display.connector] = found_controls
                group.add(found_controls.row)
            self._update_display_controls(found_controls, display, choices, truth)
        self._playback_widgets = [
            widget
            for widget in (
                self._palette_playback_row,
                *(controls.row for controls in self._display_playback_rows.values()),
            )
            if widget is not None
        ]

    def _remove_independent_live_rows(self, group: Adw.PreferencesGroup) -> None:
        if self._palette_playback_row is not None:
            group.remove(self._palette_playback_row)
            self._palette_playback_row = None
        for controls in self._display_playback_rows.values():
            group.remove(controls.row)
        self._display_playback_rows = {}
        self._display_selected_echo.clear()

    def _new_display_controls(
        self,
        connector: str,
        choices: tuple[Any, ...],
    ) -> _DisplayControls:
        row = Adw.ExpanderRow(title=connector)
        playlist = Adw.ComboRow(
            title="Active playlist",
            model=Gtk.StringList.new(
                ["Follow this display's schedule", *(one.name for one in choices)]
            ),
        )
        playlist.connect(
            "notify::selected",
            self._make_display_playback_changed(connector, choices),
        )
        row.add_row(playlist)

        transport = Adw.ActionRow(
            title="Playback",
            subtitle="Pause freezes motion; Stop releases renderer resources and keeps the still",
        )
        for icon, tooltip, verb in (
            ("go-previous-symbolic", "Previous wallpaper", "previous"),
            ("applications-games-symbolic", "Random wallpaper", "random"),
            ("go-next-symbolic", "Next wallpaper", "next"),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
            button.add_css_class("flat")
            button.connect("clicked", self._make_display_action(connector, verb))
            transport.add_suffix(button)
        play = Gtk.Button(icon_name="media-playback-pause-symbolic")
        play.add_css_class("flat")
        play.connect("clicked", self._make_display_play_action(connector))
        transport.add_suffix(play)
        stop = Gtk.Button(
            icon_name="media-playback-stop-symbolic",
            tooltip_text="Stop motion and release its resources",
        )
        stop.add_css_class("flat")
        stop.connect("clicked", self._make_display_action(connector, "stop"))
        transport.add_suffix(stop)
        row.add_row(transport)

        modes = Adw.ActionRow(
            title="Rotation modes",
            subtitle="Cycle advances automatically; Shuffle changes that order",
        )
        shuffle = Gtk.ToggleButton(label="Shuffle")
        shuffle.connect("toggled", self._make_display_mode_changed(connector, "shuffle"))
        modes.add_suffix(shuffle)
        cycle = Gtk.ToggleButton(label="Cycle")
        cycle.connect("toggled", self._make_display_mode_changed(connector, "cycle"))
        modes.add_suffix(cycle)
        mode_defaults = Gtk.Button(
            label="Use saved defaults",
            tooltip_text="Clear this display's temporary Cycle and Shuffle choices",
        )
        mode_defaults.add_css_class("flat")
        mode_defaults.connect("clicked", self._make_display_mode_defaults(connector))
        modes.add_suffix(mode_defaults)
        row.add_row(modes)
        return _DisplayControls(
            row,
            playlist,
            play,
            stop,
            modes,
            shuffle,
            cycle,
            mode_defaults,
        )

    def _update_display_controls(
        self,
        controls: _DisplayControls,
        display: runtime_truth.DisplayRuntimeTruth,
        choices: tuple[Any, ...],
        truth: runtime_truth.RuntimeTruth,
    ) -> None:
        taboo = display.entry_taboo or runtime_truth.entry_is_taboo(
            getattr(self._app, "runtime_status", None),
            display.playlist_id,
            display.entry_id,
        )
        description = self._display_playback_description(display, truth, taboo=taboo)
        controls.row.set_subtitle(description)
        diagnostic = (
            display.automatic_retry.reason
            if display.automatic_retry is not None
            else display.last_error
        )
        controls.row.set_tooltip_text(diagnostic or None)
        # Keep the live routing decision visible while the expander is open.
        # The selector is where a person changes that decision, so leaving its
        # subtitle blank makes a manual override look like authored state.
        controls.playlist.set_subtitle(description)
        self._set_display_selected(
            display.connector,
            controls.playlist,
            self._display_playback_selected(display, choices),
        )
        # A renderer failure leaves the resolved still on screen and may keep
        # the route's logical playback state as ``playing``. A transient
        # failure can be retried with Play. A session-taboo entry cannot: the
        # Media/Pairings health surface explains how to remove it safely.
        playing = display.playback_state == "playing" and not display.renderer_failed
        controls.play.set_icon_name(
            "dialog-warning-symbolic"
            if taboo
            else "media-playback-pause-symbolic"
            if playing
            else "media-playback-start-symbolic"
        )
        if taboo:
            controls.play.set_tooltip_text(
                "Borked wallpaper: playback is disabled; open it in Media/Pairings"
            )
        elif display.renderer_failed:
            controls.play.set_tooltip_text("Retry motion on this display")
        else:
            controls.play.set_tooltip_text(
                "Pause this display" if playing else "Resume this display"
            )
        controls.play.set_sensitive(not taboo)
        controls.stop.set_sensitive(display.playback_state != "stopped")
        controls.modes.set_subtitle(self._display_modes_description(display))
        controls.shuffle.set_active(display.shuffle)
        controls.shuffle.set_tooltip_text(
            f"Shuffle is {'on' if display.shuffle else 'off'} ({display.shuffle_source})"
        )
        controls.cycle.set_active(display.cycle_enabled)
        controls.cycle.set_tooltip_text(
            f"Cycle is {'on' if display.cycle_enabled else 'off'} ({display.cycle_source})"
        )
        controls.mode_defaults.set_visible(
            display.shuffle_source == "manual" or display.cycle_source == "manual"
        )

    @staticmethod
    def _display_playback_selected(
        display: runtime_truth.DisplayRuntimeTruth,
        choices: tuple[Any, ...],
    ) -> int:
        if not display.manual_override:
            return 0
        for index, playlist in enumerate(choices, start=1):
            if playlist.id == display.playlist_id:
                return index
        return 0

    @staticmethod
    def _display_playback_description(
        display: runtime_truth.DisplayRuntimeTruth,
        truth: runtime_truth.RuntimeTruth,
        *,
        taboo: bool,
    ) -> str:
        if display.automatic_retry is not None:
            retry = display.automatic_retry
            return (
                f"Retry {retry.attempt}/{retry.maximum_attempts} · {display.playlist} · "
                f"{SchedulesPage._bounded_diagnostic(retry.reason)}"
            )
        if taboo:
            detail = SchedulesPage._bounded_diagnostic(
                display.last_error or "this wallpaper is marked as a known renderer crasher"
            )
            return (
                f"Borked · playback disabled; paired still retained · {display.playlist} · {detail}"
            )
        if display.renderer_failed:
            detail = SchedulesPage._bounded_diagnostic(
                display.last_error or "the motion renderer exited"
            )
            return f"Renderer stopped · Retry available · {display.playlist} · {detail}"
        if display.route_source == "manual":
            route = "Manual override"
        elif display.route_source == "schedule":
            route = (
                f"Schedule rule {display.schedule_rule_id}"
                if display.schedule_rule_id
                else "Scheduled override"
            )
        elif display.route_source == "assignment":
            route = "Assigned baseline"
        else:
            route = "Default rotation"
        playback = (
            "Playing motion"
            if display.playback_state == "playing" and display.motion_active
            else "Playing still"
            if display.playback_state == "playing"
            else display.playback_state.capitalize()
        )
        parts = [route, display.playlist, playback]
        if truth.theme_source is not None and truth.theme_source.effective == display.connector:
            parts.append("Colours source")
        return " · ".join(parts)

    @staticmethod
    def _bounded_diagnostic(value: str, maximum: int = 220) -> str:
        """Keep subprocess stderr from turning one schedule row into a page."""
        clean = " ".join(value.split())
        if len(clean) <= maximum:
            return clean
        marker = " … "
        prefix = (maximum - len(marker)) * 3 // 5
        suffix = maximum - len(marker) - prefix
        return clean[:prefix] + marker + clean[-suffix:]

    @staticmethod
    def _display_modes_description(display: runtime_truth.DisplayRuntimeTruth) -> str:
        def describe(label: str, active: bool, default: bool, source: str) -> str:
            state = "on" if active else "off"
            if source == "manual":
                saved = "on" if default else "off"
                return f"{label} {state} — manual (saved default {saved})"
            return f"{label} {state} — saved default"

        return " · ".join(
            (
                describe(
                    "Cycle",
                    display.cycle_enabled,
                    display.cycle_default,
                    display.cycle_source,
                ),
                describe(
                    "Shuffle", display.shuffle, display.shuffle_default, display.shuffle_source
                ),
            )
        )

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
        connected = set(self._live_connectors())
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
        self._fingerprint = self._authoring_fingerprint(session)

    def _build_new_rule(self, session: Session) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Add an override",
            description=(
                "Leave months and weekdays empty for any. A time window needs both ends and "
                "may cross midnight."
            ),
        )
        choices = session.playlists.all()
        self._rule_choices = choices
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

    def _capture_rule_editor(self) -> _RuleEditorState | None:
        if not self._built:
            return None
        target_index = self._rule_target.get_selected()
        connector = (
            self._rule_connectors[target_index - 1]
            if 0 < target_index <= len(self._rule_connectors)
            else ""
        )
        playlist_index = self._rule_playlist.get_selected()
        playlist_id = (
            self._rule_choices[playlist_index].id
            if playlist_index < len(self._rule_choices)
            else ""
        )
        focus = self._rule_editor_focus()
        return _RuleEditorState(
            editing_rule=self._editing_rule,
            connector=connector,
            playlist_id=playlist_id,
            months=tuple(button.get_active() for button in self._months),
            weekdays=tuple(button.get_active() for button in self._weekdays),
            time_enabled=self._time_enabled.get_active(),
            start_hour=self._start_hour.get_selected(),
            start_minute=self._start_minute.get_selected(),
            end_hour=self._end_hour.get_selected(),
            end_minute=self._end_minute.get_selected(),
            focus=focus,
        )

    def _rule_editor_focus(self) -> str | None:
        root = self.get_root()
        focused = root.get_focus() if isinstance(root, Gtk.Window) else None
        if focused is None:
            return None
        for name in (
            "_rule_target",
            "_rule_playlist",
            "_time_enabled",
            "_start_hour",
            "_start_minute",
            "_end_hour",
            "_end_minute",
            "_rule_commit",
            "_rule_cancel",
        ):
            widget = getattr(self, name, None)
            if isinstance(widget, Gtk.Widget) and (
                focused is widget or focused.is_ancestor(widget)
            ):
                return name
        return None

    def _restore_rule_editor(
        self,
        state: _RuleEditorState | None,
        session: Session,
    ) -> None:
        if state is None:
            return
        target = (
            self._rule_connectors.index(state.connector) + 1
            if state.connector in self._rule_connectors
            else 0
        )
        self._rule_target.set_selected(target)
        playlist = next(
            (
                index
                for index, choice in enumerate(self._rule_choices)
                if choice.id == state.playlist_id
            ),
            0,
        )
        self._rule_playlist.set_selected(playlist)
        for button, active in zip(self._months, state.months, strict=False):
            button.set_active(active)
        for button, active in zip(self._weekdays, state.weekdays, strict=False):
            button.set_active(active)
        self._time_enabled.set_active(state.time_enabled)
        self._time_box.set_sensitive(state.time_enabled)
        self._start_hour.set_selected(state.start_hour)
        self._start_minute.set_selected(state.start_minute)
        self._end_hour.set_selected(state.end_hour)
        self._end_minute.set_selected(state.end_minute)
        self._editing_rule = (
            state.editing_rule
            if any(rule.id == state.editing_rule for rule in session.schedules.rules)
            else ""
        )
        self._rule_commit.set_label(
            "Save scheduled override" if self._editing_rule else "Add scheduled override"
        )
        self._rule_cancel.set_visible(bool(self._editing_rule))
        if state.focus is not None:
            widget = getattr(self, state.focus, None)
            if isinstance(widget, Gtk.Widget):
                widget.grab_focus()

    def _schedule_connectors(self, session: Session) -> tuple[str, ...]:
        connected = set(self._live_connectors())
        saved = {connector for connector, _playlist in session.displays.all()}
        targeted = {rule.connector for rule in session.schedules.rules if rule.connector}
        if session.settings.theme_source_connector:
            targeted.add(session.settings.theme_source_connector)
        return tuple(sorted(connected | saved | targeted))

    def _live_connectors(self) -> tuple[str, ...]:
        """Merge GTK monitor names with Rust's authoritative niri snapshot."""
        found = list(_connected_outputs())
        truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
        if truth is not None and truth.status_version == 2:
            for display in truth.displays:
                if display.connected and display.connector not in found:
                    found.append(display.connector)
        return tuple(found)

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
            index = row.get_selected()
            (
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
            # The next accepted status reconciles this row.  Do not invalidate
            # the authored fingerprint: doing so would rebuild unrelated
            # schedule controls on the next refresh and discard editor state.

        return changed

    def _make_display_playback_changed(
        self,
        connector: str,
        choices: tuple[Any, ...],
    ) -> Any:
        def changed(row: Adw.ComboRow, _property: object) -> None:
            index = row.get_selected()
            if self._display_selected_echo.get(connector) == index:
                self._display_selected_echo.pop(connector, None)
                return
            if self._loading:
                return
            started = (
                self._app.activate_playlist_on_async(connector, choices[index - 1].id)
                if 0 < index <= len(choices)
                else self._app.resume_schedule_on_async(connector)
            )
            # Runtime truth owns this selector. Revert the optimistic GTK
            # change immediately; the next atomic snapshot advances it only
            # after Rust has accepted and applied the route transition.
            truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
            display = truth.display(connector) if truth is not None else None
            self._loading = True
            try:
                self._set_display_selected(
                    connector,
                    row,
                    self._display_playback_selected(display, choices) if display is not None else 0,
                )
            finally:
                self._loading = False
            if started:
                self._playback_truth = None

        return changed

    def _set_display_selected(
        self,
        connector: str,
        row: Adw.ComboRow,
        selected: int,
    ) -> None:
        """Set service-owned selection without echoing it as a user command."""
        if row.get_selected() == selected:
            return
        self._display_selected_echo[connector] = selected
        row.set_selected(selected)

    def _make_display_action(self, connector: str, verb: str) -> Any:
        def activate(_button: Gtk.Button) -> None:
            self._app.runtime_action_on_async(connector, verb)

        return activate

    def _make_display_play_action(self, connector: str) -> Any:
        def activate(_button: Gtk.Button) -> None:
            truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
            display = truth.display(connector) if truth is not None else None
            if display is None:
                return
            if display.entry_taboo or runtime_truth.entry_is_taboo(
                getattr(self._app, "runtime_status", None),
                display.playlist_id,
                display.entry_id,
            ):
                self._app.present_page("media")
                return
            verb = (
                "pause"
                if display.playback_state == "playing" and not display.renderer_failed
                else "play"
            )
            self._app.runtime_action_on_async(connector, verb)

        return activate

    def _make_display_mode_changed(self, connector: str, verb: str) -> Any:
        def changed(button: Gtk.ToggleButton) -> None:
            if self._loading:
                return
            wanted = button.get_active()
            self._app.runtime_action_on_async(connector, verb, "on" if wanted else "off")
            # A mode button is still runtime truth, not an optimistic local
            # setting. Put it back until the next atomic status confirms the
            # service accepted the request.
            truth = runtime_truth.from_status(getattr(self._app, "runtime_status", None))
            display = truth.display(connector) if truth is not None else None
            current = (
                display.shuffle
                if display is not None and verb == "shuffle"
                else display.cycle_enabled
                if display is not None
                else False
            )
            self._loading = True
            try:
                button.set_active(current)
            finally:
                self._loading = False

        return changed

    def _make_display_mode_defaults(self, connector: str) -> Any:
        def reset(_button: Gtk.Button) -> None:
            # Application serialises both requests under one busy state. Keep
            # these widgets on the last atomic snapshot until Rust reports the
            # result, including a possible partial refusal.
            self._app.reset_display_modes_on_async(connector)

        return reset

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
            self._fingerprint = self._authoring_fingerprint(self._app.session)

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
            self._fingerprint = self._authoring_fingerprint(self._app.session)

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
            self._fingerprint = self._authoring_fingerprint(self._app.session)

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
