"""Settings: app-wide behavior and defaults, a modest runtime log, and about.

Per-wallpaper choices (its still, motion and colors) live with the wallpaper
in the Library. Everything here applies at once; changes that are easy to
regret (removing a folder, moving the download folder) are confirmed or offer
Undo. A search field in the header filters rows; other pages deep-link to a
group with ``settings:<group>`` (library, playback, colors, online, advanced,
log, ...).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk

from .. import ui
from ..catalog import INTERVALS
from ..models import Folder
from . import Page
from .settings_palettes import PalettesDialog
from .settings_widgets import LogView, SchemeDialog

VERSION = "0.2.2"
COMPANION = "0.1.3"

CSS = """
.st-section { border-radius: 18px; transition: background-color 650ms ease-out, box-shadow 650ms ease-out; }
.st-section.st-flash {
  background-color: alpha(var(--accent-bg-color), 0.08);
  box-shadow: 0 0 0 10px alpha(var(--accent-bg-color), 0.08);
}
row.st-row-flash { background-color: alpha(var(--accent-bg-color), 0.20); }
row.st-flashable { transition: background-color 650ms ease-out; }

.st-log-row { padding: 2px 12px; font-family: monospace; font-size: 0.86em; }
.st-log-row.level-warning { background-color: alpha(var(--warning-bg-color), 0.10); }
.st-log-row.level-error { background-color: alpha(var(--error-bg-color), 0.12); }
.st-log-time { opacity: 0.55; font-feature-settings: "tnum"; }
.st-log-cat { font-weight: 700; opacity: 0.75; }
.st-log-cat.cat-schedule { color: var(--accent-color); opacity: 1; }
.st-log-cat.cat-colors { color: var(--accent-color); opacity: 1; }
.st-log-cat.cat-power { color: var(--warning-color); opacity: 1; }
.st-log-cat.cat-renderer { color: var(--error-color); opacity: 1; }
.st-log-row.level-warning .st-log-icon { color: var(--warning-color); }
.st-log-row.level-error .st-log-icon { color: var(--error-color); }

.st-warning-icon { color: var(--warning-color); }
.st-ok-icon { color: var(--success-color); }
.st-status-dot { min-width: 10px; min-height: 10px; border-radius: 999px; background-color: var(--success-color); }
.st-status-dot.stopped { background-color: alpha(currentColor, 0.35); }
.st-value { font-feature-settings: "tnum"; }
button.st-badge-button { padding: 0; min-height: 0; min-width: 0; border-radius: 999px; }
button.st-badge-button:disabled { opacity: 1; }
"""

# Deep-link aliases: other pages and the banner use these after "settings:".
ALIASES = {
    "colors": "colors",
    "color": "colors",
    "theme": "colors",
    "providers": "online",
    "store": "online",
    "wallhaven": "online.key",
    "folders": "library",
    "downloads": "library.download",
    "battery": "playback.battery",
    "power": "playback",
    "renderer": "advanced",
    "video": "advanced",
    "scenes": "advanced",
    "service": "advanced.service",
    "logs": "log",
    "diagnostics": "log",
    "template": "colors.template",
    "scheme": "colors.scheme",
}

INTERVAL_LABELS = [label for _minutes, label in INTERVALS]
COVERED = [("pause", "Pause"), ("stop", "Stop and free memory"), ("play", "Keep playing")]
DECODING = [("auto", "Auto"), ("off", "Off (software)")]
SMOOTHING = [("off", "Off"), ("oversample", "Oversample"), ("linear", "Linear (may ghost)")]
SCALING = [
    ("", "Renderer default"),
    ("fill", "Fill (crop to display)"),
    ("fit", "Fit (whole scene)"),
    ("stretch", "Stretch"),
]
EDGES = [("", "Renderer default"), ("clamp", "Clamp to edge"), ("border", "Border color"), ("repeat", "Repeat")]


@dataclass
class _Entry:
    """A searchable row."""

    row: Gtk.Widget
    keywords: str = ""
    parent: Adw.ExpanderRow | None = None
    available: Callable[[], bool] = lambda: True
    children: list[_Entry] = field(default_factory=list)


@dataclass
class _Group:
    widget: Adw.PreferencesGroup
    title: str
    keywords: str = ""
    entries: list[_Entry] = field(default_factory=list)
    search_only: bool = False


@dataclass
class _Section:
    id: str
    box: Gtk.Box
    groups: list[_Group] = field(default_factory=list)


def _row_text(row: Gtk.Widget) -> str:
    bits = []
    if isinstance(row, Adw.PreferencesRow):
        bits.append(row.get_title())
    if isinstance(row, (Adw.ActionRow, Adw.ExpanderRow)):
        bits.append(row.get_subtitle() or "")
    return " ".join(bits)


class SettingsPage(Page):
    name = "settings"
    title = "Settings"

    def __init__(self, state) -> None:
        super().__init__(state)
        ui.add_css(CSS)
        self._sections: dict[str, _Section] = {}
        self._targets: dict[str, Gtk.Widget] = {}
        self._expanders: list[Adw.ExpanderRow] = []
        self._expanded_before: dict[Adw.ExpanderRow, bool] | None = None
        self._query = ""
        self._syncing = False
        self._dialogs: list[Adw.Dialog] = []
        self._animation: Adw.TimedAnimation | None = None
        self._narrow_setters: list[tuple[GObject.Object, str, object]] = []

        # -- header ------------------------------------------------------------
        self.search = Gtk.SearchEntry(placeholder_text="Search settings", hexpand=True)
        self.search.connect("search-changed", lambda entry: self._set_query(entry.get_text()))
        self.search.connect("stop-search", lambda entry: entry.set_text(""))
        self._title_box = Adw.Clamp(maximum_size=420, child=self.search)

        # -- body ----------------------------------------------------------------
        self._content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=40,
            margin_top=24,
            margin_bottom=40,
            margin_start=14,
            margin_end=14,
        )
        self._clamp = Adw.Clamp(maximum_size=700, tightening_threshold=560, child=self._content)
        self._scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self._scroller.set_child(self._clamp)
        self.search.set_key_capture_widget(self._scroller)

        self._build_library()
        self._build_playback()
        self._build_colors()
        self._build_appearance()
        self._build_online()
        self._build_advanced()
        self._build_log()
        self._build_about()
        self._build_elsewhere()

        empty = Adw.StatusPage(
            icon_name="edit-find-symbolic", title="No matching settings", description="Try another word."
        )
        clear = Gtk.Button(label="Clear search", halign=Gtk.Align.CENTER)
        clear.add_css_class("pill")
        clear.connect("clicked", lambda *_: (self.search.set_text(""), self.search.grab_focus()))
        empty.set_child(clear)
        self._stack = Gtk.Stack()  # instant: filtering should feel immediate
        self._stack.add_named(self._scroller, "page")
        self._stack.add_named(empty, "empty")
        # Narrow windows: drop a few secondary subtitles so inline controls keep their labels.
        bin_ = Adw.BreakpointBin(width_request=360, height_request=300, child=self._stack)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 520sp"))
        for obj, prop, value in self._narrow_setters:
            narrow.add_setter(obj, prop, value)
        bin_.add_breakpoint(narrow)
        self.widget = bin_

        state.connect("changed", self._on_changed)
        self._sync()
        self._apply_filter()

    # -- Page API ------------------------------------------------------------------
    def title_widget(self) -> Gtk.Widget:
        return self._title_box

    def focus_search(self) -> bool:
        self.search.grab_focus()
        return True

    def activate(self, argument: str | None) -> None:
        self._close_dialogs()
        if argument:
            self.scroll_to(argument)

    def demo(self, scene: str) -> None:
        what, _, arg = scene.partition(":")
        self._reset()
        if what == "search":
            self.search.set_text(arg)
            self._set_query(arg)
        elif what == "group":
            self.scroll_to(arg)
        elif what == "advanced":
            for expander in self._expanders:
                expander.set_expanded(True)
            self.scroll_to("advanced", flash=False)
        elif what == "remove-folder":
            index = 0 if arg == "download" else (2 if arg == "missing" else 1)
            self.scroll_to("library", flash=False)
            self._confirm_remove(self.state.folders[index])
        elif what == "download-folder":
            self.scroll_to("library", flash=False)
            self._choose_download_folder()
        elif what == "add-folder":
            self.scroll_to("library", flash=False)
            self._add_folder()
        elif what == "locate":
            self.scroll_to("library", flash=False)
            self._locate(self.state.folders[2])
        elif what == "scheme":
            self._open_scheme_dialog()
        elif what == "palettes":
            self._open_palettes()
        elif what == "palette-edit":
            dialog = self._open_palettes()
            dialog.edit(self.state.palettes("custom")[0])
        elif what == "no-key":
            self._forget_key(toast=False)
            self.scroll_to("online", flash=False)
        elif what == "battery-off":
            # Turn the setting off while on battery: the banner and player bar follow.
            self.scroll_to("playback.battery", flash=False)
            self._battery.set_active(False)
        elif what == "template-missing":
            self.state.set_template_status("missing")
            self._refresh_template()
            self.scroll_to("colors", flash=False)

    def _reset(self) -> None:
        """Screenshot scenes run one after another in one window: start clean."""
        self._close_dialogs()
        self.search.set_text("")
        self._set_query("")
        for expander in self._expanders:
            expander.set_expanded(False)
        self._scroller.get_vadjustment().set_value(0)

    # -- building helpers ------------------------------------------------------------
    def _section(self, sid: str) -> _Section:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.add_css_class("st-section")
        section = _Section(sid, box)
        self._sections[sid] = section
        self._content.append(box)
        return section

    def _group(
        self, section: _Section, title: str = "", description: str = "", keywords: str = "", search_only: bool = False
    ) -> _Group:
        widget = Adw.PreferencesGroup(
            title=GLib.markup_escape_text(title),
            description=GLib.markup_escape_text(description) if description else None,
        )
        group = _Group(widget, title, keywords, search_only=search_only)
        section.groups.append(group)
        section.box.append(widget)
        return group

    def _add(
        self,
        group: _Group,
        row: Gtk.Widget,
        keywords: str = "",
        available: Callable[[], bool] | None = None,
        target: str | None = None,
    ) -> Gtk.Widget:
        entry = _Entry(row, keywords, available=available or (lambda: True))
        group.entries.append(entry)
        group.widget.add(row)
        row.add_css_class("st-flashable")
        if target:
            self._targets[target] = row
        return row

    def _add_child(self, group: _Group, expander: Adw.ExpanderRow, row: Gtk.Widget, keywords: str = "") -> Gtk.Widget:
        parent = next(entry for entry in group.entries if entry.row is expander)
        parent.children.append(_Entry(row, keywords, parent=expander))
        expander.add_row(row)
        return row

    @staticmethod
    def _suffix_button(label: str, callback, *classes: str, tooltip: str | None = None) -> Gtk.Button:
        button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
        for css in classes or ("flat",):
            button.add_css_class(css)
        if tooltip:
            button.set_tooltip_text(tooltip)
        button.connect("clicked", lambda *_: callback())
        return button

    @staticmethod
    def _combo(
        title: str, subtitle: str, options: list[tuple[str, str]], value: str, on_change: Callable[[str], None]
    ) -> Adw.ComboRow:
        row = Adw.ComboRow(title=title, subtitle=subtitle or None)
        row.set_model(Gtk.StringList.new([label for _key, label in options]))
        keys = [key for key, _label in options]
        row.set_selected(keys.index(value))
        row.connect("notify::selected", lambda r, _p: on_change(keys[r.get_selected()]))
        return row

    def _switch(
        self, title: str, subtitle: str, key: str, on_change: Callable[[bool], None] | None = None
    ) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle or None, active=self.state.setting(key))

        def changed(r: Adw.SwitchRow, _p) -> None:
            self.state.set_setting(key, r.get_active())
            if on_change and not self._syncing:
                on_change(r.get_active())

        row.connect("notify::active", changed)
        return row

    @staticmethod
    def _toggles(
        options: list[tuple[str, str, str | None]], value: str, on_change: Callable[[str], None]
    ) -> Adw.ToggleGroup:
        group = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for name, label, tooltip in options:
            toggle = Adw.Toggle(name=name, label=label)
            if tooltip:
                toggle.set_tooltip(tooltip)
            group.add(toggle)
        group.set_active_name(value)
        group.connect("notify::active-name", lambda g, _p: on_change(g.get_active_name()))
        return group

    def _set(self, key: str) -> Callable[[str], None]:
        return lambda value: self.state.set_setting(key, value)

    # ---------------------------------------------------------------------------
    # 1. Library & downloads
    # ---------------------------------------------------------------------------
    def _build_library(self) -> None:
        section = self._section("library")
        self._folder_group = self._group(
            section,
            "Library & downloads",
            "Folders scanned for wallpapers. Nothing is moved.",
            keywords="folders library locations directories scan files",
        )
        self._folder_rows: list[Gtk.Widget] = []
        self._add_folder_row = Adw.ButtonRow(title="Add folder…", start_icon_name="list-add-symbolic")
        self._add_folder_row.connect("activated", lambda *_: self._add_folder())
        self._targets["library.add"] = self._add_folder_row

        more = self._group(section, keywords="library downloads folders")
        workshop = self._switch(
            "Include Wallpaper Engine scenes",
            "From Steam Workshop · never modified",
            "workshop",
            lambda on: self.state.toast("Scenes will show in the Library" if on else "Scenes hidden from the Library"),
        )
        self._add(more, workshop, "steam workshop wallpaper engine scenes scan")
        self._rebuild_folders()

    def _rebuild_folders(self) -> None:
        group = self._folder_group
        for row in self._folder_rows:
            group.widget.remove(row)
        if self._add_folder_row.get_parent():
            group.widget.remove(self._add_folder_row)
        group.entries = []
        self._folder_rows = []
        for index, folder in enumerate(self.state.folders):
            row = self._folder_row(index, folder)
            keywords = f"folder {folder.path} {'missing unplugged locate' if folder.missing else ''}"
            if index == 0:
                keywords += " downloads save store wallhaven motionbgs stills captured destination"
            self._add(group, row, keywords, target="library.download" if index == 0 else None)
            self._folder_rows.append(row)
        if not self.state.folders:
            row = Adw.ActionRow(title="No folders yet", subtitle="Add one to start your library")
            self._add(group, row, "folder empty")
            self._folder_rows.append(row)
        self._add(group, self._add_folder_row, "add folder new location")
        self._apply_filter()

    def _folder_row(self, index: int, folder: Folder) -> Adw.ActionRow:
        row = Adw.ActionRow(title=folder.path, use_markup=False)
        if folder.missing:
            icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            icon.add_css_class("st-warning-icon")
            paused = " · downloads are paused" if index == 0 else ""
            row.set_subtitle(f"Not found · drive may be unplugged{paused}")
        else:
            icon = Gtk.Image.new_from_icon_name("folder-download-symbolic" if index == 0 else "folder-symbolic")
            if folder.scanning:
                row.set_subtitle("Scanning…")
            elif index == 0:
                row.set_subtitle(f"{folder.count} · Downloads and stills are saved here")
            else:
                row.set_subtitle(folder.count)
        row.add_prefix(icon)
        if index == 0 and not folder.missing:
            # The badge is also the way to move downloads to another folder.
            label = ui.pill("Downloads", None, "accent")
            arrow = Gtk.Image.new_from_icon_name("pan-down-symbolic")
            arrow.set_pixel_size(12)
            label.append(arrow)
            badge = Gtk.Button(child=label, valign=Gtk.Align.CENTER)
            badge.add_css_class("flat")
            badge.add_css_class("st-badge-button")
            others = sum(1 for other in self.state.folders if other is not folder and not other.missing)
            badge.set_sensitive(others > 0)
            badge.set_tooltip_text(
                "Downloads and captured stills are saved here · click to choose another folder"
                if others
                else "Downloads and captured stills are saved here"
            )
            badge.connect("clicked", lambda *_: self._choose_download_folder())
            row.add_suffix(badge)
        if folder.scanning:
            row.add_suffix(Adw.Spinner(valign=Gtk.Align.CENTER))
        if folder.missing:
            row.add_suffix(
                self._suffix_button(
                    "Locate…", lambda f=folder: self._locate(f), tooltip="Point to where this folder is now"
                )
            )
        remove = ui.icon_button(
            "list-remove-symbolic", "Remove from library", lambda *_, f=folder: self._confirm_remove(f), "flat"
        )
        remove.set_valign(Gtk.Align.CENTER)
        row.add_suffix(remove)
        return row

    def _add_folder(self) -> None:
        # Unwired: stands in for the folder chooser with a plausible pick.
        known = {folder.path for folder in self.state.folders}
        path = next(
            (p for p in ("~/Downloads/Wallpapers", "~/Pictures/Backgrounds", "~/Videos/Loops") if p not in known), None
        )
        if path is None:
            self.state.toast("All the demo folders are already in the library")
            return
        undo = self.state.add_library_folder(path)
        self.state.toast(f"Added {path} · scanning", undo)

    def _locate(self, folder: Folder) -> None:
        # Unwired: pretends the chooser found the drive under /run/media.
        undo = self.state.locate_folder(folder.path, "/run/media/goober/Archive/wallpapers")
        self.state.toast("Found on “Archive” · 2,310 files", undo)

    def _confirm_remove(self, folder: Folder) -> None:
        folders = self.state.folders
        index = folders.index(folder)
        path = GLib.markup_escape_text(folder.path)
        body = f"<b>{path}</b> leaves the library and every playlist. Files stay on disk."
        if index == 0 and len(folders) > 1:
            body += f"\n\nNew downloads will be saved in <b>{GLib.markup_escape_text(folders[1].path)}</b>."
        elif index == 0:
            body += "\n\nDownloads stop until you add a folder."
        dialog = Adw.AlertDialog(heading="Remove from library?", body=body, body_use_markup=True)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def done(_dialog, response: str) -> None:
            if response != "remove" or folder not in self.state.folders:
                return
            undo = self.state.remove_library_folder(folder.path)
            self.state.toast(f"Removed {folder.path} · files stay on disk", undo)

        dialog.connect("response", done)
        self._present(dialog)

    def _choose_download_folder(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Download folder",
            body="New downloads and captured stills are saved here. Files already saved stay where they are.",
        )
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        folders = self.state.folders
        choice: dict[str, Folder | None] = {"folder": folders[0] if folders else None}
        first_check: Gtk.CheckButton | None = None
        for index, folder in enumerate(folders):
            check = Gtk.CheckButton(active=index == 0, valign=Gtk.Align.CENTER)
            if first_check is None:
                first_check = check
            else:
                check.set_group(first_check)
            row = Adw.ActionRow(
                title=folder.path, use_markup=False, subtitle="Not found" if folder.missing else folder.count
            )
            row.add_prefix(check)
            row.set_activatable_widget(check)
            row.set_sensitive(not folder.missing)
            check.connect("toggled", lambda c, f=folder: c.get_active() and choice.__setitem__("folder", f))
            rows.append(row)
        dialog.set_extra_child(rows)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("use", "Use folder")
        dialog.set_response_appearance("use", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("use")
        dialog.set_close_response("cancel")

        def done(_dialog, response: str) -> None:
            chosen = choice["folder"]
            if response != "use" or chosen is None or chosen is self.state.folders[0]:
                return
            undo = self.state.set_download_folder(chosen.path)
            self.state.toast(f"Downloads now go to {chosen.path}", undo)

        dialog.connect("response", done)
        self._present(dialog)

    # ---------------------------------------------------------------------------
    # 2. Playback & power
    # ---------------------------------------------------------------------------
    def _build_playback(self) -> None:
        section = self._section("playback")
        group = self._group(section, "Playback & power", keywords="playback power play")
        minutes = [m for m, _label in INTERVALS]
        self._interval = Adw.ComboRow(title="Change wallpaper", subtitle="Default for new playlists")
        self._interval.set_model(Gtk.StringList.new(INTERVAL_LABELS))
        self._interval.connect("notify::selected", lambda r, _p: self._set_interval(minutes[r.get_selected()]))
        self._add(group, self._interval, "interval cycle rotate timer every minutes hours change automatically")

        shuffle = self._switch("Shuffle by default", "Random order in new playlists", "shuffle")
        self._add(group, shuffle, "shuffle random order")
        autostart = self._switch("Start with your session", "Run the wallpaper service when you log in", "autostart")
        self._add(group, autostart, "autostart login boot startup service session systemd")

        # An action row (not a SwitchRow) so the "On battery" badge sits before the switch.
        self._battery_row = Adw.ActionRow(title="Stop animations on battery")
        self._battery_pill = ui.pill("On battery", "battery-caution-symbolic", "warning")
        self._battery_pill.set_valign(Gtk.Align.CENTER)
        self._battery_row.add_suffix(self._battery_pill)
        self._battery = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._battery.update_property([Gtk.AccessibleProperty.LABEL], ["Stop animations on battery"])
        self._battery.connect("notify::active", self._on_battery)
        self._battery_row.add_suffix(self._battery)
        self._battery_row.set_activatable_widget(self._battery)
        self._add(
            group,
            self._battery_row,
            "battery power laptop energy save stills animation unplugged",
            target="playback.battery",
        )

        covered = self._combo(
            "When a window covers it",
            "For videos · takes effect on the next one",
            COVERED,
            self.state.setting("covered"),
            self._set("covered"),
        )
        self._add(group, covered, "pause covered hidden fullscreen window maximized stop memory obscured")

    def _set_interval(self, minutes: int) -> None:
        if self._syncing:
            return
        self.state.set_default_interval(minutes)

    def _on_battery(self, switch: Gtk.Switch, _param) -> None:
        if self._syncing:
            return
        self.state.set_stop_on_battery(switch.get_active())
        self._refresh_battery()

    def _refresh_battery(self) -> None:
        state = self.state
        self._battery_pill.set_visible(state.on_battery)
        if state.on_battery and state.stop_on_battery:
            self._battery_row.set_subtitle("On battery now · stills are showing")
        elif state.on_battery:
            self._battery_row.set_subtitle("On battery now · animations keep playing")
        else:
            self._battery_row.set_subtitle("Show stills until you plug in")

    # ---------------------------------------------------------------------------
    # 3. Colors
    # ---------------------------------------------------------------------------
    def _build_colors(self) -> None:
        section = self._section("colors")
        group = self._group(section, "Colors", keywords="colors colors noctalia theme desktop palette")

        desktop = Adw.SwitchRow(
            title="Use wallpaper colors on the desktop",
            subtitle="Noctalia recolors to match each wallpaper",
            active=self.state.desktop_colors,
        )
        desktop.connect("notify::active", self._on_desktop_colors)
        self._add(group, desktop, "adaptive follow noctalia recolor shell bar")

        self._follow_display = Adw.ActionRow(
            title="Colors come from DP-1", subtitle="Choose the display in Displays", activatable=True
        )
        self._follow_display.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self._follow_display.connect("activated", lambda *_: self.state.navigate("displays"))
        self._add(
            group,
            self._follow_display,
            "display monitor source independent which follow",
            available=lambda: self.state.display_mode == "independent",
        )

        self._source = Adw.ActionRow(title="Palette source", use_markup=False)
        self._source_swatches = ui.Swatches([], size=12, overlap=True)
        self._source.add_prefix(self._source_swatches)
        self._reload = ui.icon_button(
            "view-refresh-symbolic", "Reload palette", lambda *_: self._reload_palette(), "flat"
        )
        self._reload.set_valign(Gtk.Align.CENTER)
        self._source.add_suffix(self._reload)
        self._add(group, self._source, "palette source current generated reload refresh")

        self._palettes_row = Adw.ActionRow(title="Palettes", activatable=True)
        manage = self._suffix_button("Manage…", self._open_palettes, tooltip="Browse, apply and edit palettes")
        self._palettes_row.add_suffix(manage)
        self._palettes_row.set_activatable_widget(manage)
        self._add(group, self._palettes_row, "palettes browse custom community built-in edit duplicate catppuccin nord")

        self._template = Adw.ActionRow(title="Noctalia template", subtitle="Lets wallpapers recolor the desktop")
        self._template_status = Gtk.Box(valign=Gtk.Align.CENTER)
        self._template.add_suffix(self._template_status)
        self._template_menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER)
        self._template_menu.add_css_class("flat")
        self._template_menu.set_tooltip_text("Template actions")
        self._template.add_suffix(self._template_menu)
        self._install_template_actions()
        self._add(
            group,
            self._template,
            "template noctalia install reinstall remove repair integration",
            target="colors.template",
        )

        defaults = self._group(
            section,
            "Default colors",
            "For wallpapers that don't choose their own",
            keywords="default colors colors inherited",
        )
        self._scheme = Adw.ActionRow(title="Color scheme", activatable=True, use_markup=False)
        self._scheme_thumb = Gtk.Box(valign=Gtk.Align.CENTER)
        self._scheme.add_prefix(self._scheme_thumb)
        self._scheme_swatches = ui.Swatches([], size=14, overlap=True)
        self._scheme.add_suffix(self._scheme_swatches)
        self._scheme.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self._scheme.connect("activated", lambda *_: self._open_scheme_dialog())
        self._add(
            defaults,
            self._scheme,
            "scheme adaptive tonal spot vibrant soft monochrome rainbow preview",
            target="colors.scheme",
        )

        theme_row = Adw.ActionRow(title="Light or dark")
        theme_row.add_suffix(
            self._toggles(
                [
                    ("auto", "Auto", "Follow the time of day"),
                    ("light", "Light", None),
                    ("dark", "Dark", None),
                    ("keep", "Don't change", "Leave the current mode"),
                ],
                self.state.setting("theme_mode"),
                self._set("theme_mode"),
            )
        )
        self._add(defaults, theme_row, "light dark mode night day theme auto")
        self._color_dependents = [self._source, self._scheme, theme_row, self._follow_display]

    def _refresh_colors(self) -> None:
        state = self.state
        on = state.desktop_colors
        for row in self._color_dependents:
            row.set_sensitive(on)
        source_connector = state.color_connector()
        wallpaper = state.color_wallpaper()
        self._follow_display.set_title(f"Colors come from {source_connector}")
        applied = state.applied_palette()
        if not on:
            text, swatches = "Off · desktop colors stay as they are", []
        elif state.template_status != "working":
            text, swatches = "Template missing · desktop colors can't change", []
        elif applied:
            text, swatches = f"“{applied.name}” palette · until the wallpaper changes", applied.strip(state.dark)
        elif wallpaper.color_mode == "palette":
            text = f"“{wallpaper.palette}” palette · set by “{wallpaper.name}”"
            swatches = state.wallpaper_swatches(wallpaper, state.dark)
        elif wallpaper.color_mode == "keep":
            text, swatches = f"Unchanged · “{wallpaper.name}” keeps your colors", []
        else:
            scheme = wallpaper.scheme or state.default_scheme
            text = f"Generated from “{wallpaper.name}” · {state.scheme_name(scheme)}"
            swatches = state.scheme_swatches(wallpaper, scheme, state.dark)
        self._source.set_subtitle(text)
        # An empty, dashed dot when nothing is being generated.
        self._source_swatches.set_colors(swatches[1:4] if swatches else [])
        counts = {origin: len(state.palettes(origin)) for origin in ("builtin", "community", "custom")}
        self._palettes_row.set_subtitle(
            f"{counts['builtin']} built-in · {counts['community']} community · {counts['custom']} yours"
        )

        # Default scheme, previewed on the wallpaper that is on screen now.
        child = self._scheme_thumb.get_first_child()
        if child:
            self._scheme_thumb.remove(child)
        self._scheme_thumb.append(ui.thumbnail(wallpaper, 56, 32, 6))
        users = sum(1 for w in state.wallpapers if w.color_mode == "adaptive" and not w.scheme)
        self._scheme.set_subtitle(f"{state.scheme_name(state.default_scheme)} · used by {users} wallpapers")
        self._scheme_swatches.set_colors(state.scheme_swatches(wallpaper, state.default_scheme, state.dark)[1:4])

    def _on_desktop_colors(self, row: Adw.SwitchRow, _param) -> None:
        # The window's own colors follow the desktop too (state.desktop_swatches).
        self.state.set_desktop_colors(row.get_active())
        if not self._syncing:
            self._refresh_colors()

    def _reload_palette(self) -> None:
        self._reload.set_sensitive(False)
        spinner = Adw.Spinner(valign=Gtk.Align.CENTER)
        self._source.add_suffix(spinner)

        def done() -> bool:
            self._source.remove(spinner)
            self._reload.set_sensitive(True)
            self.state.toast("Palette reloaded from Noctalia")
            return False

        GLib.timeout_add(800, done)

    def _install_template_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        for name, callback in (("reinstall", self._reinstall_template), ("remove", self._confirm_remove_template)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, c=callback: c())
            group.add_action(action)
        self._template.insert_action_group("tpl", group)
        self._refresh_template()

    def _refresh_template(self) -> None:
        child = self._template_status.get_first_child()
        while child:
            self._template_status.remove(child)
            child = self._template_status.get_first_child()
        status = self.state.template_status
        menu = Gio.Menu()
        if status == "working":
            self._template_status.append(ui.pill("Installed · working", "object-select-symbolic", "success"))
            menu.append("Reinstall", "tpl.reinstall")
            menu.append("Remove…", "tpl.remove")
        elif status == "busy":
            self._template_status.append(Adw.Spinner())
        else:
            self._template_status.append(ui.pill("Not installed", "dialog-warning-symbolic", "warning"))
            install = Gtk.Button(label="Install", valign=Gtk.Align.CENTER, margin_start=6)
            install.add_css_class("suggested-action")
            install.connect("clicked", lambda *_: self._reinstall_template())
            self._template_status.append(install)
        self._template_menu.set_menu_model(menu)
        self._template_menu.set_visible(status == "working")
        if hasattr(self, "_color_dependents"):  # not during construction
            self._refresh_colors()

    def _reinstall_template(self) -> None:
        was = self.state.template_status
        self.state.set_template_status("busy")
        self._refresh_template()

        def done() -> bool:
            self.state.set_template_status("working")
            self._refresh_template()
            self.state.toast("Noctalia template reinstalled" if was == "working" else "Noctalia template installed")
            return False

        GLib.timeout_add(900, done)

    def _confirm_remove_template(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Remove the Noctalia template?",
            body="Wallpapers stop recoloring the desktop until you install it again.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def done(_dialog, response: str) -> None:
            if response == "remove":
                self.state.set_template_status("missing")
                self._refresh_template()
                self.state.toast(
                    "Template removed",
                    lambda: (self.state.set_template_status("working"), self._refresh_template()),
                )

        dialog.connect("response", done)
        self._present(dialog)

    def _open_scheme_dialog(self) -> SchemeDialog:
        before = self.state.default_scheme

        def pick(scheme: str) -> None:
            self.state.set_default_scheme(scheme)
            self._refresh_colors()

        dialog = SchemeDialog(self.state, pick)

        def closed(_dialog) -> None:
            after = self.state.default_scheme
            if after == before:
                return

            def undo() -> None:
                pick(before)

            self.state.toast(f"Default scheme is now {self.state.scheme_name(after)}", undo)

        dialog.connect("closed", closed)
        self._present(dialog)
        return dialog

    def _open_palettes(self) -> PalettesDialog:
        dialog = PalettesDialog(self.state, self._refresh_colors)
        self._present(dialog)
        return dialog

    # ---------------------------------------------------------------------------
    # 4. Online sources
    # ---------------------------------------------------------------------------
    def _build_online(self) -> None:
        section = self._section("online")
        group = self._group(section, "Online sources", keywords="online store providers internet download")

        self._key_row = Adw.ActionRow(title="Wallhaven API key")
        self._key_remove = self._suffix_button("Remove", lambda: self._forget_key(), tooltip="Delete the saved key")
        self._key_row.add_suffix(self._key_remove)
        self._add(group, self._key_row, "wallhaven api key account token password", target="online.key")

        self._key_entry = Adw.PasswordEntryRow(show_apply_button=True)
        self._key_entry.connect("apply", self._save_key)
        self._key_entry.connect("changed", lambda e: e.remove_css_class("error"))
        self._add(group, self._key_entry, "wallhaven api key paste token")

        purity = Adw.ActionRow(title="Content", subtitle="Default for Wallhaven searches")
        self._purity = self._toggles(
            [
                ("sfw", "SFW", "Safe for work only"),
                ("sketchy", "+ Sketchy", "Also show sketchy results"),
                ("nsfw", "+ NSFW", "Also show NSFW results"),
            ],
            self.state.setting("purity"),
            self._set("purity"),
        )
        purity.add_suffix(self._purity)
        self._narrow_setters.append((purity, "subtitle", ""))
        self._add(group, purity, "wallhaven purity nsfw sfw sketchy filter adult safe content")

        motion = Adw.ActionRow(title="MotionBGS", subtitle="No account needed")
        self._add(group, motion, "motionbgs videos live")
        self._refresh_key()

    def _set_key_saved(self, saved: bool) -> None:
        # Shared with the Store, which enables NSFW only with a key.
        self.state.set_wallhaven_key_saved(saved)
        self._refresh_key()

    def _refresh_key(self) -> None:
        saved = self.state.wallhaven_key_saved
        self._key_row.set_subtitle("Saved · only you can read it" if saved else "Not set · needed for NSFW")
        self._key_remove.set_visible(saved)
        self._key_entry.set_title("Replace key" if saved else "Paste your key")
        nsfw = self._purity.get_toggle_by_name("nsfw")
        nsfw.set_enabled(saved)
        nsfw.set_tooltip("Also show NSFW results" if saved else "Needs a Wallhaven API key")
        if not saved and self._purity.get_active_name() == "nsfw":
            self._purity.set_active_name("sketchy")

    def _save_key(self, entry: Adw.PasswordEntryRow) -> None:
        key = entry.get_text().strip()
        if len(key) < 20 or not key.isalnum():
            entry.add_css_class("error")
            self.state.toast("That doesn't look like a Wallhaven key")
            return
        entry.set_text("")
        self._set_key_saved(True)
        self.state.toast("Wallhaven key saved")

    def _forget_key(self, toast: bool = True) -> None:
        self._set_key_saved(False)
        if toast:
            self.state.toast("Wallhaven key removed", lambda: self._set_key_saved(True))

    # ---------------------------------------------------------------------------
    # 5. Appearance (this app only)
    # ---------------------------------------------------------------------------
    def _build_appearance(self) -> None:
        section = self._section("appearance")
        group = self._group(section, "Appearance", keywords="appearance window app look")
        self._follow = Adw.SwitchRow(title="Follow Noctalia for app colors", subtitle="Tints this window only")
        self._follow.connect("notify::active", self._on_follow)
        self._add(group, self._follow, "accent tint app colors colors noctalia palette chrome")

        style_row = Adw.ActionRow(title="Window style")
        self._style_toggle = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for name, label, tip in (
            ("solid", "Solid", "Opaque window"),
            ("translucent", "Translucent", "The desktop shows through; your compositor can blur it"),
            ("frosted", "Frosted", "Your wallpaper, blurred, behind the window — works anywhere"),
        ):
            toggle = Adw.Toggle(name=name, label=label)
            toggle.set_tooltip(tip)
            self._style_toggle.add(toggle)
        self._style_toggle.set_active_name(self.state.window_style)
        self._style_toggle.connect("notify::active-name", self._on_window_style)
        style_row.add_suffix(self._style_toggle)
        self._style_row = style_row
        self._add(group, style_row, "window style glass frosted blur translucent transparent solid")

        def set_background(percent: int) -> None:
            self.state.set_background_opacity(percent / 100)

        def set_panels(percent: int) -> None:
            self.state.set_panel_opacity(percent / 100)

        def set_frost(percent: int) -> None:
            self.state.set_frost(percent / 100)

        self._background_row, self._background_scale = self._dial(
            "Background opacity", "The page behind the grid and lists", set_background
        )
        glass_only = lambda: self.state.window_style != "solid"  # noqa: E731
        self._add(
            group,
            self._background_row,
            "background opacity transparency translucent see-through window glass",
            available=glass_only,
        )
        self._panel_row, self._panel_scale = self._dial(
            "Panel opacity", "Sidebar, header, player bar, details and cards", set_panels
        )
        self._add(
            group,
            self._panel_row,
            "panel opacity transparency translucent see-through cards elements glass",
            available=glass_only,
        )
        self._frost_row, self._frost_scale = self._dial(
            "Frost", "How strongly the wallpaper behind is blurred", set_frost, round(self.state.frost * 100)
        )
        self._add(
            group,
            self._frost_row,
            "frost blur frosted glass strength soft clear",
            available=lambda: self.state.window_style == "frosted",
        )

        blur = Adw.ActionRow(title="Blur behind the window", subtitle="Comes from niri: add a background-effect rule")
        copy = Gtk.Button(label="Copy rule", valign=Gtk.Align.CENTER)
        copy.set_tooltip_text("Copy a niri window rule that blurs behind Wall-in-One")
        copy.connect("clicked", self._copy_blur_rule)
        blur.add_suffix(copy)
        self._blur_row = blur
        self._add(
            group,
            blur,
            "blur niri compositor frosted glass background-effect",
            available=lambda: self.state.window_style == "translucent",
        )
        self._sync_window_style()

    def _on_window_style(self, group: Adw.ToggleGroup, _param) -> None:
        if self._syncing:
            return
        self.state.set_window_style(group.get_active_name())
        self._sync_window_style()

    def _sync_window_style(self) -> None:
        style = self.state.window_style
        self._style_row.set_subtitle(
            {
                "solid": "Opaque",
                "translucent": "The desktop shows through",
                "frosted": "Your wallpaper, blurred, behind the window",
            }[style]
        )
        # Each glass style remembers its own two opacities.
        self._syncing = True
        for row, scale, value in (
            (self._background_row, self._background_scale, self.state.background_alpha),
            (self._panel_row, self._panel_scale, self.state.panel_alpha),
        ):
            row.set_visible(style != "solid")
            scale.set_value(round(value * 100))
        self._syncing = False
        self._frost_row.set_visible(style == "frosted")
        self._blur_row.set_visible(style == "translucent")

    def _dial(
        self, title: str, subtitle: str, on_move: Callable[[int], None], percent: int = 100
    ) -> tuple[Adw.ActionRow, Gtk.Scale]:
        """A 0–100% slider row that applies live while dragging."""
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        scale.set_value(percent)
        scale.set_draw_value(False)
        scale.set_size_request(150, -1)
        self._narrow_setters.append((scale, "width-request", 110))
        scale.set_valign(Gtk.Align.CENTER)
        scale.update_property([Gtk.AccessibleProperty.LABEL], [title])
        value = Gtk.Label(label=f"{percent}%", width_chars=4, xalign=1)
        value.add_css_class("st-value")
        value.add_css_class("dimmed")

        def moved(widget: Gtk.Scale) -> None:
            stepped = round(widget.get_value() / 5) * 5
            value.set_label(f"{stepped}%")
            if not self._syncing:
                on_move(stepped)

        scale.connect("value-changed", moved)
        row.add_suffix(scale)
        row.add_suffix(value)
        return row, scale

    def _copy_blur_rule(self, _button: Gtk.Button) -> None:
        from .. import glass

        self.widget.get_clipboard().set(glass.NIRI_RULE)
        self.state.toast("niri rule copied — paste it into your niri config")

    def _on_follow(self, row: Adw.SwitchRow, _param) -> None:
        if self._syncing:
            return
        self.state.set_follow_noctalia_colors(row.get_active())

    # ---------------------------------------------------------------------------
    # 6. Advanced
    # ---------------------------------------------------------------------------
    def _build_advanced(self) -> None:
        section = self._section("advanced")
        group = self._group(section, "Advanced", "The defaults suit most systems", keywords="advanced renderer expert")

        video = Adw.ExpanderRow(title="Video", subtitle="Decoding, smoothing and sound")
        video.add_prefix(Gtk.Image.new_from_icon_name("video-x-generic-symbolic"))
        self._expanders.append(video)
        self._add(group, video, "video mpv mpvpaper")
        self._add_child(
            group,
            video,
            self._combo(
                "Hardware decoding",
                "Turn off if videos glitch",
                DECODING,
                self.state.setting("decoding"),
                self._set("decoding"),
            ),
            "hardware decoding gpu vaapi cpu driver artifacts glitch",
        )
        self._add_child(
            group,
            video,
            self._combo(
                "Smooth motion",
                "For low-frame-rate videos",
                SMOOTHING,
                self.state.setting("smoothing"),
                self._set("smoothing"),
            ),
            "smooth motion interpolation oversample linear frame rate judder",
        )
        sound = self._switch("Sound", "Videos start muted when off", "sound", lambda on: self._volume.set_sensitive(on))
        self._add_child(group, video, sound, "sound audio mute muted")
        self._volume = Adw.ActionRow(title="Volume")
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        scale.set_value(self.state.setting("volume"))
        scale.set_draw_value(False)
        scale.set_size_request(150, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.update_property([Gtk.AccessibleProperty.LABEL], ["Volume"])
        volume_value = Gtk.Label(label=f"{self.state.setting('volume')}%", width_chars=4, xalign=1)
        volume_value.add_css_class("st-value")
        volume_value.add_css_class("dimmed")
        scale.connect(
            "value-changed",
            lambda s: (
                self.state.set_setting("volume", round(s.get_value())),
                volume_value.set_label(f"{round(s.get_value())}%"),
            ),
        )
        self._volume.add_suffix(scale)
        self._volume.add_suffix(volume_value)
        self._volume.set_sensitive(self.state.setting("sound"))
        self._add_child(group, video, self._volume, "volume sound audio loud")

        scenes = Adw.ExpanderRow(title="Scenes", subtitle="Wallpaper Engine renderer")
        scenes.add_prefix(Gtk.Image.new_from_icon_name("applications-games-symbolic"))
        self._expanders.append(scenes)
        self._add(group, scenes, "scenes wallpaper engine linux-wallpaperengine steam")
        run = self._switch(
            "Let Wall-in-One run scenes",
            "Starts linux-wallpaperengine when needed",
            "run_scenes",
            lambda on: [row.set_sensitive(on) for row in scene_rows],
        )
        self._add_child(group, scenes, run, "run own renderer linux-wallpaperengine start")
        fps = Adw.ActionRow(title="Frame rate", subtitle="Lower saves power")
        fps.add_suffix(
            self._toggles(
                [(v, v, f"{v} fps") for v in ("15", "24", "30", "60")], self.state.setting("fps"), self._set("fps")
            )
        )
        scene_rows: list[Gtk.Widget] = [fps]
        self._add_child(group, scenes, fps, "frame rate fps power")
        scaling = self._combo("Scaling", "", SCALING, self.state.setting("scaling"), self._set("scaling"))
        scene_rows.append(scaling)
        self._add_child(group, scenes, scaling, "scaling fit fill stretch crop aspect")
        edges = self._combo("Texture edges", "", EDGES, self.state.setting("edges"), self._set("edges"))
        scene_rows.append(edges)
        self._add_child(group, scenes, edges, "texture edges clamp border repeat sampling")
        renderer = Adw.ActionRow(title="Renderer", subtitle="linux-wallpaperengine found")
        ok = Gtk.Image.new_from_icon_name("object-select-symbolic")
        ok.add_css_class("st-ok-icon")
        renderer.add_suffix(ok)
        self._add_child(group, scenes, renderer, "renderer installed found available")

        self._service = Adw.ActionRow(title="Wallpaper service")
        self._service_dot = Gtk.Box(valign=Gtk.Align.CENTER)
        self._service_dot.add_css_class("st-status-dot")
        self._service.add_prefix(self._service_dot)
        self._service_button = self._suffix_button("Restart", self._restart_service)
        self._service.add_suffix(self._service_button)
        self._add(
            group,
            self._service,
            "service daemon runtime restart start running version status",
            target="advanced.service",
        )

    def _refresh_service(self, busy: bool = False) -> None:
        running = self.state.service_running
        if busy:
            self._service.set_subtitle("Restarting…")
        else:
            self._service.set_subtitle(f"Running · {VERSION}" if running else "Stopped · wallpapers won't change")
        if running:
            self._service_dot.remove_css_class("stopped")
        else:
            self._service_dot.add_css_class("stopped")
        self._service_button.set_label("Restart" if running else "Start")
        self._service_button.set_sensitive(not busy)
        if running:
            self._service_button.remove_css_class("suggested-action")
            self._service_button.add_css_class("flat")
        else:
            self._service_button.remove_css_class("flat")
            self._service_button.add_css_class("suggested-action")

    def _restart_service(self) -> None:
        if not self.state.service_running:
            self.state.set_service_running(True)
            self.state.toast("Wallpaper service started")
            return
        self._refresh_service(busy=True)

        def done() -> bool:
            self._refresh_service()
            self.state.toast("Wallpaper service restarted")
            return False

        GLib.timeout_add(1000, done)

    # ---------------------------------------------------------------------------
    # 7. Runtime log
    # ---------------------------------------------------------------------------
    def _build_log(self) -> None:
        section = self._section("log")
        self.log = LogView(self.state.runtime_log())
        problems = self.log.problems
        description = f"Today · {len(self.log)} events" + (f" · {problems} problems" if problems else "")
        group = self._group(
            section, "Runtime log", description, keywords="log runtime diagnostics debug errors events problems history"
        )
        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        copy = Gtk.Button(child=Adw.ButtonContent(icon_name="edit-copy-symbolic", label="Copy"))
        copy.add_css_class("flat")
        copy.set_tooltip_text("Copy the log to the clipboard")
        copy.connect("clicked", lambda *_: self._copy_log())
        folder = ui.icon_button(
            "folder-open-symbolic",
            "Open log folder",
            lambda *_: self.state.toast("Would open ~/.local/state/wall-in-one in Files"),
            "flat",
        )
        buttons.append(copy)
        buttons.append(folder)
        group.widget.set_header_suffix(buttons)

        log_row = Adw.PreferencesRow(activatable=False, focusable=False)
        log_row.set_title("Runtime log")
        log_row.set_child(self.log)
        self._add(group, log_row, "log copy events")
        hide = self._switch(
            "Hide file paths", "When showing and copying", "hide_paths", lambda on: self.log.set_hide_paths(on)
        )
        self._add(group, hide, "privacy paths hide redact home username")

    def _copy_log(self) -> None:
        text = self.log.text()
        clipboard = self.widget.get_clipboard()
        clipboard.set_content(Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_STRING, text)))
        lines = text.count("\n") + 1
        self.state.toast(f"Copied {lines} lines" + (" · file paths hidden" if self.state.setting("hide_paths") else ""))

    # ---------------------------------------------------------------------------
    # 8. About
    # ---------------------------------------------------------------------------
    def _build_about(self) -> None:
        section = self._section("about")
        group = self._group(section, "About", keywords="about version help")
        app = Adw.ActionRow(title="Wall-in-One", subtitle=f"Version {VERSION}", activatable=True)
        app.add_prefix(Gtk.Image.new_from_icon_name("help-about-symbolic"))
        app.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        app.connect("activated", lambda *_: self.widget.activate_action("win.about", None))
        self._add(group, app, "version about credits license")
        companion = Adw.ActionRow(title="Noctalia companion", subtitle=f"Version {COMPANION}")
        companion.add_prefix(Gtk.Image.new_from_icon_name("network-workgroup-symbolic"))
        badge = ui.pill("Connected", "object-select-symbolic", "success")
        badge.set_valign(Gtk.Align.CENTER)
        companion.add_suffix(badge)
        self._add(group, companion, "noctalia companion plugin bar widget connected")
        keys = Adw.ActionRow(title="Keyboard shortcuts", activatable=True)
        keys.add_prefix(Gtk.Image.new_from_icon_name("preferences-desktop-keyboard-shortcuts-symbolic"))
        keys.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        keys.connect("activated", lambda *_: self.widget.activate_action("win.shortcuts", None))
        self._add(group, keys, "keyboard shortcuts keys hotkeys accelerators")

    # ---------------------------------------------------------------------------
    # Search-only: settings that moved to another page
    # ---------------------------------------------------------------------------
    def _build_elsewhere(self) -> None:
        section = self._section("elsewhere")
        group = self._group(section, "On other pages", search_only=True)
        for title, page, label, keywords in (
            (
                "Same wallpaper on every display",
                "displays",
                "Displays",
                "display displays monitor screen mirror mirrored independent output connector same",
            ),
            (
                "Which display sets the colors",
                "displays",
                "Displays",
                "colors colors follow display source theme monitor",
            ),
            (
                "A wallpaper's own colors",
                "library",
                "Library",
                "palette scheme per wallpaper pairing colors colors light dark own",
            ),
            (
                "Animation and sound for one wallpaper",
                "library",
                "Library",
                "animate animation motion still sound mute one wallpaper",
            ),
            ("When playlists play", "schedule", "Schedule", "schedule time day night calendar rule week weekend when"),
        ):
            row = Adw.ActionRow(title=title, subtitle=f"In {label}", activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda *_, p=page: self.state.navigate(p))
            self._add(group, row, keywords)

    # ---------------------------------------------------------------------------
    # Search
    # ---------------------------------------------------------------------------
    def _set_query(self, text: str) -> None:
        query = text.strip().lower()
        if query == self._query:
            return
        if query and not self._query:
            self._expanded_before = {e: e.get_expanded() for e in self._expanders}
        elif not query and self._expanded_before is not None:
            for expander, expanded in self._expanded_before.items():
                expander.set_expanded(expanded)
            self._expanded_before = None
        self._query = query
        self._apply_filter()
        if query:
            adjustment = self._scroller.get_vadjustment()
            adjustment.set_value(0)
            # Rows hide on the next layout, which can leave the view part-way
            # down a heading; reset again once that layout has run.
            GLib.idle_add(lambda: (adjustment.set_value(0), False)[1])

    def _apply_filter(self) -> None:
        if not hasattr(self, "_stack"):
            return
        words = self._query.split()
        anything = False
        for section in self._sections.values():
            section_visible = False
            for group in section.groups:
                context = f"{group.title} {group.keywords}".lower()
                group_visible = False
                for entry in group.entries:
                    own = f"{context} {_row_text(entry.row)} {entry.keywords}".lower()
                    matches = all(word in own for word in words)
                    visible = entry.available() and (matches or not words)
                    if entry.children:
                        hits = 0
                        for child in entry.children:
                            text = f"{own} {_row_text(child.row)} {child.keywords}".lower()
                            child_visible = not words or all(word in text for word in words)
                            child.row.set_visible(child_visible)
                            hits += child_visible
                        visible = entry.available() and (not words or matches or hits > 0)
                        if words and hits:
                            entry.row.set_expanded(True)
                    if group.search_only and not words:
                        visible = False
                    entry.row.set_visible(visible)
                    group_visible |= visible
                group.widget.set_visible(group_visible)
                section_visible |= group_visible
            section.box.set_visible(section_visible)
            anything |= section_visible
        self._stack.set_visible_child_name("page" if anything else "empty")

    # ---------------------------------------------------------------------------
    # Deep links
    # ---------------------------------------------------------------------------
    def scroll_to(self, key: str, flash: bool = True) -> None:
        key = ALIASES.get(key, key)
        if self._query:
            self.search.set_text("")
            self._set_query("")
        target = self._targets.get(key)
        if target is None and key.partition(".")[0] in self._sections:
            target = self._sections[key.partition(".")[0]].box
        if target is None:
            return
        attempts = [0]

        def run() -> bool:
            attempts[0] += 1
            ready = target.get_mapped() and self._clamp.get_height() > 1
            if not ready and attempts[0] < 60:
                return True
            ok, point = target.compute_point(self._clamp, Graphene.Point().init(0, 0))
            if not ok:
                return False
            adjustment = self._scroller.get_vadjustment()
            # Rows sit a little lower than their section so the group title stays visible.
            offset = 64 if isinstance(target, Gtk.ListBoxRow) else 14
            destination = max(0.0, min(point.y - offset, adjustment.get_upper() - adjustment.get_page_size()))
            if self._animation:
                self._animation.skip()
            self._animation = Adw.TimedAnimation(
                widget=self._scroller,
                value_from=adjustment.get_value(),
                value_to=destination,
                duration=320,
                target=Adw.CallbackAnimationTarget.new(adjustment.set_value),
            )
            self._animation.play()
            if flash:
                self._flash(target)
            return False

        GLib.timeout_add(30, run)

    @staticmethod
    def _flash(widget: Gtk.Widget) -> None:
        css = "st-row-flash" if isinstance(widget, Gtk.ListBoxRow) else "st-flash"
        widget.add_css_class(css)
        GLib.timeout_add(1500, lambda: (widget.remove_css_class(css), False)[1])

    # ---------------------------------------------------------------------------
    # State
    # ---------------------------------------------------------------------------
    def _present(self, dialog: Adw.Dialog) -> None:
        self._dialogs.append(dialog)
        dialog.connect("closed", lambda d: d in self._dialogs and self._dialogs.remove(d))
        dialog.present(self.widget)

    def _close_dialogs(self) -> None:
        for dialog in list(self._dialogs):
            dialog.force_close()
        self._dialogs.clear()

    def _sync(self) -> None:
        """Reflect shared state in the widgets without feeding changes back."""
        self._syncing = True
        state = self.state
        minutes = [m for m, _label in INTERVALS]
        if state.default_interval in minutes:
            self._interval.set_selected(minutes.index(state.default_interval))
        self._battery.set_active(state.stop_on_battery)
        self._follow.set_active(state.follow_noctalia_colors)
        if state.live_colors():
            self._follow.set_subtitle(f"{state.live.describe()} · tints this window only")
        elif state.live is not None and state.live.found:
            self._follow.set_subtitle("Simulated from the demo's wallpapers (☰ → Real Noctalia colors)")
        else:
            self._follow.set_subtitle("Tints this window only")
        self._syncing = False
        self._refresh_battery()
        self._refresh_colors()
        self._refresh_service()

    def _on_changed(self, _state, topic: str) -> None:
        if topic == "folders":
            self._rebuild_folders()
            return
        if topic == "appearance":
            self._syncing = True
            self._style_toggle.set_active_name(self.state.window_style)
            self._syncing = False
            self._sync_window_style()
            return
        if topic in ("settings", "system", "theme", "now", "library", "displays"):
            self._sync()
            self._apply_filter()  # some rows depend on state (e.g. independent displays)


def create(state) -> Page:
    return SettingsPage(state)
