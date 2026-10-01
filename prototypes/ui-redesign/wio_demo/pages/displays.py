"""Displays: what each monitor shows and how it is assigned.

The arrangement at the top mirrors the compositor's layout (to scale, like
GNOME Settings); the details below always describe the selected monitor. In
"Same on all displays" mode everything below applies to every display and the
per-display choices are kept aside until the mode is switched back.

Route precedence is the runtime's: your pick → a matching schedule rule (global
or for this display) → the display's own playlist → the default. So a
display's own playlist only plays when nothing is scheduled, and the page says so.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk

from .. import art, data, thumbs, ui
from ..models import Display, Rule, Wallpaper
from ..state import rule_matches
from . import Page
from .displays_arrangement import CSS as ARRANGEMENT_CSS
from .displays_arrangement import Arrangement, LinkGlyph, MonitorTile, TileInfo

# Outputs the runtime still keeps routes for although they are unplugged.
REMEMBERED = [
    {
        "connector": "eDP-1",
        "model": "Laptop screen",
        "icon": "computer-symbolic",
        "seen": "2 days ago",
        "playlist": "mc-night",
    },
    {
        "connector": "DP-2",
        "model": "Samsung Odyssey G7",
        "icon": "video-display-symbolic",
        "seen": "3 weeks ago",
        "playlist": "",
    },
]

SCALING = [
    ("fill", "Fill", "Crop to cover the screen"),
    ("fit", "Fit", "Show all of it, with bars"),
    ("stretch", "Stretch", "Squash it to the screen"),
]
DEFAULT_ADVANCED = {"fps": 0, "sound": False, "scaling": "fill", "covered": True}

MODES = {
    "mirrored": "One rotation, shown on every display",
    "independent": "Each display plays its own playlist",
}

CSS = (
    ARRANGEMENT_CSS
    + """
.displays-page .mode-caption { margin-top: 2px; }
.display-title { font-size: 1.35em; font-weight: 800; }
.now-name { font-weight: 700; }
.now-thumb { padding: 0; border-radius: 8px; }
.remembered-icon {
  color: alpha(currentColor, 0.7);
  background-color: alpha(currentColor, 0.08);
  border-radius: 10px;
  min-width: 40px; min-height: 40px;
}
.display-controls { padding: 6px 10px 6px 14px; min-height: 36px; }
.layout-caption { font-size: 0.85em; }
.plays-cover { border-radius: 6px; min-width: 28px; min-height: 28px; }
"""
)


class DisplaysPage(Page):
    name = "displays"
    title = "Displays"

    def __init__(self, state) -> None:
        super().__init__(state)
        ui.add_css(CSS)
        self._selected = self._lead()
        self._held: set[str] = set()  # displays paused on their own while the rest play
        self._saved_assigned: dict[str, str] = {}  # kept aside while displays are linked
        self._advanced = {c: dict(DEFAULT_ADVANCED) for c in state.connectors()}
        self._remembered = [dict(item) for item in REMEMBERED]
        self._plays_ids: list[str] = []
        self._building = False
        self._compact = False
        self._linked_shown: bool | None = None
        self._playback_seen = state.playback
        self._identify_source = 0
        self._focus_source = 0
        self._menu: Gtk.PopoverMenu | None = None
        self._dialog: Adw.AlertDialog | None = None
        self._plays_checks: dict[int, Gtk.Image] = {}
        self._demo_rules: list[Rule] | None = None  # the real list while a scene hides some
        self._initial = self._snapshot()

        # -- header ----------------------------------------------------------------
        self._identify = Gtk.Button(label="Identify")
        self._identify.set_tooltip_text("Show each display’s number on its screen")
        self._identify.connect("clicked", lambda *_: self._identify_displays())

        # -- mode ------------------------------------------------------------------
        self._mode = Adw.ToggleGroup(homogeneous=True, halign=Gtk.Align.CENTER)
        for key, label, tooltip in (
            ("mirrored", "Same on all displays", "One rotation, in sync everywhere"),
            ("independent", "Each display separately", "Give each display its own playlist"),
        ):
            toggle = Adw.Toggle(name=key, label=label)
            toggle.set_tooltip(tooltip)
            self._mode.add(toggle)
        self._mode.set_active_name(state.display_mode)
        self._mode.connect("notify::active-name", self._on_mode)
        self._mode_caption = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True)
        self._mode_caption.add_css_class("dimmed")
        self._mode_caption.add_css_class("mode-caption")

        # -- arrangement -------------------------------------------------------------
        self.arrangement = Arrangement(self._select, self._popup_menu)
        self.arrangement.set_displays(state.displays)
        self._layout_caption = Gtk.Label(label="Positions come from your compositor", xalign=1)
        self._layout_caption.add_css_class("dimmed")
        self._layout_caption.add_css_class("layout-caption")

        # -- details -------------------------------------------------------------------
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        details.append(self._build_heading())
        details.append(self._build_now())
        details.append(self._build_settings())
        self._remembered_section = self._build_remembered_section()
        details.append(self._remembered_section)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=14,
            margin_bottom=28,
            margin_start=18,
            margin_end=18,
        )
        content.append(self._mode)
        content.append(self._mode_caption)
        arrangement_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8)
        arrangement_box.append(self.arrangement)
        arrangement_box.append(self._layout_caption)
        content.append(arrangement_box)
        details.set_margin_top(10)
        content.append(details)
        self._content = content

        clamp = Adw.Clamp(maximum_size=780, tightening_threshold=640)
        clamp.set_child(content)
        self._scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._scroller.set_child(clamp)

        breakpoint_bin = Adw.BreakpointBin(width_request=360, height_request=300)
        breakpoint_bin.add_css_class("displays-page")
        breakpoint_bin.set_child(self._scroller)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.connect("apply", lambda *_: self._set_compact(True))
        narrow.connect("unapply", lambda *_: self._set_compact(False))
        breakpoint_bin.add_breakpoint(narrow)
        self.widget = breakpoint_bin
        self._install_actions()

        self._rebuild_plays_model()
        self._build_remembered()
        state.connect("changed", self._on_changed)
        self.refresh()

    # -- Page API ------------------------------------------------------------------------
    def header_end(self) -> list[Gtk.Widget]:
        return [self._identify]

    def activate(self, argument: str | None) -> None:
        if self.state.display_mode == "mirrored":
            self._sync_linked()
        if argument in self.state.connectors():
            self._selected = argument
        self.refresh()

    def demo(self, scene: str) -> None:
        what, _, arg = scene.partition(":")
        self._demo_reset()
        state = self.state
        if what == "select":
            self._select(arg)
        elif what == "advanced":
            self._select(arg or self._selected)
            self._advanced_row.set_expanded(True)
            self._scroll_to(self._settings_section)
        elif what == "pick":  # a temporary "your pick" playlist on one display
            connector = arg or "HDMI-A-1"
            state.manual[connector] = "mc-night"
            state.current[connector] = state.playlist("mc-night").entries[1]
            self._select(connector)
            state.emit_changed("now")
        elif what == "assigned":  # its own playlist is set, but the schedule wins right now
            connector = arg or "HDMI-A-1"
            state.assigned[connector] = "cozy-rain"
            self._select(connector)
            state.emit_changed("displays", "now")
        elif what == "unscheduled":  # nothing is scheduled now, so its own playlist plays
            connector = arg or "HDMI-A-1"
            # Hide the rules that match now from this state only; rule objects are shared data.
            self._demo_rules = state.rules
            state.rules = [r for r in state.rules if not rule_matches(r, state.now, connector)]
            state.assigned[connector] = "cozy-rain"
            state.current[connector] = state.playlist("cozy-rain").entries[0]
            self._select(connector)
            state.emit_changed("schedule", "displays", "now")
        elif what == "paused":
            connector = arg or "HDMI-A-1"
            self._held.add(connector)
            self._select(connector)
            self.refresh()
        elif what == "colors":
            state.color_display = arg or "HDMI-A-1"
            if arg in state.connectors():
                self._select(arg)
            state.emit_changed("displays")
        elif what == "identify":
            self._identify_displays()
        elif what == "menu":
            tile = self.arrangement.tiles[arg or "HDMI-A-1"]
            GLib.timeout_add(
                300, lambda: (self._popup_menu(tile, tile.get_width() * 0.4, tile.get_height() * 0.45), False)[1]
            )
        elif what == "plays":
            self._select(arg or self._selected)
            self._scroll_to(self._plays, above=150)
            GLib.timeout_add(450, lambda: (self._plays.activate(), False)[1])
        elif what == "forget":
            self._scroll_to(self._remembered_section)
            GLib.timeout_add(300, lambda: (self._confirm_forget(self._remembered[0]), False)[1])
        elif what == "mode":
            self._mode.set_active_name(arg or "mirrored")
        elif what == "bottom":
            self._scroll_to(self._remembered_section)

    # -- helpers ---------------------------------------------------------------------------
    def _lead(self) -> str:
        for display in self.state.displays:
            if display.primary:
                return display.connector
        return self.state.connectors()[0]

    def _display(self, connector: str) -> Display:
        return next(d for d in self.state.displays if d.connector == connector)

    def _mirrored(self) -> bool:
        return self.state.display_mode == "mirrored"

    def _key(self, connector: str) -> str:
        """Linked displays all report the lead display's truth."""
        return self._lead() if self._mirrored() else connector

    def _scope(self, connector: str) -> str:
        return "all" if self._mirrored() else connector

    def _shown(self, connector: str) -> Wallpaper:
        return self.state.wallpaper(self.state.current[self._key(connector)])

    def _color_display(self) -> str:
        chosen = self.state.color_display
        return chosen if chosen in self.state.connectors() else self._lead()

    def _paused(self, connector: str) -> bool:
        state = self.state
        return state.playback == "paused" or (not self._mirrored() and connector in self._held)

    def _status(self, connector: str) -> tuple[str, str, bool]:
        state = self.state
        if not state.service_running:
            return "Not running", "media-playback-stop-symbolic", True
        if state.playback == "stopped":
            return "Still only", "media-playback-stop-symbolic", False
        if self._paused(connector):
            return "Paused", "media-playback-pause-symbolic", True
        if state.on_battery and state.stop_on_battery and self._shown(connector).is_moving:
            return "Still on battery", "battery-caution-symbolic", False
        return "Playing", "media-playback-start-symbolic", False

    def _why(self, connector: str) -> str:
        state = self.state
        key = self._key(connector)
        if key in state.manual:
            pid = state.manual[key]
            return "Your pick" if pid == "quick" else f"{state.playlist(pid).name} · your pick"
        resolution = state.resolution(key)
        until = f" until {resolution.until}" if resolution.until else ""
        name = state.playlist(resolution.playlist).name
        if resolution.rule is not None:
            return f"{name} · from schedule{until}"
        # Nothing is scheduled right now: the display's own playlist, else the default.
        if state.assigned.get(key):
            return f"{name} · this display’s playlist{until}"
        return f"{name} · nothing scheduled{until}"

    def _unscheduled_status(self, connector: str) -> str:
        """Is "When nothing is scheduled" what's playing right now? Say so plainly."""
        state = self.state
        resolution = state.resolution(connector)
        if resolution.rule is not None:
            text = f"Not in use now · “{state.playlist(resolution.rule.playlist).name}” is scheduled"
            return text + (f" until {resolution.until}" if resolution.until else "")
        if connector in state.manual:
            return "Not in use now · your pick is showing"
        return "In use now" + (f" · nothing is scheduled until {resolution.until}" if resolution.until else "")

    def _own_playlist_in_effect(self, connector: str) -> str:
        """The display's own playlist id when it is what plays after a pick, else ""."""
        if self._mirrored() or self.state.resolution(connector).rule is not None:
            return ""
        return self.state.assigned.get(connector, "")

    def _source(self, connector: str) -> str:
        """The short "where from" for a tile: a playlist name or "your pick"."""
        pid = self.state.effective_playlist(self._key(connector))
        return "your pick" if pid == "quick" else self.state.playlist(pid).name

    def _snapshot(self) -> tuple:
        state = self.state
        return (
            state.display_mode,
            dict(state.current),
            dict(state.manual),
            dict(state.assigned),
            state.playback,
            set(self._held),
            dict(self._saved_assigned),
            state.color_display,
        )

    def _restore(self, snapshot: tuple, emit: bool = True) -> None:
        state = self.state
        (mode, current, manual, assigned, playback, held, saved, colors) = snapshot
        state.display_mode = mode
        state.current, state.manual, state.assigned = dict(current), dict(manual), dict(assigned)
        state.playback = playback
        self._playback_seen = playback
        self._held, self._saved_assigned = set(held), dict(saved)
        state.color_display = colors
        if emit:
            state.emit_changed("displays", "now", "playback")

    def _demo_reset(self) -> None:
        # Scenes share one window: close what the previous scene opened.
        if self._menu is not None:
            self._menu.popdown()
        if self._dialog is not None:
            self._dialog.force_close()
        _close_popovers(self._plays)
        # Closed popovers hand focus back a moment later; don't let that scroll the scene.
        viewport = self._scroller.get_child()
        viewport.set_scroll_to_focus(False)
        if self._focus_source:
            GLib.source_remove(self._focus_source)

        def settle() -> bool:
            viewport.set_scroll_to_focus(True)
            self._focus_source = 0
            return False

        self._focus_source = GLib.timeout_add(1500, settle)
        if self._identify_source:
            GLib.source_remove(self._identify_source)
            self._identify_source = 0
        for tile in self.arrangement.tiles.values():
            tile.set_identify(False)
        self._scroller.get_vadjustment().set_value(0)
        if self._demo_rules is not None:
            self.state.rules = self._demo_rules
            self._demo_rules = None
        mode = self.state.display_mode  # the scene flags decide the mode
        self._restore(self._initial, emit=False)
        self.state.display_mode = mode
        self._advanced = {c: dict(DEFAULT_ADVANCED) for c in self.state.connectors()}
        self._remembered = [dict(item) for item in REMEMBERED]
        self._build_remembered()
        self._advanced_row.set_expanded(False)
        if mode == "mirrored":
            self._sync_linked(emit=False)
        self.state.emit_changed("schedule", "displays", "now", "playback")

    def _sync_linked(self, emit: bool = True) -> None:
        """Make every display match the lead one (what "Same on all displays" means)."""
        state, lead = self.state, self._lead()
        changed = False
        for connector in state.connectors():
            if state.assigned.get(connector):
                self._saved_assigned[connector] = state.assigned[connector]
                state.assigned[connector] = ""
                changed = True
            if connector == lead:
                continue
            if state.current[connector] != state.current[lead]:
                state.current[connector] = state.current[lead]
                changed = True
            if state.manual.get(connector) != state.manual.get(lead):
                if lead in state.manual:
                    state.manual[connector] = state.manual[lead]
                else:
                    state.manual.pop(connector, None)
                changed = True
        if self._held:
            self._held.clear()
            changed = True
        if changed and emit:
            state.emit_changed("displays", "now")

    def _scroll_to(self, widget: Gtk.Widget, above: int = 8) -> None:
        def scroll() -> bool:
            ok, point = widget.compute_point(self._content, Graphene.Point().init(0, 0))
            if ok:
                self._scroller.get_vadjustment().set_value(max(0, point.y - above))
            return False

        GLib.timeout_add(250, scroll)

    def _section(self, title: str, child: Gtk.Widget, extra: Gtk.Widget | None = None) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(spacing=6)
        label = Gtk.Label(label=title.upper(), xalign=0, hexpand=True, valign=Gtk.Align.CENTER)
        label.add_css_class("section-label")
        row.append(label)
        if extra:
            row.append(extra)
        box.append(row)
        box.append(child)
        return box

    # -- building ---------------------------------------------------------------------------
    def _build_heading(self) -> Gtk.Widget:
        head = Gtk.Box(spacing=12)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self._d_title = Gtk.Label(xalign=0, wrap=True)
        self._d_title.add_css_class("display-title")
        self._d_meta = ui.dim("")
        self._d_meta.add_css_class("numeric")
        texts.append(self._d_title)
        texts.append(self._d_meta)
        head.append(texts)
        self._d_primary = ui.pill("Primary", "starred-symbolic", "subtle")
        self._d_primary.set_valign(Gtk.Align.CENTER)
        self._d_primary.set_tooltip_text("Your compositor’s primary display")
        head.append(self._d_primary)
        return head

    def _build_now(self) -> Gtk.Widget:
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")

        row = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=10, margin_end=12)
        self._now_thumb = ui.Thumb.of(self._shown(self._selected), 128, 72, radius=8, size=(320, 180))
        thumb_button = Gtk.Button(valign=Gtk.Align.CENTER)
        thumb_button.add_css_class("flat")
        thumb_button.add_css_class("now-thumb")
        thumb_button.set_child(self._now_thumb)
        thumb_button.set_tooltip_text("Show in Library")
        thumb_button.connect("clicked", lambda *_: self.state.navigate("library:" + self._shown(self._selected).id))
        row.append(thumb_button)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER, hexpand=True)
        self._now_name = Gtk.Label(xalign=0, ellipsize=3)
        self._now_name.add_css_class("now-name")
        self._now_why = ui.dim("")
        status = Gtk.Box(spacing=6)
        self._now_dot = Gtk.Box(valign=Gtk.Align.CENTER)
        self._now_dot.add_css_class("status-dot")
        self._now_status = Gtk.Label(xalign=0)
        self._now_status.add_css_class("caption")
        self._now_status.add_css_class("dimmed")
        status.append(self._now_dot)
        status.append(self._now_status)
        for widget in (self._now_name, self._now_why, status):
            texts.append(widget)
        row.append(texts)
        first = Gtk.ListBoxRow(activatable=False, child=row)
        group.append(first)

        # Playback controls live in the player bar only (its scope menu picks a
        # display). This row just says when it changes and offers to resume.
        controls = Gtk.Box(spacing=4)
        controls.add_css_class("display-controls")
        self._timing = Gtk.Label(valign=Gtk.Align.CENTER, hexpand=True, xalign=0)
        self._timing.add_css_class("dimmed")
        self._timing.add_css_class("caption")
        self._timing.add_css_class("numeric")
        controls.append(self._timing)
        self._resume = Gtk.Button(label="Resume schedule", valign=Gtk.Align.CENTER)
        self._resume.add_css_class("pill")
        self._resume.connect("clicked", lambda *_: self._resume_display(self._selected))
        controls.append(self._resume)
        self._now_actions = Gtk.ListBoxRow(activatable=False, child=controls)
        group.append(self._now_actions)

        self._now_scope = Gtk.Label()
        self._now_scope.add_css_class("dimmed")
        self._now_scope.add_css_class("caption")
        return self._section("Now showing", group, self._now_scope)

    def _build_settings(self) -> Gtk.Widget:
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")

        # The display's own playlist: only used while no schedule rule applies to it.
        self._plays = Adw.ComboRow(title="When nothing is scheduled")
        self._plays.set_factory(self._value_factory())
        self._plays.set_list_factory(self._plays_factory())
        self._plays.connect("notify::selected", self._on_plays)
        group.append(self._plays)

        self._schedule_link = Adw.ActionRow(title="Schedule for this display…", activatable=True)
        calendar = Gtk.Image.new_from_icon_name("x-office-calendar-symbolic")
        self._schedule_link.add_prefix(calendar)
        self._schedule_link.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self._schedule_link.connect("activated", lambda *_: self.state.navigate(f"schedule:new:{self._selected}"))
        group.append(self._schedule_link)

        self._linked_note = Adw.ActionRow(title="Same rotation on every display", subtitle_lines=2)
        linked_icon = LinkGlyph(16)
        linked_icon.add_css_class("dimmed")
        self._linked_note.add_prefix(linked_icon)
        group.append(self._linked_note)

        self._colors_row = Adw.ActionRow(title="Desktop colors")
        self._colors_swatches = ui.Swatches([], size=14, overlap=True)
        self._colors_row.add_prefix(self._colors_swatches)
        self._colors_use = Gtk.Button(label="Use this display", valign=Gtk.Align.CENTER)
        self._colors_use.set_tooltip_text("Noctalia has one palette: take it from this display’s wallpaper")
        self._colors_use.connect("clicked", lambda *_: self._set_color_display(self._selected))
        self._colors_row.add_suffix(self._colors_use)
        self._colors_check = Gtk.Image.new_from_icon_name("object-select-symbolic")
        self._colors_check.add_css_class("accent")
        self._colors_check.set_tooltip_text("Desktop colors follow this display")
        self._colors_row.add_suffix(self._colors_check)
        group.append(self._colors_row)

        self._advanced_row = Adw.ExpanderRow(title="Advanced")
        self._advanced_reset = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        self._advanced_reset.add_css_class("flat")
        self._advanced_reset.set_tooltip_text("Use the app’s defaults on this display")
        self._advanced_reset.connect("clicked", lambda *_: self._reset_advanced(self._selected))
        self._advanced_row.add_suffix(self._advanced_reset)

        self._fps = Adw.ComboRow(title="Frame rate limit")
        self._fps.connect("notify::selected", self._on_fps)
        self._advanced_row.add_row(self._fps)
        self._sound = Adw.SwitchRow(title="Sound", subtitle="Audio from videos and scenes")
        self._sound.connect("notify::active", self._on_advanced_switch, "sound")
        self._advanced_row.add_row(self._sound)
        self._scaling_row = Adw.ActionRow(title="Scaling")
        self._scaling = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for key, label, _description in SCALING:
            self._scaling.add(Adw.Toggle(name=key, label=label))
        self._scaling.connect("notify::active-name", self._on_scaling)
        self._scaling_row.add_suffix(self._scaling)
        self._advanced_row.add_row(self._scaling_row)
        self._covered = Adw.SwitchRow(title="Pause when covered", subtitle="Saves power behind full-screen windows")
        self._covered.connect("notify::active", self._on_advanced_switch, "covered")
        self._advanced_row.add_row(self._covered)
        group.append(self._advanced_row)

        self._settings_section = self._section("This display", group)
        return self._settings_section

    def _build_remembered_section(self) -> Gtk.Widget:
        self._remembered_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._remembered_list.add_css_class("boxed-list")
        return self._section("Not connected", self._remembered_list)

    def _build_remembered(self) -> None:
        self._remembered_list.remove_all()
        for item in self._remembered:
            plays = (
                f"{self.state.playlist(item['playlist']).name} when nothing is scheduled"
                if item["playlist"]
                else f"Default ({self.state.playlist(self.state.fallback).name}) when nothing is scheduled"
            )
            row = Adw.ActionRow(
                title=f"{item['connector']} · {item['model']}", subtitle=f"Last seen {item['seen']} · {plays}"
            )
            icon = Gtk.Image.new_from_icon_name(item["icon"])
            icon.set_pixel_size(20)
            icon.set_valign(Gtk.Align.CENTER)
            icon.add_css_class("remembered-icon")
            row.add_prefix(icon)
            forget = Gtk.Button(label="Forget", valign=Gtk.Align.CENTER)
            forget.set_tooltip_text(f"Stop remembering {item['connector']}")
            forget.connect("clicked", lambda *_, it=item: self._confirm_forget(it))
            row.add_suffix(forget)
            self._remembered_list.append(row)
        self._remembered_section.set_visible(bool(self._remembered))

    def _rebuild_plays_model(self) -> None:
        state = self.state
        # The default is offered once, as "Default (…)", not again under its own name.
        playlists = [p for p in state.playlists if p.id != state.fallback]
        self._plays_ids = [""] + [p.id for p in playlists]
        default = state.playlist(state.fallback).name
        labels = ["Default" if self._compact else f"Default ({default})"] + [p.name for p in playlists]
        self._building = True
        self._plays.set_model(Gtk.StringList.new(labels))
        self._building = False

    @staticmethod
    def _value_factory() -> Gtk.ListItemFactory:
        """The chosen value beside the row title. It never ellipsizes, so the
        status line underneath wraps instead of the playlist name being cut."""
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(Gtk.Label(xalign=1)))
        factory.connect("bind", lambda _f, item: item.get_child().set_label(item.get_item().get_string()))
        return factory

    def _plays_factory(self) -> Gtk.ListItemFactory:
        """Dropdown rows with a cover, a name and a short detail."""
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, item: Gtk.ListItem) -> None:
            box = Gtk.Box(spacing=10)
            image = Gtk.Image(pixel_size=28, valign=Gtk.Align.CENTER)
            image.set_overflow(Gtk.Overflow.HIDDEN)
            image.add_css_class("plays-cover")
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
            title = Gtk.Label(xalign=0)
            detail = Gtk.Label(xalign=0)
            detail.add_css_class("caption")
            detail.add_css_class("dimmed")
            texts.append(title)
            texts.append(detail)
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_margin_start(12)
            box.append(image)
            box.append(texts)
            box.append(check)
            item.set_child(box)

        def bind(_factory, item: Gtk.ListItem) -> None:
            position = item.get_position()
            if position >= len(self._plays_ids):
                return
            pid = self._plays_ids[position]
            image = item.get_child().get_first_child()
            texts = image.get_next_sibling()
            check = texts.get_next_sibling()
            title, detail = texts.get_first_child(), texts.get_last_child()
            if not pid:
                default = self.state.playlist(self.state.fallback)
                image.set_from_icon_name(default.icon or "view-grid-symbolic")
                image.set_pixel_size(20)
                title.set_label("Default")
                detail.set_label(default.name)
            else:
                playlist = self.state.playlist(pid)
                if playlist.automatic:
                    image.set_from_icon_name(playlist.icon or "view-grid-symbolic")
                    image.set_pixel_size(20)
                else:
                    image.set_from_paintable(art.mosaic(data.cover_keys(playlist), 64))
                    image.set_pixel_size(28)
                title.set_label(playlist.name)
                count = len(playlist.entries)
                detail.set_label(f"{count} wallpaper" + ("" if count == 1 else "s"))
            check.set_opacity(1 if position == self._plays.get_selected() else 0)
            self._plays_checks[position] = check

        def unbind(_factory, item: Gtk.ListItem) -> None:
            self._plays_checks.pop(item.get_position(), None)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
        return factory

    # -- refresh ------------------------------------------------------------------------------
    def _on_changed(self, _state, topic: str) -> None:
        if topic in ("playlists", "schedule", "settings"):
            self._rebuild_plays_model()
            self._build_remembered()
        if topic == "playback" and self.state.playback != self._playback_seen:
            self._held.clear()  # the player bar paused or resumed every display
        self._playback_seen = self.state.playback
        if topic in ("now", "playback", "displays", "playlists", "schedule", "settings", "system", "theme", "library"):
            self.refresh()

    def refresh(self) -> None:
        state = self.state
        mirrored = self._mirrored()
        connector = self._selected
        display = self._display(connector)
        self._building = True

        self._mode.set_active_name(state.display_mode)
        self._mode_caption.set_label(MODES[state.display_mode])
        count = len(state.displays)
        self.set_title("Displays", f"{count} connected" if count != 1 else "1 connected")

        # Arrangement
        if self._linked_shown != mirrored:
            self.arrangement.set_linked(mirrored)
            self._linked_shown = mirrored
        colors = self._color_display()
        for item in state.displays:
            wallpaper = self._shown(item.connector)
            status, icon, paused = self._status(item.connector)
            self.arrangement.tiles[item.connector].update(
                TileInfo(
                    texture=thumbs.texture(wallpaper, 640, 360),
                    wallpaper=wallpaper.name,
                    source=self._source(item.connector),
                    status=status,
                    status_icon=icon,
                    paused=paused,
                    colors=not mirrored and item.connector == colors,
                )
            )
        self.arrangement.select(connector)

        # Heading
        self._d_title.set_label(f"{display.connector} · {display.model}")
        self._d_meta.set_label(f"{display.mode} · {round(display.scale * 100)}%")
        self._d_primary.set_visible(display.primary)

        # Now showing
        wallpaper = self._shown(connector)
        self._now_thumb.show(wallpaper, 320, 180)
        self._now_name.set_label(wallpaper.name)
        self._now_why.set_label(self._why(connector))
        status, _icon, _paused = self._status(connector)
        self._now_status.set_label(status)
        for css in ("paused", "stopped"):
            self._now_dot.remove_css_class(css)
        if status in ("Not running", "Still only"):
            self._now_dot.add_css_class("stopped")
        elif status in ("Paused", "Still on battery"):
            self._now_dot.add_css_class("paused")
        self._now_scope.set_label("All displays" if mirrored else f"{connector} only")
        key = self._key(connector)
        manual = key in state.manual
        own = self._own_playlist_in_effect(connector)
        self._resume.set_visible(manual)
        self._resume.set_label(f"Resume “{state.playlist(own).name}”" if own else "Resume schedule")
        self._resume.set_tooltip_text(
            "Nothing is scheduled now: go back to this display’s playlist"
            if own
            else "Go back to what the schedule says"
        )
        effective = state.effective_playlist(key)
        if manual or not state.service_running:
            self._timing.set_label("")
        elif state.rotate and effective != "quick":
            self._timing.set_label(f"Next in {state.next_change_minutes} min")
        else:
            self._timing.set_label("Holding")
        self._timing.set_visible(bool(self._timing.get_label()) and not self._compact)
        self._now_actions.set_visible(self._timing.get_visible() or self._resume.get_visible())

        # Plays, colors, advanced
        self._plays.set_visible(not mirrored)
        self._schedule_link.set_visible(not mirrored)
        self._colors_row.set_visible(not mirrored)
        self._linked_note.set_visible(mirrored)
        self._plays.set_subtitle(self._unscheduled_status(connector))
        self._schedule_link.set_tooltip_text(f"Add a rule that only applies to {connector}")
        selected_id = state.assigned.get(connector, "")
        self._plays.set_selected(self._plays_ids.index(selected_id) if selected_id in self._plays_ids else 0)
        for position, check in self._plays_checks.items():
            check.set_opacity(1 if position == self._plays.get_selected() else 0)
        saved = [f"{c} keeps “{state.playlist(p).name}”" for c, p in self._saved_assigned.items()]
        self._linked_note.set_subtitle(
            f"{', '.join(saved)} for later." if saved else "Choose “Each display separately” to set this one apart."
        )

        chosen = state.color_display
        is_source = connector == colors
        self._colors_swatches.set_colors(data.wallpaper_swatches(wallpaper, state.dark)[1:4])
        if chosen and chosen not in state.connectors():
            subtitle = f"{chosen} isn’t connected, so {colors} is used"
        elif is_source:
            subtitle = "Follow this display’s wallpaper"
        else:
            subtitle = f"Follow {colors}"
        self._colors_row.set_subtitle(subtitle)
        self._colors_use.set_visible(not is_source)
        self._colors_check.set_visible(is_source)

        self._load_advanced(connector)
        self._building = False

    def _set_compact(self, compact: bool) -> None:
        self._compact = compact
        self.arrangement.set_compact(compact)
        self._rebuild_plays_model()
        self._layout_caption.set_visible(not compact)
        self.refresh()

    # -- advanced ---------------------------------------------------------------------------------
    def _fps_options(self, connector: str) -> list[str]:
        options = ["Default (30 fps)", "15 fps", "24 fps", "30 fps", "60 fps"]
        refresh = self._display(connector).mode.rpartition("@")[2].strip().split(" ")[0]
        if refresh.isdigit() and int(refresh) > 60:
            options.append(f"{refresh} fps")
        return options

    def _load_advanced(self, connector: str) -> None:
        values = self._advanced.setdefault(connector, dict(DEFAULT_ADVANCED))
        options = self._fps_options(connector)
        model = self._fps.get_model()
        if model is None or [model.get_string(i) for i in range(model.get_n_items())] != options:
            self._fps.set_model(Gtk.StringList.new(options))
        self._fps.set_selected(min(values["fps"], len(options) - 1))
        self._sound.set_active(values["sound"])
        self._scaling.set_active_name(values["scaling"])
        self._scaling_row.set_subtitle(next(d for k, _l, d in SCALING if k == values["scaling"]))
        self._covered.set_active(values["covered"])
        self._advanced_row.set_subtitle(self._advanced_summary(connector))
        self._advanced_reset.set_visible(values != DEFAULT_ADVANCED)

    def _advanced_summary(self, connector: str) -> str:
        values = self._advanced[connector]
        bits = []
        if values["scaling"] != "fill":
            bits.append(next(label for key, label, _d in SCALING if key == values["scaling"]))
        if values["fps"]:
            bits.append(f"{self._fps_options(connector)[values['fps']]} max")
        if values["sound"]:
            bits.append("Sound on")
        if not values["covered"]:
            bits.append("Plays when covered")
        return " · ".join(bits) if bits else "Frame rate, sound, scaling"

    def _set_advanced(self, key: str, value) -> None:
        if self._building:
            return
        self._advanced[self._selected][key] = value
        self._building = True
        self._load_advanced(self._selected)
        self._building = False

    def _on_fps(self, row: Adw.ComboRow, _param) -> None:
        self._set_advanced("fps", row.get_selected())

    def _on_advanced_switch(self, row: Adw.SwitchRow, _param, key: str) -> None:
        self._set_advanced(key, row.get_active())

    def _on_scaling(self, group: Adw.ToggleGroup, _param) -> None:
        if group.get_active_name():
            self._set_advanced("scaling", group.get_active_name())

    def _reset_advanced(self, connector: str) -> None:
        before = dict(self._advanced[connector])
        self._advanced[connector] = dict(DEFAULT_ADVANCED)
        self.refresh()

        def undo() -> None:
            self._advanced[connector] = before
            self.refresh()

        self.state.toast(f"{connector} uses the app’s defaults again", undo)

    # -- interactions ------------------------------------------------------------------------------
    def _select(self, connector: str) -> None:
        if connector not in self.state.connectors():
            return
        self._selected = connector
        self.refresh()

    def _on_mode(self, group: Adw.ToggleGroup, _param) -> None:
        mode = group.get_active_name()
        state = self.state
        if self._building or not mode or mode == state.display_mode:
            return
        before = self._snapshot()
        if mode == "mirrored":
            self._sync_linked(emit=False)
        else:
            for connector, pid in self._saved_assigned.items():
                state.assigned[connector] = pid
                # Its own playlist only plays where nothing is scheduled right now.
                if connector not in state.manual and state.resolution(connector).rule is None:
                    state.current[connector] = state.playlist(pid).entries[0]
            self._saved_assigned = {}
        state.display_mode = mode
        state.emit_changed("displays", "now")

        def undo() -> None:
            self._restore(before)

        if mode == "mirrored":
            name = state.wallpaper(state.current[self._lead()]).name
            state.toast(f"All displays now show “{name}”", undo)
        elif any(state.assigned.values()):
            state.toast("Each display has its own settings again", undo)

    def _on_plays(self, row: Adw.ComboRow, _param) -> None:
        if self._building:
            return
        index = row.get_selected()
        if index == Gtk.INVALID_LIST_POSITION or index >= len(self._plays_ids):
            return
        pid, connector = self._plays_ids[index], self._selected
        # Let the dropdown close before the page updates around it.
        GLib.idle_add(lambda: (self._assign(connector, pid), False)[1])

    def _assign(self, connector: str, pid: str) -> None:
        state = self.state
        if state.assigned.get(connector, "") == pid:
            return
        before = self._snapshot()
        state.assigned[connector] = pid
        resolution = state.resolution(connector)
        in_use = resolution.rule is None and connector not in state.manual
        playlist = state.playlist(resolution.playlist)
        if in_use and state.current[connector] not in playlist.entries:
            state.current[connector] = playlist.entries[0]
        state.emit_changed("displays", "now")
        if in_use:
            text = f"{connector} now plays “{playlist.name}”"
        elif pid:
            text = f"{connector} plays “{state.playlist(pid).name}” when nothing is scheduled"
        else:
            default = state.playlist(state.fallback).name
            text = f"{connector} plays the default (“{default}”) when nothing is scheduled"
        state.toast(text, lambda: self._restore(before))

    def _toggle_pause(self, connector: str) -> None:
        state = self.state
        if not state.service_running:
            state.set_service_running(True)
            state.toast("Wallpaper service started")
            return
        if state.playback == "stopped":
            self._held.clear()
            state.playback = "playing"
            self._playback_seen = state.playback
            state.emit_changed("playback")
            return
        if self._mirrored():
            state.toggle_play()
            return
        connectors = set(state.connectors())
        if self._paused(connector):
            if state.playback != "playing":
                state.playback = "playing"
                self._held = connectors - {connector}  # the others stay paused
            else:
                self._held.discard(connector)
        else:
            self._held.add(connector)
            if self._held >= connectors:  # everything paused: that's plain "paused"
                self._held.clear()
                state.playback = "paused"
        self._playback_seen = state.playback
        state.emit_changed("playback")

    def _resume_display(self, connector: str) -> None:
        state = self.state
        own = self._own_playlist_in_effect(connector)
        if not own:
            state.resume_schedule(self._scope(connector))
            return
        before = self._snapshot()
        state.manual.pop(connector, None)
        playlist = state.playlist(own)
        state.current[connector] = playlist.entries[0]
        state.emit_changed("now", "playback")
        state.toast(f"{connector} is back to “{playlist.name}”", lambda: self._restore(before))

    def _set_color_display(self, connector: str) -> None:
        state = self.state
        before = state.color_display
        state.color_display = connector
        state.emit_changed("displays", "now")

        def undo() -> None:
            state.color_display = before
            state.emit_changed("displays", "now")

        state.toast(f"Desktop colors now follow {connector}", undo)

    def _identify_displays(self) -> None:
        for tile in self.arrangement.tiles.values():
            tile.set_identify(True)
        if self._identify_source:
            GLib.source_remove(self._identify_source)

        def done() -> bool:
            for tile in self.arrangement.tiles.values():
                tile.set_identify(False)
            self._identify_source = 0
            return False

        self._identify_source = GLib.timeout_add(3000, done)
        self.state.toast("Each display shows its number for a moment")

    def _confirm_forget(self, item: dict) -> None:
        connector = item["connector"]
        dialog = Adw.AlertDialog(
            heading=f"Forget {connector}?",
            body="Its playlist and settings are removed. If it’s plugged in again, it starts with the schedule.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def done(_dialog, response: str) -> None:
            if response != "forget" or item not in self._remembered:
                return
            index = self._remembered.index(item)
            self._remembered.remove(item)
            self._build_remembered()

            def undo() -> None:
                self._remembered.insert(index, item)
                self._build_remembered()

            self.state.toast(f"Forgot {connector}", undo)

        dialog.connect("response", done)
        dialog.connect("closed", lambda d: setattr(self, "_dialog", None) if self._dialog is d else None)
        self._dialog = dialog
        dialog.present(self.widget.get_root())

    # -- context menu ---------------------------------------------------------------------------
    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        state = self.state

        def add(name: str, callback) -> None:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, value: callback(value.get_string()))
            group.add_action(action)

        add("previous", lambda c: state.step(-1, self._scope(c)))
        add("next", lambda c: state.step(1, self._scope(c)))
        add("random", lambda c: state.random(self._scope(c)))
        add("pause", self._toggle_pause)
        add("resume", self._resume_display)
        add("colors", self._set_color_display)
        add("library", lambda c: state.navigate("library:" + self._shown(c).id))
        self.widget.insert_action_group("disp", group)

    def _popup_menu(self, tile: MonitorTile, x: float, y: float) -> None:
        state = self.state
        connector = tile.display.connector
        self._select(connector)
        where = "all displays" if self._mirrored() else connector
        menu = Gio.Menu()
        play = Gio.Menu()
        play.append("Previous wallpaper", f"disp.previous::{connector}")
        play.append("Resume" if self._paused(connector) else "Pause", f"disp.pause::{connector}")
        play.append("Next wallpaper", f"disp.next::{connector}")
        play.append("Random wallpaper", f"disp.random::{connector}")
        menu.append_section(f"On {where}", play)
        other = Gio.Menu()
        if self._key(connector) in state.manual:
            other.append(self._resume.get_label(), f"disp.resume::{connector}")
        if not self._mirrored() and connector != self._color_display():
            other.append("Use for desktop colors", f"disp.colors::{connector}")
        other.append(f"Show “{self._shown(connector).name}” in Library", f"disp.library::{connector}")
        menu.append_section(None, other)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(tile)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        # Unparent later: the chosen action is activated after "closed".
        popover.connect("closed", self._menu_closed)
        self._menu = popover
        popover.popup()

    def _menu_closed(self, popover: Gtk.PopoverMenu) -> None:
        def drop() -> bool:
            popover.unparent()
            if self._menu is popover:
                self._menu = None
            return False

        GLib.idle_add(drop)


def _close_popovers(widget: Gtk.Widget) -> None:
    child = widget.get_first_child()
    while child:
        if isinstance(child, Gtk.Popover):
            child.popdown()
        _close_popovers(child)
        child = child.get_next_sibling()


def create(state) -> Page:
    return DisplaysPage(state)
