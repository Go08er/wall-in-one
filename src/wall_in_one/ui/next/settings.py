"""Settings: the app's settings, library folders, the Wallhaven key, colours and palettes.

The redesign's page (a search field in the header filters rows; other pages
deep-link with ``settings:<section>``) over the classic Settings page's
settings, each row bound to the same ``settings.toml`` key. Everything here
goes through `SettingsAccess` (``state.settings_access``); the window's own
look goes through the `AppState` (``ui.toml``), and three parts are handed in
by whoever builds the page: the rows choosing how the next start opens
(`LaunchRows`, which needs the window's ``ui.toml`` keeper), Tidy up (the
classic `TidySection`, which needs the application) and the session's log
(`log_section.LogSection`, over the application's log). The prototype passes
none of them.

**What a row shows.** A row saves when the user changes it and then shows
what the adapter reports, on the ``settings`` topic: the saved value, or,
after a failed save, the value that was durable before it. Showing a value
never saves it (rows are filled under ``_loading``), so opening, idling on
and leaving the page writes nothing.

**Read-only.** While settings.toml has keys this version doesn't know, every
row that writes it is off and the page says why; the Wallhaven key, the
window's look, the next start's interface and Tidy up write other files and
stay on, as in classic Settings.

**Noctalia's template.** The Colours group shows whether Noctalia's settings
register Wall-in-One's palette template (installed, missing, needing repair,
or something only a hand can fix, with the reason), read without writing.
Its Install or Repair button is the only thing here that writes Noctalia's
settings, and only when pressed; it never renders. While the template is
installed but not rendered yet, a separate Render now row asks Noctalia to
re-render all of its templates, and says so before it is pressed.

**Folders.** The first folder receives downloads and generated stills; another
one is made first with its download button. There is no separate download
folder (owner decision 4) and no "Locate" for a missing folder: it is said to
be missing and comes back when its drive does. A folder found on this
machine is only ever suggested, never added (a suggestion is not consent).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gio, GLib, Graphene, Gtk

from wall_in_one.ui.next import widgets
from wall_in_one.ui.next.catalog import CLASSIC_HINT
from wall_in_one.ui.next.page import Page
from wall_in_one.ui.next.settings_palettes import PalettesDialog
from wall_in_one.ui.next.settings_state import (
    DISPLAY_MODES,
    FPS_RANGE,
    INTERPOLATION,
    INTERVAL_RANGE,
    KEY_REFUSED,
    KEY_SAVED,
    OPACITY_RANGE,
    SCENE_CLAMP,
    SCENE_SCALING,
    SCHEMES,
    VOLUME_RANGE,
    WHEN_HIDDEN,
    Choices,
    Folder,
    SettingsAccess,
)
from wall_in_one.ui.next.state import AppState

if TYPE_CHECKING:
    from wall_in_one.ui.launch_rows import LaunchRows

CSS: Final = """
.st-section {
  border-radius: 18px;
  transition: background-color 650ms ease-out, box-shadow 650ms ease-out;
}
.st-section.st-flash {
  background-color: alpha(var(--accent-bg-color), 0.08);
  box-shadow: 0 0 0 10px alpha(var(--accent-bg-color), 0.08);
}
row.st-row-flash { background-color: alpha(var(--accent-bg-color), 0.20); }
row.st-flashable { transition: background-color 650ms ease-out; }
.st-warning-icon { color: var(--warning-color); }
.st-ok-icon { color: var(--success-color); }
.st-value { font-feature-settings: "tnum"; }
"""

#: Deep-link aliases: other pages and the window use these after "settings:".
ALIASES: Final = {
    "color": "colors",
    "colours": "colors",
    "theme": "colors",
    "scheme": "colors.scheme",
    "providers": "online",
    "store": "online",
    "wallhaven": "online.key",
    "folders": "library",
    "downloads": "library",
    "battery": "playback.battery",
    "power": "playback",
    "display": "displays",
    "renderer": "advanced",
    "video": "advanced",
    "scenes": "advanced",
    "look": "appearance",
    "interface": "appearance.interface",
    "tidy-up": "tidy",
    "logs": "log",
    "diagnostics": "log",
}
STYLES: Final = (
    ("solid", "Solid", "Opaque window"),
    ("translucent", "Translucent", "The desktop shows through; your compositor can blur it"),
    ("frosted", "Frosted", "Your wallpaper, blurred, behind the window"),
)
STYLE_NOTES: Final = {
    "solid": "Opaque",
    "translucent": "The desktop shows through",
    "frosted": "Your wallpaper, blurred, behind the window",
}

_css_added = False


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


@dataclass
class _Section:
    id: str
    box: Gtk.Box
    groups: list[_Group] = field(default_factory=list)


@dataclass(frozen=True)
class _Binding:
    """A row bound to one settings key."""

    key: str
    row: Adw.PreferencesRow
    choices: Choices = ()


def _row_text(row: Gtk.Widget) -> str:
    bits = []
    if isinstance(row, Adw.PreferencesRow):
        bits.append(row.get_title())
    if isinstance(row, (Adw.ActionRow, Adw.ExpanderRow)):
        bits.append(row.get_subtitle() or "")
    if isinstance(row, Adw.PreferencesGroup):
        bits.append(row.get_title() or "")
    return " ".join(bits)


class SettingsPage(Page):
    """The Settings page over `SettingsAccess`."""

    name = "settings"
    title = "Settings"

    def __init__(
        self,
        state: AppState,
        *,
        launch: LaunchRows | None = None,
        tidy: Gtk.Widget | None = None,
        log: Gtk.Widget | None = None,
    ) -> None:
        super().__init__(state)
        global _css_added
        if not _css_added:
            widgets.add_css(CSS)
            _css_added = True
        self.launch = launch
        self.interface_row = launch.interface if launch is not None else None
        self.gpu_row = launch.gpu if launch is not None else None
        self.tidy = tidy
        self.log = log
        self._dialogs: list[Adw.Dialog] = []
        self._sections: dict[str, _Section] = {}
        self._targets: dict[str, Gtk.Widget] = {}
        self._expanders: list[Adw.ExpanderRow] = []
        self._expanded_before: dict[Adw.ExpanderRow, bool] | None = None
        self._query = ""
        self._loading = False
        self._bindings: list[_Binding] = []
        #: Rows and buttons that write settings.toml: off while it is read-only.
        self._writers: list[Gtk.Widget] = []
        self._folders_shown: tuple[object, ...] | None = None
        self._folder_rows: list[Gtk.Widget] = []
        self._theme_connectors: tuple[tuple[str, bool], ...] | None = None
        self._animation: Adw.TimedAnimation | None = None

        access = state.settings_access
        self.access: SettingsAccess | None = access
        self.search = Gtk.SearchEntry(placeholder_text="Search settings", hexpand=True)
        self._title_box = Adw.Clamp(maximum_size=420, child=self.search)
        if access is None:
            self.widget = self._without_access()
            return

        self.search.connect("search-changed", lambda entry: self._set_query(entry.get_text()))
        self.search.connect("stop-search", lambda entry: entry.set_text(""))
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

        self._build_notice()
        self._build_library(access)
        self._build_playback(access)
        self._build_displays(access)
        self._build_colors(access)
        self._build_appearance(access)
        self._build_online(access)
        self._build_advanced(access)
        self._build_tidy()
        self._build_log()
        self._build_about()

        empty = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="No matching settings",
            description="Try another word.",
        )
        clear = Gtk.Button(label="Clear search", halign=Gtk.Align.CENTER)
        clear.add_css_class("pill")
        clear.connect("clicked", lambda *_: self._clear_search())
        empty.set_child(clear)
        self._stack = Gtk.Stack()  # instant: filtering should feel immediate
        self._stack.add_named(self._scroller, "page")
        self._stack.add_named(empty, "empty")
        self.widget = Adw.BreakpointBin(width_request=360, height_request=300, child=self._stack)

        state.connect("changed", self._on_changed)
        self._sync()
        self._apply_filter()

    def _without_access(self) -> Gtk.Widget:
        """An adapter without settings: say so, and still offer the next start's interface."""
        status = Adw.StatusPage(
            icon_name="emblem-system-symbolic",
            title="Settings",
            description=f"Settings is not in the new interface yet. {CLASSIC_HINT}",
        )
        if self.launch is not None:
            group = Adw.PreferencesGroup()
            for row in self.launch.rows:
                group.add(row)
            status.set_child(Adw.Clamp(child=group, maximum_size=480))
        return status

    def _clear_search(self) -> None:
        self.search.set_text("")
        self.search.grab_focus()

    # -- Page API ----------------------------------------------------------------------
    def title_widget(self) -> Gtk.Widget:
        return self._title_box if self.access is not None else self._title

    def focus_search(self) -> bool:
        if self.access is None:
            return False
        self.search.grab_focus()
        return True

    def activate(self, argument: str | None) -> None:
        self._close_dialogs()
        if self.access is None or not argument:
            return
        if argument in ("palettes", "schemes"):
            self.open_palettes(argument)
        else:
            self.scroll_to(argument)

    def demo(self, scene: str) -> None:
        """The prototype's screenshot and smoke hook."""
        what, _, argument = scene.partition(":")
        self._reset()
        if self.access is None:
            return
        if what == "search":
            self.search.set_text(argument)
            self._set_query(argument)
        elif what == "group":
            self.scroll_to(argument)
        elif what == "advanced":
            for expander in self._expanders:
                expander.set_expanded(True)
            self.scroll_to("advanced", flash=False)
        elif what == "remove-folder":
            folders = self.access.folder_list()
            if folders:
                self.scroll_to("library", flash=False)
                self.confirm_remove(folders[0].path)
        elif what in ("palettes", "schemes"):
            self.open_palettes(what)
        elif what == "no-key":
            self.scroll_to("online", flash=False)

    def _reset(self) -> None:
        """Screenshot scenes run one after another in one window: start clean."""
        self._close_dialogs()
        if self.access is None:
            return
        self.search.set_text("")
        self._set_query("")
        for expander in self._expanders:
            expander.set_expanded(False)
        self._scroller.get_vadjustment().set_value(0)

    # -- building helpers ----------------------------------------------------------------
    def _section(self, sid: str) -> _Section:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.add_css_class("st-section")
        section = _Section(sid, box)
        self._sections[sid] = section
        self._content.append(box)
        return section

    def _group(
        self, section: _Section, title: str = "", description: str = "", keywords: str = ""
    ) -> _Group:
        widget = Adw.PreferencesGroup(
            title=GLib.markup_escape_text(title),
            description=GLib.markup_escape_text(description) if description else None,
        )
        group = _Group(widget, title, keywords)
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

    def _add_child(
        self, group: _Group, expander: Adw.ExpanderRow, row: Gtk.Widget, keywords: str = ""
    ) -> Gtk.Widget:
        parent = next(entry for entry in group.entries if entry.row is expander)
        parent.children.append(_Entry(row, keywords, parent=expander))
        expander.add_row(row)
        return row

    def _switch(self, title: str, subtitle: str, key: str) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.connect("notify::active", lambda r, _p: self._save(key, r.get_active()))
        self._bind(key, row)
        return row

    def _spin(
        self,
        title: str,
        subtitle: str,
        key: str,
        limits: tuple[float, float],
        step: float,
        *,
        digits: int = 0,
    ) -> Adw.SpinRow:
        low, high = limits
        adjustment = Gtk.Adjustment(
            lower=low, upper=high, step_increment=step, page_increment=step * 10, value=low
        )
        row = Adw.SpinRow(title=title, subtitle=subtitle, adjustment=adjustment, digits=digits)

        def changed(spin: Adw.SpinRow, _param: object) -> None:
            value = spin.get_value()
            self._save(key, round(value, digits) if digits else int(value))

        row.connect("notify::value", changed)
        self._bind(key, row)
        return row

    def _combo(self, title: str, subtitle: str, key: str, choices: Choices) -> Adw.ComboRow:
        row = Adw.ComboRow(
            title=title,
            subtitle=subtitle,
            model=Gtk.StringList.new([label for _value, label in choices]),
        )

        def changed(combo: Adw.ComboRow, _param: object) -> None:
            index = combo.get_selected()
            if 0 <= index < len(choices):
                self._save(key, choices[index][0])

        row.connect("notify::selected", changed)
        self._bind(key, row, choices)
        return row

    def _bind(self, key: str, row: Adw.PreferencesRow, choices: Choices = ()) -> None:
        self._bindings.append(_Binding(key, row, choices))
        self._writers.append(row)

    def _save(self, key: str, value: Any) -> None:
        if self._loading or self.access is None:
            return
        self.access.change_settings(**{key: value})

    @staticmethod
    def _button(label: str, callback: Callable[[], None], tooltip: str = "") -> Gtk.Button:
        button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        if tooltip:
            button.set_tooltip_text(tooltip)
        button.connect("clicked", lambda *_: callback())
        return button

    # -- read-only -----------------------------------------------------------------------
    def _build_notice(self) -> None:
        section = self._section("notice")
        group = self._group(section)
        self._notice = Adw.ActionRow(title="Settings can't be changed here", use_markup=False)
        self._notice.set_subtitle_lines(0)
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.add_css_class("st-warning-icon")
        self._notice.add_prefix(icon)
        self._add(
            group,
            self._notice,
            "read-only newer version unknown keys",
            available=lambda: bool(self.access is not None and self.access.settings_blocked()),
        )

    # -- 1. Library ----------------------------------------------------------------------
    def _build_library(self, access: SettingsAccess) -> None:
        section = self._section("library")
        self._folder_group = self._group(
            section,
            "Library",
            "Folders scanned for wallpapers. The first one receives downloads and generated "
            "stills. Nothing is moved.",
            keywords="folders library locations directories scan files downloads stills",
        )
        self._add_folder_row = Adw.ButtonRow(
            title="Add folder…", start_icon_name="folder-new-symbolic"
        )
        self._add_folder_row.connect("activated", lambda *_: self._choose_folder())
        self._writers.append(self._add_folder_row)
        self._targets["library.add"] = self._add_folder_row
        more = self._group(section, keywords="library folders")
        workshop = self._switch(
            "Scan Wallpaper Engine library",
            "Show installed Steam Workshop wallpapers; Wall-in-One never modifies them",
            "scan_workshop",
        )
        self._add(more, workshop, "steam workshop wallpaper engine scenes scan")

    def _rebuild_folders(self, access: SettingsAccess, blocked: bool) -> None:
        folders = tuple(access.folder_list())
        suggestion = access.suggested_folder() if not folders else ""
        shown = (folders, suggestion, blocked)
        if shown == self._folders_shown:
            return
        self._folders_shown = shown
        group = self._folder_group
        for row in self._folder_rows:
            group.widget.remove(row)
        if self._add_folder_row.get_parent() is not None:
            group.widget.remove(self._add_folder_row)
        group.entries = []
        self._folder_rows = []
        for index, folder in enumerate(folders):
            row = self._folder_row(index, folder, blocked)
            keywords = f"folder {folder.path} {'missing unplugged' if folder.missing else ''}"
            if index == 0:
                keywords += " downloads save store wallhaven motionbgs stills destination"
            self._add(group, row, keywords)
            self._folder_rows.append(row)
        if not folders:
            row = Adw.ActionRow(
                title="Not configured",
                subtitle=(
                    f"Suggested folder: {suggestion}"
                    if suggestion
                    else "Choose a folder before downloading or generating stills"
                ),
                use_markup=False,
            )
            self._add(group, row, "folder empty suggested")
            self._folder_rows.append(row)
        self._add(group, self._add_folder_row, "add folder new location")
        self._apply_filter()

    def _folder_row(self, index: int, folder: Folder, blocked: bool) -> Adw.ActionRow:
        row = Adw.ActionRow(title=folder.name, subtitle=folder.path, use_markup=False)
        writers: list[Gtk.Widget] = []
        if folder.missing:
            # Said plainly rather than dropped: a folder on a drive that is not
            # mounted comes back when it is.
            icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            icon.add_css_class("st-warning-icon")
            row.set_subtitle(f"{folder.path} · not there right now")
            row.add_css_class("warning")
        else:
            icon = Gtk.Image.new_from_icon_name(
                "folder-download-symbolic" if index == 0 else "folder-symbolic"
            )
        row.add_prefix(icon)
        if index == 0:
            badge = widgets.pill("Downloads & stills", None, "accent")
            badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(badge)
            row.set_tooltip_text("Downloads and generated stills go here")
        else:
            first = widgets.icon_button(
                "folder-download-symbolic",
                "Make this the download and generated-still folder",
                lambda *_: self.make_download_folder(folder.path),
                "flat",
            )
            first.set_valign(Gtk.Align.CENTER)
            row.add_suffix(first)
            writers.append(first)
        remove = widgets.icon_button(
            "list-remove-symbolic",
            "Remove from Library",
            lambda *_: self._ask_remove(folder.path),
            "flat",
        )
        remove.set_valign(Gtk.Align.CENTER)
        row.add_suffix(remove)
        writers.append(remove)
        for button in writers:
            button.set_sensitive(not blocked)
        return row

    def _choose_folder(self) -> None:
        dialog = Gtk.FileDialog(title="Add a wallpaper folder", modal=True)
        root = self.widget.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        dialog.select_folder(parent, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            chosen = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # dismissed: a toast saying so would be noise
        path = chosen.get_path() if chosen is not None else None
        if path is None:
            self.state.toast("That folder is not on this machine's filesystem")
            return
        self.add_folder(path)

    def add_folder(self, path: str) -> None:
        """Add a folder the user chose (the folder chooser's answer)."""
        if self.access is not None:
            self.access.add_folder(path)

    def make_download_folder(self, path: str) -> None:
        if self.access is not None:
            self.access.make_download_folder(path)

    def _ask_remove(self, path: str) -> None:
        self.confirm_remove(path)

    def confirm_remove(self, path: str) -> Adw.AlertDialog | None:
        """Ask before a folder leaves the library; its files stay where they are."""
        access = self.access
        if access is None:
            return None
        folders = [folder.path for folder in access.folder_list()]
        if path not in folders:
            return None
        escaped = GLib.markup_escape_text(path)
        body = f"<b>{escaped}</b> leaves the library. Its files stay on disk."
        if folders[0] == path and len(folders) > 1:
            following = GLib.markup_escape_text(folders[1])
            body += f"\n\nDownloads and generated stills will go to <b>{following}</b>."
        elif folders[0] == path:
            body += "\n\nDownloads and generated stills stop until you add a folder."
        dialog = Adw.AlertDialog(heading="Remove from library?", body=body, body_use_markup=True)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def done(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "remove" and self.access is not None:
                self.access.remove_folder(path)

        dialog.connect("response", done)
        self._present(dialog)
        return dialog

    # -- 2. Playback ---------------------------------------------------------------------
    def _build_playback(self, access: SettingsAccess) -> None:
        section = self._section("playback")
        group = self._group(
            section,
            "Playback",
            "Saved defaults for the wallpaper service. The player bar changes cycle and "
            "shuffle for this session only.",
            keywords="playback defaults power",
        )
        self._add(
            group,
            self._switch("Shuffle by default", "Initial order when the service starts", "shuffle"),
            "shuffle random order",
        )
        self._add(
            group,
            self._switch(
                "Cycle by default", "Initial timer state when the service starts", "cycle_enabled"
            ),
            "cycle rotate change automatically timer",
        )
        self._add(
            group,
            self._spin(
                "Cycle interval", "Seconds between changes", "cycle_interval", INTERVAL_RANGE, 30
            ),
            "interval cycle rotate timer every seconds minutes",
        )
        self._add(
            group,
            self._switch(
                "Cycle favourites only",
                "Ignored while nothing is starred, so the rotation never empties",
                "cycle_favourites_only",
            ),
            "favorites favourites starred cycle only",
        )
        self._add(
            group,
            self._switch(
                "Animate wallpapers",
                "Play videos and Wallpaper Engine scenes. Off shows paired stills",
                "dynamics_enabled",
            ),
            "animate dynamics motion video scenes stills",
        )
        battery = self._switch(
            "Stop animations on battery",
            "Show still wallpapers on battery. Resume when plugged in, according to your "
            "playback settings",
            "stop_animations_on_battery",
        )
        self._add(
            group,
            battery,
            "battery power laptop energy save stills animation unplugged",
            target="playback.battery",
        )
        self._battery_note = Adw.ActionRow(title="Battery animation control", use_markup=False)
        self._add(
            group,
            self._battery_note,
            "battery power status",
            available=lambda: bool(self.access is not None and self.access.battery_note()),
        )
        self._add(
            group,
            self._combo(
                "When covered by a window",
                "Takes effect on the next video. Try “Keep playing” if the others misbehave",
                "video_when_hidden",
                WHEN_HIDDEN,
            ),
            "pause covered hidden fullscreen window maximized stop memory obscured",
        )

    # -- 3. Displays ---------------------------------------------------------------------
    def _build_displays(self, access: SettingsAccess) -> None:
        section = self._section("displays")
        group = self._group(section, "Displays", keywords="display displays monitor screen")
        self._add(
            group,
            self._combo(
                "Displays",
                "Same wallpaper everywhere is lighter; independent mode unlocks connector "
                "assignments, schedules and separate picks",
                "display_mode",
                DISPLAY_MODES,
            ),
            "mirror mirrored independent same every output connector",
        )
        self._theme_row = Adw.ComboRow(
            title="Colours follow",
            subtitle="Choose which display drives Noctalia's one shell-wide palette",
        )
        self._theme_row.connect("notify::selected", self._on_theme_source)
        self._writers.append(self._theme_row)
        self._add(
            group,
            self._theme_row,
            "colors colours follow display source theme monitor palette",
            available=lambda: self._setting("display_mode") == "independent",
            target="displays.colors",
        )

    def _on_theme_source(self, row: Adw.ComboRow, _param: object) -> None:
        connectors = self._theme_connectors or ()
        index = row.get_selected()
        if 0 <= index < len(connectors):
            self._save("theme_source_connector", connectors[index][0])

    def _refresh_theme_sources(self, access: SettingsAccess) -> None:
        sources = access.theme_sources()
        if sources.connectors != self._theme_connectors:
            # Rebuilt only when the displays changed: the saved choice stays.
            self._theme_connectors = sources.connectors
            labels = [
                connector if attached else f"{connector} (not attached)"
                for connector, attached in sources.connectors
            ]
            self._theme_row.set_model(Gtk.StringList.new(labels))
        names = [connector for connector, _attached in sources.connectors]
        if sources.selected in names:
            self._theme_row.set_selected(names.index(sources.selected))
        self._theme_row.set_subtitle(sources.note)

    # -- 4. Colours ----------------------------------------------------------------------
    def _build_colors(self, access: SettingsAccess) -> None:
        section = self._section("colors")
        group = self._group(
            section,
            "Colours",
            "This window's colours are separate from the colours each wallpaper gives the desktop.",
            keywords="colors colours noctalia theme palette",
        )
        self._add(
            group,
            self._switch(
                "Follow Noctalia for app colours",
                "Update this app's appearance only; pairing colour choices stay unchanged",
                "follow_noctalia_palette",
            ),
            "accent tint app colors window chrome",
        )
        self._source = Adw.ActionRow(title="Palette source", use_markup=False)
        self._source_swatches = widgets.Swatches([], size=12, overlap=True)
        self._source.add_prefix(self._source_swatches)
        reload = widgets.icon_button(
            "view-refresh-symbolic",
            "Reload palette",
            lambda *_: access.reload_app_palette(),
            "flat",
        )
        reload.set_valign(Gtk.Align.CENTER)
        self._source.add_suffix(reload)
        self._add(group, self._source, "palette source current generated reload refresh")
        self._add(
            group,
            self._combo(
                "Default adaptive colour scheme",
                "For previews and adaptive pairings without a chosen scheme. Explicit pairing "
                "colours stay unchanged",
                "preview_scheme",
                SCHEMES,
            ),
            "scheme adaptive tonal spot vibrant soft monochrome rainbow preview default",
            target="colors.scheme",
        )
        palettes = Adw.ActionRow(
            title="Palettes",
            subtitle="Browse, apply, duplicate and edit Noctalia's palettes; preview every scheme",
        )
        manage = self._button("Manage…", self._manage_palettes, "Browse, apply and edit palettes")
        palettes.add_suffix(manage)
        palettes.set_activatable_widget(manage)
        self._add(
            group,
            palettes,
            "palettes browse custom community built-in legacy edit duplicate schemes",
            target="colors.palettes",
        )
        self._build_template_row(access, group)

    def _build_template_row(self, access: SettingsAccess, group: _Group) -> None:
        """Noctalia's palette template: its state as read, then the one button that writes.

        It writes Noctalia's settings, not settings.toml, so read-only settings
        don't turn it off; an upgrade still running does, and says so.
        """
        row = Adw.ActionRow(title="Noctalia template", use_markup=False)
        row.set_subtitle_lines(0)
        self._template_pill = Gtk.Box(valign=Gtk.Align.CENTER)
        row.add_suffix(self._template_pill)
        self._template_spinner = Adw.Spinner(valign=Gtk.Align.CENTER, visible=False)
        row.add_suffix(self._template_spinner)
        self._template_button = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        self._template_button.add_css_class("suggested-action")
        self._template_button.connect("clicked", lambda *_: self.install_template())
        row.add_suffix(self._template_button)
        check = widgets.icon_button(
            "view-refresh-symbolic",
            "Check Noctalia's settings again",
            lambda *_: access.check_template(),
            "flat",
        )
        check.set_valign(Gtk.Align.CENTER)
        row.add_suffix(check)
        self._template_row = row
        self._add(
            group,
            row,
            "template noctalia install repair recolor desktop colours integration",
            target="colors.template",
        )
        # Its own row and press: installing never renders, and this does more
        # than Wall-in-One's template, so it says so before it is pressed.
        render = Adw.ActionRow(
            title="Render the colours now",
            subtitle=(
                "Asks Noctalia to re-render all of its templates, not only Wall-in-One's: "
                "everything Noctalia themes is redrawn with its current colours."
            ),
            use_markup=False,
        )
        render.set_subtitle_lines(0)
        self._render_button = Gtk.Button(label="Render now", valign=Gtk.Align.CENTER)
        self._render_button.connect("clicked", lambda *_: self.render_templates())
        render.add_suffix(self._render_button)
        self._render_row = render
        self._add(
            group,
            render,
            "render now templates noctalia apply redraw",
            available=self._render_offered,
        )

    def install_template(self) -> None:
        """The template row's button."""
        if self.access is not None and self._template_button.get_sensitive():
            self.access.install_template()

    def render_templates(self) -> None:
        """The Render now button."""
        if (
            self.access is not None
            and self._render_offered()
            and (self._render_button.get_sensitive())
        ):
            self.access.render_templates()

    def _render_offered(self) -> bool:
        return self.access is not None and self.access.template_status().can_render

    def _sync_template(self, access: SettingsAccess) -> None:
        status = access.template_status()
        pill = {
            "checking": ("Checking…", None, ""),
            "installed": ("Installed", "object-select-symbolic", "success"),
            "missing": ("Not installed", "dialog-warning-symbolic", "warning"),
            "stale": ("Needs repair", "dialog-warning-symbolic", "warning"),
            "unavailable": ("Can't install", "dialog-warning-symbolic", "error"),
        }.get(status.state, ("", None, ""))
        while (child := self._template_pill.get_first_child()) is not None:
            self._template_pill.remove(child)
        label, icon, tone = pill
        if label:
            self._template_pill.append(widgets.pill(label, icon, *([tone] if tone else [])))
        subtitle = status.detail
        if status.blocked:
            subtitle = f"{subtitle}\n{status.blocked}"
        self._template_row.set_subtitle(subtitle)
        self._template_button.set_label(status.action or "Install")
        self._template_button.set_visible(bool(status.action))
        self._template_button.set_sensitive(not status.blocked and not status.busy)
        self._template_spinner.set_visible(status.busy)
        self._render_row.set_visible(status.can_render)
        self._render_button.set_sensitive(not status.blocked and not status.busy)

    def _manage_palettes(self) -> None:
        self.open_palettes()

    def open_palettes(self, view: str = "palettes") -> PalettesDialog | None:
        """The palette manager: installed palettes, or (``schemes``) the scheme previews."""
        access = self.access
        if access is None:
            return None
        dialog = PalettesDialog(
            self.state, access, schemes=view == "schemes", dark=self._dark_preview()
        )
        self._present(dialog)
        return dialog

    @staticmethod
    def _dark_preview() -> bool:
        return Adw.StyleManager.get_default().get_dark()

    # -- 5. Appearance -------------------------------------------------------------------
    def _build_appearance(self, access: SettingsAccess) -> None:
        section = self._section("appearance")
        group = self._group(
            section,
            "Appearance",
            "This window's look is saved for this window only.",
            keywords="appearance window app look",
        )
        style_row = Adw.ActionRow(title="Window style")
        self._style_toggle = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for name, label, tip in STYLES:
            toggle = Adw.Toggle(name=name, label=label)
            toggle.set_tooltip(tip)
            self._style_toggle.add(toggle)
        self._style_toggle.connect("notify::active-name", self._on_window_style)
        style_row.add_suffix(self._style_toggle)
        self._style_row = style_row
        self._add(group, style_row, "window style glass frosted blur translucent transparent solid")

        def glass() -> bool:
            return self.state.window_style != "solid"

        self._background_row, self._background_scale = self._dial(
            "Background opacity",
            "The page behind the grid and lists",
            self.state.set_background_opacity,
        )
        self._add(
            group,
            self._background_row,
            "background opacity transparency translucent see-through window glass",
            available=glass,
        )
        self._panel_row, self._panel_scale = self._dial(
            "Panel opacity",
            "Sidebar, header, player bar, details and cards",
            self.state.set_panel_opacity,
        )
        self._add(
            group,
            self._panel_row,
            "panel opacity transparency translucent see-through cards glass",
            available=glass,
        )
        self._frost_row, self._frost_scale = self._dial(
            "Frost", "How strongly the wallpaper behind is blurred", self.state.set_frost
        )
        self._add(
            group,
            self._frost_row,
            "frost blur frosted glass strength soft clear",
            available=lambda: self.state.window_style == "frosted",
        )
        if self.launch is not None:
            for row, keywords in zip(
                self.launch.rows,
                ("interface classic new preview start next", "gpu acceleration renderer cairo"),
                strict=True,
            ):
                self._add(
                    group,
                    row,
                    keywords,
                    target="appearance.interface" if row is self.interface_row else None,
                )
        # settings.toml's `opacity` is the classic window's own: in a group of its
        # own, so nobody expects it to change this window (its look is the style).
        classic = self._group(
            section,
            "Classic interface",
            "Used only when Wall-in-One starts the classic interface. This window's look is "
            "its window style above.",
            keywords="classic interface window",
        )
        self._add(
            classic,
            self._spin(
                "Window opacity",
                "The classic window's background, from 0.30 to 1 (opaque)",
                "opacity",
                OPACITY_RANGE,
                0.01,
                digits=2,
            ),
            "opacity classic window transparency translucent",
        )
        self._sync_window_style()

    def _dial(
        self, title: str, subtitle: str, setter: Callable[[float], None]
    ) -> tuple[Adw.ActionRow, Gtk.Scale]:
        """A 0-100% slider row that applies live while dragging; saved once it rests."""
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        scale.set_draw_value(False)
        scale.set_size_request(150, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.update_property([Gtk.AccessibleProperty.LABEL], [title])
        value = Gtk.Label(label="100%", width_chars=4, xalign=1)
        value.add_css_class("st-value")
        value.add_css_class("dimmed")

        def moved(widget: Gtk.Scale) -> None:
            stepped = round(widget.get_value() / 5) * 5
            value.set_label(f"{stepped}%")
            if not self._loading:
                setter(stepped / 100)

        scale.connect("value-changed", moved)
        row.add_suffix(scale)
        row.add_suffix(value)
        return row, scale

    def _on_window_style(self, group: Adw.ToggleGroup, _param: object) -> None:
        name = group.get_active_name()
        if not self._loading and name and name != self.state.window_style:
            self.state.set_window_style(name)

    def _sync_window_style(self) -> None:
        """Show the window's look as saved (the ``appearance`` topic), never save it."""
        state = self.state
        style = state.window_style
        blocked = state.appearance_blocked()
        self._loading = True
        try:
            self._style_toggle.set_active_name(style)
            for scale, value in (
                (self._background_scale, state.background_alpha),
                (self._panel_scale, state.panel_alpha),
                (self._frost_scale, state.frost),
            ):
                scale.set_value(round(value * 100))
        finally:
            self._loading = False
        self._style_row.set_subtitle(blocked or STYLE_NOTES.get(style, ""))
        for widget in (self._style_toggle, self._background_row, self._panel_row, self._frost_row):
            widget.set_sensitive(not blocked)
        self._apply_filter()

    # -- 6. Online sources -----------------------------------------------------------------
    def _build_online(self, access: SettingsAccess) -> None:
        section = self._section("online")
        group = self._group(
            section,
            "Online sources",
            "Wallhaven searches work without an API key, but NSFW results are only "
            "reachable with one. A key saved here is written to a file only you can read.",
            keywords="online store providers internet download",
        )
        self._key_row = Adw.ActionRow(title="Wallhaven API key", use_markup=False)
        self._key_row.set_subtitle_lines(0)
        self._key_remove = self._button("Remove", self.clear_key, "Delete the saved key file")
        self._key_row.add_suffix(self._key_remove)
        self._add(group, self._key_row, "wallhaven api key account token", target="online.key")
        # A password row, never filled from the stored key: the page only
        # needs to know that one exists.
        self._key_entry = Adw.PasswordEntryRow(title="New key", show_apply_button=True)
        self._key_entry.connect("apply", lambda *_: self.save_key())
        self._key_entry.connect("entry-activated", lambda *_: self.save_key())
        # Otherwise the row stays red from a refused key while the corrected one is typed.
        self._key_entry.connect("changed", lambda entry: entry.remove_css_class("error"))
        self._add(group, self._key_entry, "wallhaven api key paste token password")

    def save_key(self) -> None:
        access = self.access
        if access is None or not self._key_entry.get_sensitive():
            return
        key = self._key_entry.get_text()
        self._key_entry.set_sensitive(False)

        def done(outcome: str) -> None:
            self._key_entry.set_sensitive(True)
            if outcome == KEY_SAVED:
                self._key_entry.remove_css_class("error")
                self._key_entry.set_text("")
            elif outcome == KEY_REFUSED:
                self._key_entry.add_css_class("error")

        access.save_key(key, done)

    def clear_key(self) -> None:
        if self.access is not None:
            self.access.clear_key()

    # -- 7. Advanced ---------------------------------------------------------------------
    def _build_advanced(self, access: SettingsAccess) -> None:
        section = self._section("advanced")
        group = self._group(
            section,
            "Advanced",
            "Renderer settings. The defaults suit most systems",
            keywords="advanced renderer expert",
        )
        video = Adw.ExpanderRow(title="Video", subtitle="Sound, decoding and smoothing")
        video.add_prefix(Gtk.Image.new_from_icon_name("video-x-generic-symbolic"))
        self._expanders.append(video)
        self._add(group, video, "video mpv mpvpaper")
        self._add_child(
            group,
            video,
            self._switch(
                "Mute video wallpapers",
                "The audio track stays loaded, so this takes effect immediately",
                "video_muted",
            ),
            "sound audio mute muted",
        )
        self._add_child(
            group,
            video,
            self._spin(
                "Video volume",
                "Applied to the video already playing",
                "video_volume",
                VOLUME_RANGE,
                5,
            ),
            "volume sound audio loud",
        )
        self._add_child(
            group,
            video,
            self._switch(
                "Hardware video decoding",
                "Usually lowers CPU use; turn off to diagnose decoder or driver artifacts",
                "video_hardware_decode",
            ),
            "hardware decoding gpu vaapi cpu driver artifacts glitch",
        )
        self._add_child(
            group,
            video,
            self._combo(
                "Smooth low-frame-rate videos",
                "Uses an unambiguous active-monitor refresh; mixed-rate All outputs keeps "
                "source cadence. Linear blending may ghost",
                "video_interpolation",
                INTERPOLATION,
            ),
            "smooth motion interpolation oversample linear frame rate judder",
        )
        scenes = Adw.ExpanderRow(title="Scenes", subtitle="Wallpaper Engine")
        scenes.add_prefix(Gtk.Image.new_from_icon_name("applications-games-symbolic"))
        self._expanders.append(scenes)
        self._add(group, scenes, "scenes wallpaper engine linux-wallpaperengine steam")
        self._add_child(
            group,
            scenes,
            self._switch(
                "Play Wallpaper Engine scenes",
                "Starts linux-wallpaperengine only when no other process owns the target output",
                "own_scene_renderer",
            ),
            "run own renderer linux-wallpaperengine start",
        )
        self._renderer = Adw.ActionRow(title="Wallpaper Engine renderer", use_markup=False)
        self._renderer_icon = Gtk.Image()
        self._renderer.add_prefix(self._renderer_icon)
        self._add_child(group, scenes, self._renderer, "renderer installed found available")
        self._add_child(
            group,
            scenes,
            self._spin(
                "Wallpaper Engine frame rate",
                "Native scene-rendering limit; videos keep their source frame rate",
                "scene_fps",
                FPS_RANGE,
                1,
            ),
            "frame rate fps power",
        )
        self._add_child(
            group,
            scenes,
            self._combo(
                "Wallpaper Engine scaling",
                "Global scene fit; videos keep their own Fill behavior",
                "scene_scaling",
                SCENE_SCALING,
            ),
            "scaling fit fill stretch crop aspect",
        )
        self._add_child(
            group,
            scenes,
            self._combo(
                "Wallpaper Engine texture edges",
                "Controls sampling outside a scene's texture bounds",
                "scene_clamp",
                SCENE_CLAMP,
            ),
            "texture edges clamp border repeat sampling",
        )

    # -- 8. Tidy up, 9. About ------------------------------------------------------------
    def _build_tidy(self) -> None:
        tidy = self.tidy
        if not isinstance(tidy, Adw.PreferencesGroup):
            return
        section = _Section("tidy", Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18))
        section.box.add_css_class("st-section")
        self._sections["tidy"] = section
        self._content.append(section.box)
        # The section is one entry: Tidy up's rows come and go with its preview.
        holder = _Group(tidy, "Tidy up", "tidy clean leftovers archive cache old plugin template")
        holder.entries.append(_Entry(tidy, "tidy up clean leftovers archive cache"))
        section.groups.append(holder)
        section.box.append(tidy)

    def _build_log(self) -> None:
        log = self.log
        if not isinstance(log, Adw.PreferencesGroup):
            return
        section = _Section("log", Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18))
        section.box.add_css_class("st-section")
        self._sections["log"] = section
        self._content.append(section.box)
        holder = _Group(log, "Log", "log logs diagnostics errors problems events copy paths")
        holder.entries.append(_Entry(log, "log logs diagnostics errors problems events copy"))
        section.groups.append(holder)
        section.box.append(log)

    def _build_about(self) -> None:
        section = self._section("about")
        group = self._group(section, "About", keywords="about version help")
        for title, icon, action, keywords in (
            ("Wall-in-One", "help-about-symbolic", "win.about", "version about credits license"),
            (
                "Keyboard shortcuts",
                "preferences-desktop-keyboard-shortcuts-symbolic",
                "win.shortcuts",
                "keyboard shortcuts keys hotkeys accelerators",
            ),
        ):
            row = Adw.ActionRow(title=title, activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect(
                "activated", lambda *_, name=action: self.widget.activate_action(name, None)
            )
            self._add(group, row, keywords)

    # -- search ----------------------------------------------------------------------------
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
                        if words and hits and isinstance(entry.row, Adw.ExpanderRow):
                            entry.row.set_expanded(True)
                    if entry.row is not group.widget:
                        entry.row.set_visible(visible)
                    group_visible |= visible
                group.widget.set_visible(group_visible)
                section_visible |= group_visible
            section.box.set_visible(section_visible)
            anything |= section_visible
        self._stack.set_visible_child_name("page" if anything else "empty")

    # -- deep links -------------------------------------------------------------------------
    def scroll_to(self, key: str, flash: bool = True) -> None:
        if self.access is None:
            return
        key = ALIASES.get(key, key)
        if self._query:
            self.search.set_text("")
            self._set_query("")
        target = self._targets.get(key)
        section = self._sections.get(key.partition(".")[0])
        if target is None and section is not None:
            target = section.box
        if target is None:
            return
        attempts = [0]

        def run() -> bool:
            attempts[0] += 1
            ready = target.get_mapped() and self._clamp.get_height() > 1
            if not ready and attempts[0] < 60:
                return GLib.SOURCE_CONTINUE
            ok, point = target.compute_point(self._clamp, Graphene.Point().init(0, 0))
            if not ok:
                return GLib.SOURCE_REMOVE
            adjustment = self._scroller.get_vadjustment()
            # Rows sit a little lower than their section so the group title stays visible.
            offset = 64 if isinstance(target, Gtk.ListBoxRow) else 14
            destination = max(
                0.0, min(point.y - offset, adjustment.get_upper() - adjustment.get_page_size())
            )
            if self._animation is not None:
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
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(30, run)

    @staticmethod
    def _flash(widget: Gtk.Widget) -> None:
        css = "st-row-flash" if isinstance(widget, Gtk.ListBoxRow) else "st-flash"
        widget.add_css_class(css)

        def done() -> bool:
            widget.remove_css_class(css)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(1500, done)

    # -- dialogs ------------------------------------------------------------------------------
    def _present(self, dialog: Adw.Dialog) -> None:
        self._dialogs.append(dialog)
        dialog.connect("closed", lambda closed: self._forget(closed))
        dialog.present(self.widget)

    def _forget(self, dialog: Adw.Dialog) -> None:
        if dialog in self._dialogs:
            self._dialogs.remove(dialog)

    def _close_dialogs(self) -> None:
        for dialog in list(self._dialogs):
            dialog.force_close()
        self._dialogs.clear()

    # -- what the adapter says ------------------------------------------------------------------
    def _setting(self, key: str) -> Any:
        return self.access.setting(key) if self.access is not None else None

    def _sync(self) -> None:
        """Show what the adapter reports; never saves (``_loading`` holds every row)."""
        access = self.access
        if access is None:
            return
        blocked = access.settings_blocked()
        self._loading = True
        try:
            for binding in self._bindings:
                value = access.setting(binding.key)
                row = binding.row
                if isinstance(row, Adw.SwitchRow):
                    row.set_active(bool(value))
                elif isinstance(row, Adw.SpinRow):
                    row.set_value(value)
                elif isinstance(row, Adw.ComboRow):
                    keys = [choice for choice, _label in binding.choices]
                    if value in keys:
                        row.set_selected(keys.index(value))
            self._refresh_theme_sources(access)
        finally:
            self._loading = False
        self._notice.set_subtitle(blocked)
        for widget in self._writers:
            widget.set_sensitive(not blocked)
        self._rebuild_folders(access, bool(blocked))
        note = access.battery_note()
        self._battery_note.set_subtitle(note)
        available = access.scenes_available()
        self._renderer.set_subtitle(
            "Checking…"
            if available is None
            else "linux-wallpaperengine is available"
            if available
            else "linux-wallpaperengine was not found. Install the Nix package or launch "
            "Wall-in-One from its packaged wrapper."
        )
        self._renderer_icon.set_from_icon_name(
            "emblem-ok-symbolic" if available is not False else "dialog-warning-symbolic"
        )
        self._sync_key(access)
        self._sync_template(access)
        self._sync_palette(access)
        self._apply_filter()

    def _sync_key(self, access: SettingsAccess) -> None:
        status = access.key_status()
        self._key_row.set_subtitle(status.text)
        self._key_remove.set_visible(status.stored)
        self._key_entry.set_title("Replace key" if status.stored else "New key")

    def _sync_palette(self, access: SettingsAccess) -> None:
        palette = access.app_palette()
        self._source.set_subtitle(palette.detail)
        self._source_swatches.set_colors(list(palette.swatches))

    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic == "settings":
            self._sync()
        elif topic == "theme" and self.access is not None:
            self._sync_palette(self.access)
        elif topic == "appearance":
            self._sync_window_style()


def create(state: AppState) -> Page:
    """The page as the prototype builds it: no launch rows, no Tidy up."""
    return SettingsPage(state)
