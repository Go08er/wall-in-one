"""Displays: what each monitor shows, and what it plays when nothing is scheduled.

The arrangement at the top is the compositor's layout (GTK's monitors), to
scale; the details below describe the selected monitor. "Same on all
displays" (mirrored, the default) runs one rotation everywhere and keeps each
display's own choices, and the rules aimed at one display, saved but
inactive until "Each display separately" is chosen again; switching to it
needs a connected display for the desktop colors and is refused otherwise.

The order the runtime follows: your pick, then a matching schedule rule
(shared, or aimed at this display), then the display's own playlist, then
the default. A display can let its own playlist beat the *shared* rules
(rules aimed at it still win); that choice is kept beside the runtime
configuration (``runtime-overrides.toml``), never in it.

Displays that are not connected are kept with everything saved for them
until they are forgotten. What is on screen, its reason and its Cycle and
Shuffle are the runtime's answer; Cycle, Shuffle and "Play now" are runtime
commands, never saved.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from wall_in_one.ui.next import widgets
from wall_in_one.ui.next.displays_arrangement import CSS as ARRANGEMENT_CSS
from wall_in_one.ui.next.displays_arrangement import Arrangement, LinkGlyph, MonitorTile, TileInfo
from wall_in_one.ui.next.page import Page, not_ported
from wall_in_one.ui.next.schedule_editor import playlist_name
from wall_in_one.ui.next.schedule_state import DisplayInfo, RouteView, Scheduling
from wall_in_one.ui.next.state import AppState, Reason, WallpaperView, reason_text
from wall_in_one.ui.pending_edits import queue_intent, settle_intent
from wall_in_one.ui.plain_text import plain

MODES: Final = {
    "mirrored": "One rotation, shown on every display",
    "independent": "Each display plays its own playlist",
}
_MODE: Final = "mode"
_COLORS: Final = "colors"

CSS: Final = (
    ARRANGEMENT_CSS
    + """
.displays-page .mode-caption { margin-top: 2px; }
.display-title { font-size: 1.35em; font-weight: 800; }
.displays-page .now-name { font-weight: 700; }
.now-thumb { padding: 0; border-radius: 8px; }
.remembered-icon {
  color: alpha(currentColor, 0.7);
  background-color: alpha(currentColor, 0.08);
  border-radius: 10px;
  min-width: 40px; min-height: 40px;
}
.display-controls { padding: 6px 10px 6px 14px; min-height: 36px; }
.layout-caption { font-size: 0.85em; }
"""
)
_styled = False


def _style() -> None:
    global _styled
    if not _styled:
        widgets.add_css(CSS)
        _styled = True


def _meta(display: DisplayInfo) -> str:
    """``2560 x 1440 · 144 Hz · 125%``, from what GTK knows."""
    if not display.placed:
        return "Layout not reported by the compositor"
    bits = [f"{display.width} \u00d7 {display.height}"]
    if display.refresh:
        bits.append(f"{round(display.refresh / 1000)} Hz")
    bits.append(f"{round(display.scale * 100)}%")
    return " · ".join(bits)


class DisplaysPage(Page):
    name = "displays"
    title = "Displays"

    def __init__(self, state: AppState, scheduling: Scheduling) -> None:
        super().__init__(state)
        _style()
        self.scheduling = scheduling
        self._selected = ""
        self._loading = False
        self._compact = False
        self._plays_ids: list[str] = []
        self._play_ids: list[str] = []
        self._menu: Gtk.PopoverMenu | None = None
        self._dialog: Adw.AlertDialog | None = None
        #: Writes still queued, per control (`pending_edits`).
        self._pending_assign: dict[str, tuple[str, int]] = {}
        self._pending_beats: dict[str, tuple[bool, int]] = {}
        self._pending_mode: dict[str, tuple[str, int]] = {}
        self._pending_colors: dict[str, tuple[str, int]] = {}
        #: Detached display rows, by connector.
        self.remembered_rows: dict[str, Adw.ActionRow] = {}

        # -- mode -----------------------------------------------------------------
        self.mode_group = Adw.ToggleGroup(homogeneous=True, halign=Gtk.Align.CENTER)
        for key, label, tooltip in (
            ("mirrored", "Same on all displays", "One rotation, in sync everywhere"),
            ("independent", "Each display separately", "Give each display its own playlist"),
        ):
            toggle = Adw.Toggle(name=key, label=label)
            toggle.set_tooltip(tooltip)
            self.mode_group.add(toggle)
        self.mode_group.connect("notify::active-name", self._on_mode)
        self._mode_caption = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True)
        self._mode_caption.add_css_class("dimmed")
        self._mode_caption.add_css_class("mode-caption")

        # -- arrangement -----------------------------------------------------------
        self.arrangement = Arrangement(self._select, self._popup_menu)
        self._layout_caption = Gtk.Label(xalign=1)
        self._layout_caption.add_css_class("dimmed")
        self._layout_caption.add_css_class("layout-caption")
        self._none = Adw.StatusPage(
            icon_name="video-display-symbolic",
            title="No displays are connected",
            description="What is saved for each display is kept below.",
        )
        self._none.add_css_class("compact")

        # -- details ----------------------------------------------------------------
        self._details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=10)
        self._details.append(self._build_heading())
        self._details.append(self._build_now())
        self._details.append(self._build_settings())
        self._remembered_section = self._build_remembered_section()

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=14,
            margin_bottom=28,
            margin_start=18,
            margin_end=18,
        )
        content.append(self.mode_group)
        content.append(self._mode_caption)
        arrangement_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8)
        arrangement_box.append(self.arrangement)
        arrangement_box.append(self._layout_caption)
        content.append(arrangement_box)
        content.append(self._none)
        content.append(self._details)
        content.append(self._remembered_section)
        clamp = Adw.Clamp(maximum_size=780, tightening_threshold=640, child=content)
        self._scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._scroller.set_child(clamp)
        breakpoint_bin = Adw.BreakpointBin(width_request=360, height_request=300)
        breakpoint_bin.add_css_class("displays-page")
        breakpoint_bin.set_child(self._scroller)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.connect("apply", lambda *_args: self._set_compact(True))
        narrow.connect("unapply", lambda *_args: self._set_compact(False))
        breakpoint_bin.add_breakpoint(narrow)
        self.widget = breakpoint_bin
        self._install_actions()

        self._refresh_pending = False
        state.connect("changed", self._on_changed)
        self.refresh()

    # -- Page API ---------------------------------------------------------------------
    def activate(self, argument: str | None) -> None:
        if argument and argument in self._connected():
            self._selected = argument
        self.refresh()

    def demo(self, scene: str) -> None:
        """Screenshot and smoke scenes; every one goes through `Scheduling` only."""
        if self._menu is not None:
            self._menu.popdown()
        if self._dialog is not None:
            self._dialog.force_close()
        what, _, argument = scene.partition(":")
        if what == "select":
            self._select(argument)
        elif what == "menu":
            tile = self.arrangement.tiles.get(argument or self._selected)
            if tile is not None:
                self._popup_menu(tile, tile.get_width() * 0.4, tile.get_height() * 0.45)
        elif what == "forget":
            remembered = [d.connector for d in self.scheduling.known_displays() if not d.connected]
            if remembered:
                self.confirm_forget(argument or remembered[0])

    # -- reading ------------------------------------------------------------------------
    def _connected(self) -> list[str]:
        return [d.connector for d in self.scheduling.known_displays() if d.connected]

    def _mirrored(self) -> bool:
        return self.scheduling.saved_display_mode() != "independent"

    def _route(self, connector: str) -> RouteView | None:
        routes = list(self.scheduling.routes())
        found = next((route for route in routes if route.connector == connector), None)
        if found is None and len(routes) == 1 and not routes[0].connector:
            return routes[0]  # one answer for every display (a version-1 runtime)
        return found

    def _name(self, pid: str) -> str:
        return playlist_name(self.state, pid)

    def _shown(self, connector: str) -> WallpaperView | None:
        wid = self.state.current.get(connector) or self.state.current.get("Display", "")
        return self.state.wallpaper(wid) if wid and self.state.has_wallpaper(wid) else None

    def _status(self, connector: str) -> tuple[str, str, bool]:
        service = self.state.player().service
        if service == "checking":
            return "Checking", "content-loading-symbolic", False
        if service == "stopped":
            return "Not running", "media-playback-stop-symbolic", True
        route = self._route(connector)
        playback = route.playback if route is not None else "playing"
        if playback == "stopped":
            return "Still only", "media-playback-stop-symbolic", False
        if playback == "paused":
            return "Paused", "media-playback-pause-symbolic", True
        return "Playing", "media-playback-start-symbolic", False

    def why(self, connector: str) -> str:
        """Why ``connector`` shows what it shows, in the runtime's words."""
        route = self._route(connector)
        if route is None:
            return ""
        playlist = "" if route.quick else route.playlist
        return reason_text(Reason(route.route, playlist, route.until))

    # -- building ------------------------------------------------------------------------
    def _section(self, title: str, child: Gtk.Widget, extra: Gtk.Widget | None = None) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(spacing=6)
        label = Gtk.Label(label=title.upper(), xalign=0, hexpand=True, valign=Gtk.Align.CENTER)
        label.add_css_class("section-label")
        row.append(label)
        if extra is not None:
            row.append(extra)
        box.append(row)
        box.append(child)
        return box

    def _build_heading(self) -> Gtk.Widget:
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.heading = Gtk.Label(xalign=0, wrap=True)
        self.heading.add_css_class("display-title")
        self._meta = widgets.dim("")
        self._meta.add_css_class("numeric")
        head.append(self.heading)
        head.append(self._meta)
        return head

    def _build_now(self) -> Gtk.Widget:
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        row = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=10, margin_end=12)
        self._thumb = widgets.Thumb(None, 128, 72, 8)
        self._thumb_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self._thumb_button.add_css_class("flat")
        self._thumb_button.add_css_class("now-thumb")
        self._thumb_button.set_tooltip_text("Show in Library")
        self._thumb_button.connect("clicked", lambda *_args: self._show_in_library(self._selected))
        row.append(self._thumb_button)
        texts = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER, hexpand=True
        )
        self.now_name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.now_name.add_css_class("now-name")
        self.now_why = widgets.dim("")
        self._now_status = Gtk.Label(xalign=0)
        self._now_status.add_css_class("caption")
        self._now_status.add_css_class("dimmed")
        for widget in (self.now_name, self.now_why, self._now_status):
            texts.append(widget)
        row.append(texts)
        self.resume_button = Gtk.Button(label="Resume schedule", valign=Gtk.Align.CENTER)
        self.resume_button.add_css_class("pill")
        self.resume_button.set_tooltip_text("Go back to what the schedule says")
        self.resume_button.connect("clicked", lambda *_args: self._resume(self._selected))
        row.append(self.resume_button)
        group.append(Gtk.ListBoxRow(activatable=False, child=row))

        # Play a playlist now: a runtime choice, held until the schedule is resumed.
        self.play_row = plain(Adw.ComboRow, title="Playing now")
        self.play_row.connect("notify::selected", self._on_play)
        group.append(self.play_row)

        # Cycle and Shuffle on this display: runtime choices over its saved defaults.
        self.rotation_row = plain(Adw.ActionRow, title="Rotation")
        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.cycle_toggle = Gtk.ToggleButton(label="Cycle")
        self.cycle_toggle.connect("toggled", self._on_rotation, "cycle")
        self.shuffle_toggle = Gtk.ToggleButton(label="Shuffle")
        self.shuffle_toggle.connect("toggled", self._on_rotation, "shuffle")
        self.defaults_button = Gtk.Button(label="Use saved defaults")
        self.defaults_button.add_css_class("flat")
        self.defaults_button.set_tooltip_text(
            "Clear this display\u2019s temporary Cycle and Shuffle choices"
        )
        self.defaults_button.connect("clicked", lambda *_args: self._use_defaults())
        for button in (self.cycle_toggle, self.shuffle_toggle, self.defaults_button):
            buttons.append(button)
        self.rotation_row.add_suffix(buttons)
        group.append(self.rotation_row)

        self._now_scope = Gtk.Label()
        self._now_scope.add_css_class("dimmed")
        self._now_scope.add_css_class("caption")
        return self._section("Now showing", group, self._now_scope)

    def _build_settings(self) -> Gtk.Widget:
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        self.plays_row = plain(Adw.ComboRow, title="When nothing is scheduled")
        self.plays_row.set_subtitle_lines(3)
        self.plays_row.connect("notify::selected", self._on_plays)
        group.append(self.plays_row)

        self.beats_row = plain(
            Adw.SwitchRow,
            title="Its playlist beats shared rules",
            subtitle="Rules aimed at this display still win",
        )
        self.beats_row.connect("notify::active", self._on_beats)
        group.append(self.beats_row)

        self._schedule_link = plain(
            Adw.ActionRow, title="Schedule for this display…", activatable=True
        )
        self._schedule_link.add_prefix(Gtk.Image.new_from_icon_name("x-office-calendar-symbolic"))
        self._schedule_link.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self._schedule_link.connect(
            "activated", lambda *_args: self.state.navigate(f"schedule:new:{self._selected}")
        )
        group.append(self._schedule_link)

        self.linked_note = plain(
            Adw.ActionRow, title="Same rotation on every display", subtitle_lines=3
        )
        glyph = LinkGlyph(16)
        glyph.add_css_class("dimmed")
        self.linked_note.add_prefix(glyph)
        group.append(self.linked_note)

        self.colors_row = plain(Adw.ActionRow, title="Desktop colors", subtitle_lines=2)
        self.colors_row.add_prefix(Gtk.Image.new_from_icon_name("preferences-color-symbolic"))
        self.colors_button = Gtk.Button(label="Use this display", valign=Gtk.Align.CENTER)
        self.colors_button.set_tooltip_text(
            "Noctalia has one palette: take it from this display\u2019s wallpaper"
        )
        self.colors_button.connect("clicked", lambda *_args: self.use_for_colors(self._selected))
        self.colors_row.add_suffix(self.colors_button)
        self._colors_check = Gtk.Image.new_from_icon_name("object-select-symbolic")
        self._colors_check.add_css_class("accent")
        self._colors_check.set_tooltip_text("Desktop colors follow this display")
        self.colors_row.add_suffix(self._colors_check)
        group.append(self.colors_row)
        return self._section("This display", group)

    def _build_remembered_section(self) -> Gtk.Widget:
        self._remembered_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._remembered_list.add_css_class("boxed-list")
        return self._section("Not connected", self._remembered_list)

    # -- refresh ---------------------------------------------------------------------------
    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic not in (
            "now",
            "routes",
            "playback",
            "displays",
            "playlists",
            "schedule",
            "settings",
            "system",
            "library",
        ):
            return
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.idle_add(self._idle_refresh, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _idle_refresh(self) -> bool:
        self._refresh_pending = False
        self.refresh()
        return False

    def flush(self) -> None:
        """Run a refresh that is waiting (tests, and before reading what is shown)."""
        if self._refresh_pending:
            self._idle_refresh()

    def refresh(self) -> None:
        scheduling = self.scheduling
        known = list(scheduling.known_displays())
        connected = [d for d in known if d.connected]
        mirrored = self._mirrored()
        names = [d.connector for d in connected]
        if self._selected not in names:
            self._selected = names[0] if names else ""
        self._loading = True
        try:
            pending_mode = self._pending_mode.get(_MODE)
            mode = pending_mode[0] if pending_mode is not None else scheduling.saved_display_mode()
            self.mode_group.set_active_name(mode)
            self._mode_caption.set_label(MODES.get(mode, ""))
            self.mode_group.set_sensitive(not scheduling.settings_blocked())
            self.mode_group.set_tooltip_text(scheduling.settings_blocked() or None)
            count = len(connected)
            self.set_title("Displays", f"{count} connected" if count != 1 else "1 connected")

            self.arrangement.set_displays(connected)
            self.arrangement.set_visible(bool(connected))
            self.arrangement.set_linked(mirrored and len(connected) > 1)
            self._layout_caption.set_visible(bool(connected) and not self._compact)
            self._layout_caption.set_label(
                "Positions come from your compositor"
                if all(d.placed for d in connected)
                else "Some positions aren\u2019t reported by your compositor"
            )
            self._none.set_visible(not connected)
            self._details.set_visible(bool(connected))
            colors = scheduling.color_source()
            for display in connected:
                tile = self.arrangement.tiles.get(display.connector)
                if tile is None:
                    continue
                status, icon, paused = self._status(display.connector)
                shown = self._shown(display.connector)
                route = self._route(display.connector)
                source = (
                    "your pick"
                    if route is not None and route.route == "pick"
                    else route.playlist
                    if route is not None
                    else ""
                )
                tile.update(
                    TileInfo(
                        picture=shown,
                        wallpaper=shown.name if shown is not None else "",
                        source=source,
                        status=status,
                        status_icon=icon,
                        paused=paused,
                        colors=not mirrored and display.connector == colors.effective,
                    )
                )
            if self._selected:
                self.arrangement.select(self._selected)
                display = next(d for d in connected if d.connector == self._selected)
                self._refresh_details(display, mirrored)
            self._build_remembered([d for d in known if not d.connected])
        finally:
            self._loading = False

    def _refresh_details(self, display: DisplayInfo, mirrored: bool) -> None:
        scheduling = self.scheduling
        connector = display.connector
        self.heading.set_label(f"{connector} · {display.model}" if display.model else connector)
        self._meta.set_label(_meta(display))

        # Now showing
        shown = self._shown(connector)
        if shown is not None:
            self._thumb.show(shown, 320, 180)
            if self._thumb_button.get_child() is not self._thumb:
                self._thumb_button.set_child(self._thumb)
            self._thumb_button.set_sensitive(True)
        else:
            self._thumb_button.set_child(Gtk.Image.new_from_icon_name("image-missing-symbolic"))
            self._thumb_button.set_sensitive(False)
        self.now_name.set_label(shown.name if shown is not None else "Nothing the library knows")
        self.now_why.set_label(self.why(connector))
        status, _icon, _paused = self._status(connector)
        self._now_status.set_label(status)
        self._now_scope.set_label("All displays" if mirrored else f"{connector} only")
        route = self._route(connector)
        runtime_off = scheduling.runtime_blocked()
        self.resume_button.set_visible(route is not None and route.route == "pick")
        self.resume_button.set_sensitive(not runtime_off)

        mine = [p for p in self.state.playlists if not p.automatic]
        play_ids = ["", *(p.id for p in mine)]
        if play_ids != self._play_ids:
            self._play_ids = play_ids
            self.play_row.set_model(
                Gtk.StringList.new(["Follow the schedule", *(p.name for p in mine)])
            )
        picked = route.playlist_id if route is not None and route.route == "pick" else ""
        self.play_row.set_selected(play_ids.index(picked) if picked in play_ids else 0)
        self.play_row.set_sensitive(not runtime_off)
        self.play_row.set_subtitle(runtime_off or ("Your pick" if route and route.quick else ""))

        self.rotation_row.set_visible(not mirrored)
        if route is not None:
            self.cycle_toggle.set_active(route.cycle)
            self.shuffle_toggle.set_active(route.shuffle)
            self.defaults_button.set_visible(route.cycle_manual or route.shuffle_manual)
            self.rotation_row.set_subtitle(self._rotation_text(route))
        else:
            self.defaults_button.set_visible(False)
            self.rotation_row.set_subtitle("")
        for widget in (self.cycle_toggle, self.shuffle_toggle, self.defaults_button):
            widget.set_sensitive(not runtime_off and route is not None)

        # This display
        self.plays_row.set_visible(not mirrored)
        self.beats_row.set_visible(not mirrored)
        self._schedule_link.set_visible(not mirrored)
        self.colors_row.set_visible(not mirrored)
        self.linked_note.set_visible(mirrored)
        blocked = scheduling.schedule_blocked()
        self._refresh_plays(connector, blocked, route)
        self._refresh_beats(connector, blocked)
        self._refresh_colors(connector)
        self._refresh_linked_note()

    @staticmethod
    def _rotation_text(route: RouteView) -> str:
        def describe(label: str, on: bool, default: bool, manual: bool) -> str:
            state = "on" if on else "off"
            if manual:
                return f"{label} {state} for now (saved: {'on' if default else 'off'})"
            return f"{label} {state}"

        return " · ".join(
            (
                describe("Cycle", route.cycle, route.cycle_default, route.cycle_manual),
                describe("Shuffle", route.shuffle, route.shuffle_default, route.shuffle_manual),
            )
        )

    def _refresh_plays(self, connector: str, blocked: str, route: RouteView | None) -> None:
        scheduling = self.scheduling
        default = scheduling.default_playlist
        playlists = [p for p in self.state.playlists if p.id != default]
        ids = ["", *(p.id for p in playlists)]
        pending = self._pending_assign.get(connector)
        assigned = scheduling.display_assignments().get(connector, "")
        wanted = pending[0] if pending is not None else assigned
        labels = [
            "Default" if self._compact else f"Default ({self._name(default)})",
            *(p.name for p in playlists),
        ]
        if wanted and wanted not in ids:
            ids.append(wanted)
            labels.append(self._name(wanted))
        if ids != self._plays_ids or self.plays_row.get_model() is None:
            self._plays_ids = ids
            self.plays_row.set_model(Gtk.StringList.new(labels))
        self.plays_row.set_selected(ids.index(wanted))
        self.plays_row.set_sensitive(not blocked)
        if blocked:
            self.plays_row.set_subtitle(blocked)
        elif route is None:
            self.plays_row.set_subtitle("")
        elif route.route == "display":
            self.plays_row.set_subtitle(
                "In use now"
                + (f" · nothing is scheduled until {route.until}" if route.until else "")
            )
        elif route.route == "schedule":
            text = f"Not in use now · “{route.playlist}” is scheduled"
            self.plays_row.set_subtitle(text + (f" until {route.until}" if route.until else ""))
        elif route.route == "pick":
            self.plays_row.set_subtitle("Not in use now · your pick is showing")
        else:
            self.plays_row.set_subtitle("In use now" if not assigned else "")

    def _refresh_beats(self, connector: str, blocked: str) -> None:
        scheduling = self.scheduling
        pending = self._pending_beats.get(connector)
        assigned = bool(scheduling.display_assignments().get(connector))
        active = pending[0] if pending is not None else scheduling.beats_global_rules(connector)
        self.beats_row.set_active(active)
        self.beats_row.set_sensitive(not blocked and (assigned or active))
        if blocked:
            subtitle = blocked
        elif not assigned:
            subtitle = "Choose its own playlist above first"
        elif not scheduling.overrides_apply():
            subtitle = (
                "Rules aimed at this display still win. Applies once the updated wallpaper "
                "service runs"
            )
        else:
            subtitle = "Rules aimed at this display still win"
        self.beats_row.set_subtitle(subtitle)

    def _refresh_colors(self, connector: str) -> None:
        scheduling = self.scheduling
        colors = scheduling.color_source()
        pending = self._pending_colors.get(_COLORS)
        chosen = pending[0] if pending is not None else colors.configured
        follows = colors.effective if pending is None else chosen
        is_source = connector == follows
        if colors.fallback and pending is None:
            stand_in = colors.effective or "no display"
            subtitle = f"{colors.configured} isn\u2019t connected, so colors follow {stand_in}"
        elif is_source:
            subtitle = "Follow this display\u2019s wallpaper"
        else:
            subtitle = f"Follow {follows}" if follows else "No display is chosen"
        blocked = scheduling.settings_blocked()
        self.colors_row.set_subtitle(blocked or subtitle)
        self.colors_button.set_visible(not is_source)
        self.colors_button.set_sensitive(not blocked)
        self._colors_check.set_visible(is_source)

    def _refresh_linked_note(self) -> None:
        scheduling = self.scheduling
        kept = [
            f"{c} keeps “{self._name(p)}”"
            for c, p in sorted(scheduling.display_assignments().items())
        ]
        aimed = sum(bool(rule.connector) for rule in scheduling.schedule_rules)
        if aimed:
            kept.append(f"{aimed} rule{'s' if aimed != 1 else ''} for one display")
        self.linked_note.set_subtitle(
            f"Saved and inactive: {', '.join(kept)}. Choose “Each display separately” to use them."
            if kept
            else "Choose “Each display separately” to set this one apart."
        )

    def _build_remembered(self, detached: list[DisplayInfo]) -> None:
        self._remembered_list.remove_all()
        self.remembered_rows = {}
        scheduling = self.scheduling
        for display in detached:
            plan = scheduling.forget_plan(display.connector)
            bits = []
            if plan.playlist:
                bits.append(f"plays “{self._name(plan.playlist)}” when nothing is scheduled")
            if plan.rules:
                bits.append(f"{plan.rules} rule{'s' if plan.rules != 1 else ''} aimed at it")
            if plan.colors:
                bits.append("desktop colors are saved to follow it")
            row = plain(
                Adw.ActionRow,
                title=display.connector,
                subtitle="Not connected" + (" · " + " · ".join(bits) if bits else ""),
                subtitle_lines=3,
            )
            icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
            icon.set_pixel_size(20)
            icon.set_valign(Gtk.Align.CENTER)
            icon.add_css_class("remembered-icon")
            row.add_prefix(icon)
            forget = Gtk.Button(label="Forget", valign=Gtk.Align.CENTER)
            forget.set_tooltip_text(plan.refused or f"Stop remembering {display.connector}")
            forget.set_sensitive(not plan.refused)
            forget.connect("clicked", lambda *_args, c=display.connector: self.confirm_forget(c))
            row.add_suffix(forget)
            self._remembered_list.append(row)
            self.remembered_rows[display.connector] = row
        self._remembered_section.set_visible(bool(detached))

    def _set_compact(self, compact: bool) -> None:
        self._compact = compact
        self.arrangement.set_compact(compact)
        self._plays_ids = []
        self.refresh()

    # -- interactions -----------------------------------------------------------------------
    def _select(self, connector: str) -> None:
        if connector in self._connected():
            self._selected = connector
            self.refresh()

    @property
    def selected(self) -> str:
        return self._selected

    def _on_mode(self, group: Adw.ToggleGroup, _param: object) -> None:
        mode = group.get_active_name()
        if self._loading or not mode:
            return
        current = self._pending_mode.get(_MODE)
        if mode == (current[0] if current else self.scheduling.saved_display_mode()):
            return
        queue_intent(self._pending_mode, _MODE, mode)

        def settled(saved: bool) -> None:
            # The queued choice first, before the toast (it reaches the window).
            if settle_intent(self._pending_mode, _MODE):
                self.refresh()
            if saved:
                self.state.toast(
                    "Each display plays its own playlist again"
                    if mode == "independent"
                    else "Every display shows the same rotation"
                )

        self.scheduling.choose_display_mode(mode, settled)

    def _on_plays(self, row: Adw.ComboRow, _param: object) -> None:
        if self._loading:
            return
        index = row.get_selected()
        if not 0 <= index < len(self._plays_ids):
            return
        connector, pid = self._selected, self._plays_ids[index]
        pending = self._pending_assign.get(connector)
        current = (
            pending[0] if pending else self.scheduling.display_assignments().get(connector, "")
        )
        if pid == current:
            return
        queue_intent(self._pending_assign, connector, pid)

        def settled(saved: bool) -> None:
            # The queued choice first, before the toast (it reaches the window).
            if settle_intent(self._pending_assign, connector):
                self.refresh()
            if saved:
                self.state.toast(
                    f"{connector} plays “{self._name(pid)}” when nothing is scheduled"
                    if pid
                    else f"{connector} plays the default when nothing is scheduled"
                )

        self.scheduling.assign_display(connector, pid, settled)

    def _on_beats(self, row: Adw.SwitchRow, _param: object) -> None:
        if self._loading:
            return
        connector, wanted = self._selected, row.get_active()
        queue_intent(self._pending_beats, connector, wanted)

        def settled(_saved: bool) -> None:
            if settle_intent(self._pending_beats, connector):
                self.refresh()

        self.scheduling.set_beats_global_rules(connector, wanted, settled)

    def use_for_colors(self, connector: str) -> None:
        queue_intent(self._pending_colors, _COLORS, connector)

        def settled(saved: bool) -> None:
            # The queued choice first, before the toast (it reaches the window).
            if settle_intent(self._pending_colors, _COLORS):
                self.refresh()
            if saved:
                self.state.toast(f"Desktop colors now follow {connector}")

        self.scheduling.choose_color_source(connector, settled)
        self.refresh()

    def _on_play(self, row: Adw.ComboRow, _param: object) -> None:
        if self._loading:
            return
        index = row.get_selected()
        if not 0 <= index < len(self._play_ids):
            return
        connector = "" if self._mirrored() else self._selected
        self.scheduling.play_now(connector, self._play_ids[index])
        # The runtime's next status moves this row; until then it shows the last one.
        self._refresh_soon()

    def _on_rotation(self, button: Gtk.ToggleButton, verb: str) -> None:
        if self._loading:
            return
        wanted = button.get_active()
        if verb == "cycle":
            self.scheduling.set_display_rotation(self._selected, cycle=wanted)
        else:
            self.scheduling.set_display_rotation(self._selected, shuffle=wanted)
        self._refresh_soon()

    def _use_defaults(self) -> None:
        self.scheduling.use_saved_rotation(self._selected)
        self._refresh_soon()

    def _refresh_soon(self) -> None:
        """Back to the runtime's last answer once this signal has returned."""
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.idle_add(self._idle_refresh, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _resume(self, connector: str) -> None:
        self.scheduling.play_now("" if self._mirrored() else connector, "")

    def _show_in_library(self, connector: str) -> None:
        shown = self._shown(connector)
        wid = shown.id if shown is not None else ""
        self.state.navigate(f"library:{wid}" if wid else "library")

    def confirm_forget(self, connector: str) -> None:
        plan = self.scheduling.forget_plan(connector)
        if plan.refused:
            self.state.toast(plan.refused)
            return
        parts = []
        if plan.playlist:
            parts.append(f"its playlist (“{self._name(plan.playlist)}”)")
        if plan.rules:
            parts.append(f"{plan.rules} rule{'s' if plan.rules != 1 else ''} aimed at it")
        if plan.colors:
            parts.append("the desktop colors\u2019 choice")
        removed = (
            " and ".join(parts) if len(parts) < 3 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
        )
        dialog = Adw.AlertDialog(
            heading=f"Forget {connector}?",
            body=(
                f"{removed[:1].upper()}{removed[1:]} will be removed. If it\u2019s connected "
                "again, it follows the shared schedule."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def done(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "forget":
                self.forget(connector)

        def closed(closing: Adw.Dialog) -> None:
            if self._dialog is closing:
                self._dialog = None

        dialog.connect("response", done)
        dialog.connect("closed", closed)
        self._dialog = dialog
        dialog.present(self.widget)

    def forget(self, connector: str) -> None:
        def settled(saved: bool) -> None:
            self.refresh()  # what is saved now, before the toast (it reaches the window)
            if saved:
                self.state.toast(f"Forgot {connector}")

        self.scheduling.forget_display(connector, settled)

    # -- the tiles' menu ----------------------------------------------------------------------
    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()

        def add(name: str, callback: Callable[[str], None]) -> None:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, value: callback(value.get_string()))
            group.add_action(action)

        def scope(connector: str) -> str:
            return "all" if self._mirrored() else connector

        def step(direction: int) -> Callable[[str], None]:
            def run(connector: str) -> None:
                if self.state.controls is not None:
                    self.state.controls.step(direction, scope(connector))

            return run

        def random(connector: str) -> None:
            if self.state.controls is not None:
                self.state.controls.random(scope(connector))

        add("previous", step(-1))
        add("next", step(1))
        add("random", random)
        add("resume", self._resume)
        add("colors", self.use_for_colors)
        add("library", self._show_in_library)
        self.widget.insert_action_group("disp", group)

    def _popup_menu(self, tile: MonitorTile, x: float, y: float) -> None:
        connector = tile.display.connector
        self._select(connector)
        mirrored = self._mirrored()
        menu = Gio.Menu()
        if self.state.controls is not None and not self.scheduling.runtime_blocked():
            play = Gio.Menu()
            play.append("Previous wallpaper", f"disp.previous::{connector}")
            play.append("Next wallpaper", f"disp.next::{connector}")
            play.append("Random wallpaper", f"disp.random::{connector}")
            menu.append_section("On all displays" if mirrored else f"On {connector}", play)
        other = Gio.Menu()
        route = self._route(connector)
        if route is not None and route.route == "pick":
            other.append("Resume schedule", f"disp.resume::{connector}")
        if not mirrored and connector != self.scheduling.color_source().effective:
            other.append("Use for desktop colors", f"disp.colors::{connector}")
        shown = self._shown(connector)
        if shown is not None:
            other.append(
                f"Show “{shown.name}” in Library",
                f"disp.library::{connector}",
            )
        menu.append_section(None, other)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(tile)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", self._menu_closed)
        self._menu = popover
        popover.popup()

    def _menu_closed(self, popover: Gtk.PopoverMenu) -> None:
        def drop() -> bool:
            popover.unparent()
            if self._menu is popover:
                self._menu = None
            return False

        # Unparent later: the chosen action is activated after "closed".
        GLib.idle_add(drop, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]


def create(state: AppState) -> Page:
    scheduling = state.scheduling
    if scheduling is None:
        return not_ported("displays", "Displays", "video-display-symbolic")(state)
    return DisplaysPage(state, scheduling)
