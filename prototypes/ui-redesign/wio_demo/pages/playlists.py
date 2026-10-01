"""One playlist: its cover, where it is used, how it plays, and its ordered wallpapers.

The sidebar lists the playlists; this page shows the selected one. Entries are
ordered and may repeat a wallpaper (each entry is its own row). Reordering is
direct: drag a row up or down (it lifts and the others roll aside), use its
menu, or press Alt+↑/↓. Removing, renaming, duplicating and deleting all offer
Undo.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango

from .. import art, data, reorder, ui
from ..catalog import DAYS, INTERVALS, KIND_LABEL, MONTHS, MONTHS_LONG
from ..models import Playlist, Rule, Wallpaper
from . import Page
from .playlists_picker import PICKER_CSS, CardGrid, PlaylistPicker

CSS = """
.pl-cover { border-radius: 16px; box-shadow: 0 1px 3px alpha(black, 0.20), 0 8px 20px alpha(black, 0.16); }
.pl-cover-empty {
  border-radius: 16px; background-color: alpha(currentColor, 0.07);
  border: 2px dashed alpha(currentColor, 0.16); color: alpha(currentColor, 0.40);
}
.pl-name { font-size: 1.8em; font-weight: 800; }
.pl-chip { padding: 2px 10px; min-height: 26px; }
.pl-chip.off { opacity: 0.55; }
.pl-used-label { margin-right: 2px; }
.pl-status { font-size: 0.92em; }
.pl-play { padding-left: 18px; padding-right: 20px; min-height: 36px; font-weight: 700; }

.pl-count { font-feature-settings: "tnum"; }
list.pl-entries.reorder-list > row {
  transition: background-color 900ms ease-out, border-radius 200ms ease-out,
    box-shadow 160ms ease-out, transform 160ms ease-out;
}
list.pl-entries.reorder-list > row.pl-flash { background-color: alpha(var(--accent-bg-color), 0.24); transition: none; }
.pl-entries > row.pl-dragging { opacity: 0.35; }
.pl-entries > row.pl-selected { background-color: alpha(var(--accent-bg-color), 0.12); }
.pl-entries.pl-compact > row > box.header { min-height: 42px; }
.pl-number {
  font-feature-settings: "tnum"; font-weight: 700; font-size: 0.92em; opacity: 0.55;
}
.pl-row-button { min-width: 32px; min-height: 32px; padding: 0; }
.pl-drop-line { margin: 0 6px; }
.pl-drop-bar {
  min-height: 4px; border-radius: 999px;
  background-color: var(--accent-bg-color);
  box-shadow: 0 0 0 3px alpha(var(--accent-bg-color), 0.22);
}
.pl-drop-dot {
  min-width: 10px; min-height: 10px; border-radius: 999px;
  background-color: var(--view-bg-color); box-shadow: inset 0 0 0 3px var(--accent-bg-color);
}
.pl-drag-icon {
  padding: 6px 14px 6px 6px; border-radius: 12px;
  background-color: var(--popover-bg-color); color: var(--popover-fg-color);
  box-shadow: 0 0 0 1px alpha(black, 0.10), 0 6px 18px alpha(black, 0.30);
}
.pl-drag-icon label { font-weight: 700; }
.pl-list-wrap:drop(active), .pl-drop-zone:drop(active) { box-shadow: none; outline: none; }
.pl-drop-zone { border: 2px dashed alpha(currentColor, 0.16); border-radius: 16px; }
.pl-drop-zone.pl-drop-active {
  border-color: var(--accent-bg-color); background-color: alpha(var(--accent-bg-color), 0.08);
}
.pl-auto-note { padding: 10px 12px; border-radius: 12px; background-color: alpha(currentColor, 0.05); }
"""


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------


def _days_text(days: list[int]) -> str:
    days = sorted(set(days))
    if not days or len(days) == 7:
        return "Every day"
    if days == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if days == [5, 6]:
        return "Weekends"
    if len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
        return f"{DAYS[days[0]]}–{DAYS[days[-1]]}"
    return ", ".join(DAYS[day] for day in days)


def _months_text(months: list[int]) -> str:
    months = sorted(set(months))
    if len(months) == 1:
        return MONTHS_LONG[months[0]]
    if months == list(range(months[0], months[-1] + 1)):
        return f"{MONTHS[months[0]]}–{MONTHS[months[-1]]}"
    return ", ".join(MONTHS[month] for month in months)


def describe_rule(rule: Rule) -> str:
    """“Every day 07:00–18:00”, “Weekends 09:00–19:00”, “All of December”."""
    timed = bool(rule.start and rule.end and rule.start != rule.end)
    if timed:
        text = f"{_days_text(rule.days)} {rule.start}–{rule.end}"
    elif rule.days:
        text = f"{_days_text(rule.days)}, all day"
    elif rule.months:
        text = ""
    else:
        text = "Always"
    if rule.months:
        text = f"{text} in {_months_text(rule.months)}" if text else f"All of {_months_text(rule.months)}"
    if rule.display:
        text += f" · {rule.display}"
    return text


def summary(playlist: Playlist) -> str:
    """The hero's subtitle: just the count; the Playback card beside it says how it changes."""
    count = len(playlist.entries)
    return "No wallpapers yet" if not count else "1 wallpaper" if count == 1 else f"{count} wallpapers"


def _wrap_labels(widget: Gtk.Widget) -> None:
    child = widget.get_first_child()
    while child:
        if isinstance(child, Gtk.Label):
            child.set_wrap(True)
            child.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            child.set_xalign(0)
        _wrap_labels(child)
        child = child.get_next_sibling()


def _clear(container: Gtk.Widget) -> None:
    child = container.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        container.remove(child)
        child = nxt


# ---------------------------------------------------------------------------
# One entry
# ---------------------------------------------------------------------------


class EntryRow(Adw.ActionRow):
    """A playlist entry: handle · position · picture · name · status · remove · menu."""

    def __init__(
        self,
        page: PlaylistPage,
        index: int,
        wid: str,
        *,
        total: int,
        on_screen: list[str],
        repeats: list[int],
        compact: bool,
        narrow: bool,
        selecting: bool,
        selected: bool,
    ) -> None:
        super().__init__(use_markup=False, title_lines=1, subtitle_lines=1)
        self.page, self.index, self.wid = page, index, wid
        self.wallpaper = wallpaper = data.BY_ID[wid]
        self.menu_button: Gtk.MenuButton | None = None
        self.check: Gtk.CheckButton | None = None
        self.set_title(wallpaper.name)
        bits = [KIND_LABEL[wallpaper.kind]]
        if wallpaper.duration:
            bits.append(wallpaper.duration)
        if repeats:
            bits.append("also at " + ", ".join(str(i + 1) for i in repeats))
        if compact:
            self.set_tooltip_text(" · ".join([wallpaper.name, *bits]))
        else:
            self.set_subtitle(" · ".join(bits))

        # -- prefixes (add_prefix prepends, so right to left) --------------------
        width, height = (56, 32) if compact else (80, 45) if narrow else (96, 54)
        self.add_prefix(ui.thumbnail(wallpaper, width, height, 6 if compact else 8))
        number = Gtk.Label(label=str(index + 1), xalign=1, width_chars=max(1, len(str(total))))
        number.add_css_class("pl-number")
        self.add_prefix(number)
        self.number = number
        self.handle: Gtk.Widget | None = None
        if selecting:
            self.check = Gtk.CheckButton(active=selected, valign=Gtk.Align.CENTER)
            self.check.update_property([Gtk.AccessibleProperty.LABEL], [f"Select {wallpaper.name}"])
            self.check.connect("toggled", self._on_check)
            if selected:
                self.add_css_class("pl-selected")
            self.add_prefix(self.check)
            self.set_activatable_widget(self.check)
        else:
            # Drag it like a slider; the list (reorder.ReorderList) does the rest.
            handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
            handle.add_css_class("pl-handle")
            handle.set_tooltip_text("Drag up or down to reorder (Alt+↑ / Alt+↓)")
            handle.update_property([Gtk.AccessibleProperty.LABEL], [f"Reorder {wallpaper.name}"])
            self.add_prefix(handle)
            self.handle = handle

        # -- suffixes ----------------------------------------------------------
        if wallpaper.problem:
            skipped = ui.pill("" if narrow else "Skipped", "dialog-warning-symbolic", "warning")
            skipped.set_valign(Gtk.Align.CENTER)
            skipped.set_tooltip_text("Skipped after a playback problem — retry it from the Library")
            self.add_suffix(skipped)
        if on_screen:
            where = "On screen" if len(on_screen) > 1 else f"On {on_screen[0]}"
            badge = ui.pill("" if narrow else where, "media-playback-start-symbolic", "accent")
            badge.set_valign(Gtk.Align.CENTER)
            badge.set_tooltip_text(f"Showing now on {' and '.join(on_screen)}")
            self.add_suffix(badge)
        if selecting:
            return
        remove = ui.icon_button(
            "list-remove-symbolic",
            "Remove from playlist (Delete)",
            lambda *_: page.remove_entries([page.position(self)]),
            "flat",
            "circular",
            "pl-row-button",
        )
        remove.set_valign(Gtk.Align.CENTER)
        self.add_suffix(remove)
        self.menu_button = Gtk.MenuButton(
            icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, menu_model=self._menu_model(total)
        )
        self.menu_button.add_css_class("flat")
        self.menu_button.add_css_class("circular")
        self.menu_button.add_css_class("pl-row-button")
        self.menu_button.set_tooltip_text("More")
        self.menu_button.update_property([Gtk.AccessibleProperty.LABEL], [f"More for {wallpaper.name}"])
        self.add_suffix(self.menu_button)
        self._install_actions(total)

        # Sideways drags copy the wallpaper into another playlist in the sidebar;
        # up and down is the list's own reorder.
        source = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        source.connect("prepare", self._prepare)
        source.connect("drag-begin", lambda src, drag: page.drag_begin(self, src, drag))
        source.connect("drag-end", lambda *_: page.drag_end())
        self.add_controller(source)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        secondary = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        secondary.connect("pressed", lambda _g, _n, x, y: self.popup_context(x, y))
        self.add_controller(secondary)
        self._context: Gtk.PopoverMenu | None = None

    def _on_check(self, button: Gtk.CheckButton) -> None:
        (self.add_css_class if button.get_active() else self.remove_css_class)("pl-selected")
        self.page.set_entry_selected(self.index, button.get_active())

    def popup_context(self, x: float, y: float) -> None:
        """Right-click: the same menu as ⋮, at the pointer."""
        if self._context is None:
            self._context = Gtk.PopoverMenu.new_from_model(self.menu_button.get_menu_model())
            self._context.set_has_arrow(False)
            self._context.set_halign(Gtk.Align.START)
            self._context.set_parent(self)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self._context.set_pointing_to(rect)
        self._context.popup()

    def release(self) -> None:
        if getattr(self, "_context", None) is not None:
            self._context.unparent()
            self._context = None

    def _menu_model(self, total: int) -> Gio.Menu:
        def item(label: str, action: str, accel: str | None = None) -> Gio.MenuItem:
            entry = Gio.MenuItem.new(label, action)
            if accel:
                entry.set_attribute_value("accel", GLib.Variant.new_string(accel))
            return entry

        menu = Gio.Menu()
        play = Gio.Menu()
        play.append_item(item("Play from here", "row.play"))
        menu.append_section(None, play)
        move = Gio.Menu()
        move.append_item(item("Move to top", "row.top", "<Alt>Home"))
        move.append_item(item("Move up", "row.up", "<Alt>Up"))
        move.append_item(item("Move down", "row.down", "<Alt>Down"))
        move.append_item(item("Move to bottom", "row.bottom", "<Alt>End"))
        menu.append_section(None, move)
        other = Gio.Menu()
        other.append_item(item("Show in Library", "row.library"))
        other.append_item(item("Remove from playlist", "row.remove", "Delete"))
        menu.append_section(None, other)
        return menu

    def _install_actions(self, total: int) -> None:
        page, index, last = self.page, self.index, total - 1

        def here() -> int:
            return page.position(self)

        group = Gio.SimpleActionGroup()
        for name, callback, enabled in (
            ("play", lambda: page.play_from(here()), True),
            ("top", lambda: page.move_row(self, 0), index > 0),
            ("up", lambda: page.move_row(self, here() - 1), index > 0),
            ("down", lambda: page.move_row(self, here() + 1), index < last),
            ("bottom", lambda: page.move_row(self, last), index < last),
            ("library", lambda: page.state.navigate(f"library:{self.wid}"), True),
            ("remove", lambda: page.remove_entries([here()]), True),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=callback: cb())
            action.set_enabled(enabled)
            group.add_action(action)
        self.insert_action_group("row", group)

    def _prepare(self, _source, _x, _y) -> Gdk.ContentProvider:
        # A plain wallpaper id: this list reorders it, the sidebar copies it into
        # another playlist, exactly like a drag from the Library.
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_STRING, self.wid))

    def _on_key(self, _ctrl, keyval: int, _code: int, mods: Gdk.ModifierType) -> bool:
        page, index = self.page, self.page.position(self)
        alt = bool(mods & Gdk.ModifierType.ALT_MASK)
        plain = not (mods & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
        last = len(page.playlist.entries) - 1
        # Moves roll the row into place (key repeat keeps rolling), then commit once.
        if alt and keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            page.move_row(self, index - 1)
        elif alt and keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            page.move_row(self, index + 1)
        elif alt and keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            page.move_row(self, 0)
        elif alt and keyval in (Gdk.KEY_End, Gdk.KEY_KP_End):
            page.move_row(self, last)
        elif plain and keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            page.remove_entries([index])
        elif keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and mods & Gdk.ModifierType.SHIFT_MASK):
            if self.menu_button:
                self.menu_button.popup()
        else:
            return False
        return True


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


class PlaylistPage(Page):
    name = "playlist"
    title = "Playlist"

    def __init__(self, state) -> None:
        super().__init__(state)
        ui.add_css(CSS)
        ui.add_css(PICKER_CSS)
        self._pid: str | None = None
        self._narrow = False
        self._compact = False
        self._selecting = False
        self._selected: set[int] = set()
        self._rows: list[EntryRow] = []
        self._building = False
        self._drag_from: int | None = None  # a row dragged sideways (copy), not a reorder
        self._render_deferred = False
        self._drop_at: int | None = None
        self._scroll_speed = 0.0
        self._scroll_source = 0
        self._pointer: tuple[float, float] | None = None
        self._pending_focus: int | None = None
        self._pending_flash: set[int] = set()
        self._pending_scroll: int | None = None
        self._picker: PlaylistPicker | None = None
        self._dialog: Adw.AlertDialog | None = None
        self._editing_pid: str | None = None

        # -- header bar contributions ------------------------------------------
        # Shown only once the inline "Add wallpapers…" has scrolled out of view,
        # so the page never offers the same button twice (see _sync_header_add).
        self._add_header = ui.icon_button("list-add-symbolic", "Add wallpapers", lambda *_: self.open_picker())
        self._add_header.set_visible(False)
        self._select = ui.select_toggle()
        self._select.connect("toggled", self._on_select_mode)
        self._menu_model = Gio.Menu()
        self._menu = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=self._menu_model)
        self._menu.set_tooltip_text("Playlist menu")
        self._title_reveal = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.CROSSFADE, child=self._title)

        # -- body ------------------------------------------------------------------
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24,
            margin_top=24,
            margin_bottom=28,
            margin_start=18,
            margin_end=18,
        )
        body.append(self._build_hero())
        body.append(self._build_entries())
        self._scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._scroller.set_child(Adw.Clamp(maximum_size=980, tightening_threshold=760, child=body))
        self._scroller.get_vadjustment().connect("value-changed", self._on_scrolled)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(self._scroller)
        outer.append(self._build_selection_bar())
        breakpoint_bin = Adw.BreakpointBin(width_request=340, height_request=300, child=outer)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        narrow.add_setter(self._hero, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self._hero, "spacing", 18)
        narrow.add_setter(body, "margin-start", 12)
        narrow.add_setter(body, "margin-end", 12)
        narrow.add_setter(body, "margin-top", 16)
        narrow.connect("apply", lambda *_: self._set_narrow(True))
        narrow.connect("unapply", lambda *_: self._set_narrow(False))
        breakpoint_bin.add_breakpoint(narrow)
        self.widget = breakpoint_bin
        self._install_actions()

        shortcuts = Gtk.ShortcutController()
        for trigger, callback in (("Escape", self._on_escape), ("<Control>a", self._on_select_all_key)):
            shortcuts.add_shortcut(
                Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger), Gtk.CallbackAction.new(callback))
            )
        self.widget.add_controller(shortcuts)
        state.connect("changed", self._on_changed)

    # -- Page API ----------------------------------------------------------------
    def title_widget(self) -> Gtk.Widget:
        return self._title_reveal

    def header_start(self) -> list[Gtk.Widget]:
        return [self._add_header]

    def header_end(self) -> list[Gtk.Widget]:
        return [self._menu]

    def activate(self, argument: str | None) -> None:
        pid = argument if argument in data.PLAYLIST_BY_ID else self._pid
        if pid not in data.PLAYLIST_BY_ID:
            pid = self.state.playlists[0].id
        for dialog in (self._picker, self._dialog):
            if dialog is not None and dialog.get_parent() is not None:
                dialog.force_close()
        self._picker = self._dialog = None
        self._menu.popdown()
        switched = pid != self._pid
        if switched:
            if self._name.get_editing():
                self._name.stop_editing(True)  # commits to the playlist being edited
            self._pid = pid
        if self._select.get_active():
            self._select.set_active(False)  # re-renders
        self._selected.clear()
        self._render()
        if switched:
            adjustment = self._scroller.get_vadjustment()
            adjustment.set_value(0)
            GLib.idle_add(lambda: (adjustment.set_value(0), False)[1])

    @property
    def playlist(self) -> Playlist:
        return data.PLAYLIST_BY_ID[self._pid]

    def _alive(self) -> bool:
        return self._pid in data.PLAYLIST_BY_ID

    # -- building: hero ----------------------------------------------------------
    def _build_hero(self) -> Gtk.Widget:
        self._hero = Gtk.Box(spacing=32)
        identity = Gtk.Box(spacing=20, hexpand=True)
        self._cover = Adw.Bin(valign=Gtk.Align.START)
        identity.append(self._cover)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, valign=Gtk.Align.CENTER)
        name_row = Gtk.Box(spacing=2)
        self._name = Gtk.EditableLabel(valign=Gtk.Align.CENTER)
        self._name.connect("changed", self._fit_name)
        _wrap_labels(self._name)  # long names wrap instead of widening the page
        self._name.add_css_class("pl-name")
        self._name.set_tooltip_text("Click to rename")
        self._name.connect("notify::editing", self._on_name_editing)
        self._name_static = Gtk.Label(xalign=0, ellipsize=3, wrap=False)
        self._name_static.add_css_class("pl-name")
        self._rename = ui.icon_button(
            "document-edit-symbolic", "Rename", lambda *_: self.start_rename(), "flat", "circular"
        )
        self._rename.set_valign(Gtk.Align.CENTER)
        name_row.append(self._name)
        name_row.append(self._name_static)
        name_row.append(self._rename)
        text.append(name_row)
        self._summary = ui.dim("")
        self._summary.add_css_class("numeric")
        text.append(self._summary)
        self._used = Adw.WrapBox(child_spacing=6, line_spacing=6)
        text.append(self._used)

        actions = Adw.WrapBox(child_spacing=14, line_spacing=8, margin_top=6)
        self._play = Gtk.Button(child=Adw.ButtonContent(icon_name="media-playback-start-symbolic", label="Play now"))
        self._play.add_css_class("pill")
        self._play.add_css_class("suggested-action")
        self._play.add_css_class("pl-play")
        self._play.connect("clicked", lambda *_: self.play())
        actions.append(self._play)
        self._status = Gtk.Box(spacing=7, valign=Gtk.Align.CENTER)
        dot = Gtk.Box(valign=Gtk.Align.CENTER)
        dot.add_css_class("status-dot")
        self._status.append(dot)
        self._status_label = Gtk.Label(xalign=0, ellipsize=3)
        self._status_label.add_css_class("pl-status")
        self._status.append(self._status_label)
        actions.append(self._status)
        text.append(actions)
        identity.append(text)
        self._hero.append(identity)

        # Playback settings for this playlist.
        settings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.START)
        settings.set_size_request(300, -1)
        label = Gtk.Label(label="PLAYBACK", xalign=0)
        label.add_css_class("section-label")
        settings.append(label)
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        self._interval = Adw.ComboRow(title="Change", model=Gtk.StringList.new([text for _, text in INTERVALS]))
        self._interval.set_tooltip_text("How often the next wallpaper comes on")
        self._interval.connect("notify::selected", self._on_interval)
        group.append(self._interval)
        self._shuffle = Adw.SwitchRow(title="Shuffle", subtitle="Random order")
        self._shuffle.connect("notify::active", self._on_shuffle)
        group.append(self._shuffle)
        settings.append(group)
        self._hero.append(settings)
        return self._hero

    # -- building: entries ---------------------------------------------------------
    def _build_entries(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        # The right end stays free for the Select pill hanging over the list's corner.
        head = Gtk.Box(spacing=8, margin_end=ui.CORNER_RESERVE + 10)
        heading = Gtk.Label(label="Wallpapers", xalign=0)
        heading.add_css_class("title-4")
        head.append(heading)
        self._count = ui.pill("0", None, "subtle")
        self._count.add_css_class("pl-count")
        self._count.set_valign(Gtk.Align.CENTER)
        head.append(self._count)
        head.append(Gtk.Box(hexpand=True))
        self._density = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self._density.add_css_class("flat")
        self._density.add(Adw.Toggle(name="large", icon_name="view-list-symbolic", tooltip="Large rows"))
        self._density.add(Adw.Toggle(name="compact", icon_name="view-list-bullet-symbolic", tooltip="Compact rows"))
        self._density.set_active_name("large")
        self._density.connect("notify::active-name", self._on_density)
        head.append(self._density)
        self._add_inline_content = Adw.ButtonContent(icon_name="list-add-symbolic", label="Add wallpapers…")
        self._add_inline = Gtk.Button(child=self._add_inline_content)
        self._add_inline.set_tooltip_text("Pick several wallpapers to add")
        self._add_inline.connect("clicked", lambda *_: self.open_picker())
        head.append(self._add_inline)
        box.append(head)

        # The ordered list: rows lift and roll (reorder.ReorderList). The drop
        # line marks where a wallpaper dragged in from the Library will land.
        self._list = reorder.ReorderList()
        self._list.add_css_class("pl-entries")
        self._list.connect("row-activated", self._on_row_activated)
        self._list.connect("order-changed", lambda *_: self._renumber())
        self._list.connect("reordered", self._on_reordered)
        self._list.connect("settled", self._on_settled)
        self._drop_line = Gtk.Box(valign=Gtk.Align.START, can_target=False, visible=False)
        self._drop_line.add_css_class("pl-drop-line")
        dot = Gtk.Box(valign=Gtk.Align.CENTER)
        dot.add_css_class("pl-drop-dot")
        bar = Gtk.Box(hexpand=True, valign=Gtk.Align.CENTER)
        bar.add_css_class("pl-drop-bar")
        self._drop_line.append(dot)
        self._drop_line.append(bar)
        self._list_wrap = Gtk.Overlay(child=self._list)
        self._list_wrap.add_css_class("pl-list-wrap")
        self._list_wrap.add_overlay(self._drop_line)
        self._attach_drop(self._list_wrap, positional=True)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        list_box.append(self._list_wrap)
        self._hint = Gtk.Label(label="Drag a row up or down to reorder · Alt+↑ / Alt+↓ with the keyboard", xalign=0)
        self._hint.add_css_class("dimmed")
        self._hint.add_css_class("caption")
        self._hint.set_margin_start(4)
        list_box.append(self._hint)

        # Empty: one friendly drop zone.
        self._empty = Adw.StatusPage(
            icon_name="view-list-symbolic",
            title="No wallpapers yet",
            description="Add wallpapers, or drag them here from the Library",
        )
        self._empty.add_css_class("compact")
        self._empty.add_css_class("pl-drop-zone")
        add = Gtk.Button(label="Add wallpapers…", halign=Gtk.Align.CENTER)
        add.add_css_class("pill")
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda *_: self.open_picker())
        self._empty.set_child(add)
        self._attach_drop(self._empty, positional=False)

        # Automatic playlist: a read-only grid of everything.
        auto = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        note = Gtk.Box(spacing=10)
        note.add_css_class("pl-auto-note")
        icon = Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic")
        icon.add_css_class("dimmed")
        note.append(icon)
        self._auto_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        note.append(self._auto_label)
        duplicate = Gtk.Button(label="Duplicate to edit", valign=Gtk.Align.CENTER)
        duplicate.set_tooltip_text("Make your own copy you can reorder")
        duplicate.connect("clicked", lambda *_: self.duplicate())
        note.append(duplicate)
        auto.append(note)
        self._grid = CardGrid(min_width=176, max_columns=6, valign=Gtk.Align.START)
        auto.append(self._grid)

        self._area = Gtk.Stack(vhomogeneous=False, hhomogeneous=False)
        self._area.add_named(list_box, "list")
        self._area.add_named(self._empty, "empty")
        self._area.add_named(auto, "auto")
        box.append(self._area)
        hanging = Gtk.Overlay(child=box)
        ui.hang_on_corner(hanging, self._list_wrap, self._select, inset=14)
        return hanging

    def _build_selection_bar(self) -> Gtk.Widget:
        self._action_bar = Gtk.ActionBar(revealed=False)
        select_all = Gtk.Button(label="Select all")
        select_all.add_css_class("flat")
        select_all.connect("clicked", lambda *_: self.select_all())
        self._action_bar.pack_start(select_all)
        self._selection_label = Gtk.Label(ellipsize=3)
        self._action_bar.set_center_widget(self._selection_label)
        self._sel_remove = Gtk.Button(label="Remove")
        self._sel_remove.add_css_class("destructive-action")
        self._sel_remove.connect("clicked", lambda *_: self.remove_entries(sorted(self._selected)))
        self._sel_top = Gtk.Button(label="Move to top")
        self._sel_top.connect("clicked", lambda *_: self._move_selected_to_top())
        self._action_bar.pack_end(self._sel_remove)
        self._action_bar.pack_end(self._sel_top)
        return self._action_bar

    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        for name, callback in (
            ("rename", self.start_rename),
            ("duplicate", self.duplicate),
            ("delete", self.confirm_delete),
            ("add", self.open_picker),
            ("play", self.play),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=callback: cb())
            group.add_action(action)
        for widget in (self.widget, self._menu, self._add_header):
            widget.insert_action_group("pl", group)

    # -- rendering -------------------------------------------------------------------
    def _render(self) -> None:
        if not self._alive():
            return
        self._render_hero()
        self._render_entries()

    def _render_hero(self) -> None:
        playlist = self.playlist
        automatic = bool(playlist.automatic)
        self.set_title(playlist.name, "Automatic playlist" if automatic else "Playlist")
        size = 96 if self._narrow else 128
        if playlist.entries:
            cover = ui.Thumb(art.mosaic(data.cover_keys(playlist), 256), size, size, radius=16)
            cover.add_css_class("pl-cover")
        else:
            cover = Gtk.Image.new_from_icon_name("view-list-symbolic")
            cover.set_pixel_size(40)
            cover.set_size_request(size, size)
            cover.add_css_class("pl-cover-empty")
        self._cover.set_child(cover)

        self._name.set_visible(not automatic)
        self._rename.set_visible(not automatic)
        self._name_static.set_visible(automatic)
        if not self._name.get_editing():
            self._name.set_text(playlist.name)
        self._name_static.set_label(playlist.name)
        self._summary.set_label(summary(playlist))

        self._building = True
        values = [minutes for minutes, _ in INTERVALS]
        index = values.index(playlist.interval) if playlist.interval in values else values.index(30)
        if self._interval.get_selected() != index:
            self._interval.set_selected(index)
        if self._shuffle.get_active() != playlist.shuffle:
            self._shuffle.set_active(playlist.shuffle)
        self._building = False

        self._play.set_sensitive(bool(playlist.entries))
        self._play.set_tooltip_text(
            "Show this playlist on all displays until you resume the schedule"
            if playlist.entries
            else "Add wallpapers first"
        )
        self._render_used()
        self._render_status()

        # Header contributions follow what this playlist allows.
        self._sync_header_add()
        self._select.set_visible(not automatic and bool(playlist.entries))
        self._menu_model.remove_all()
        edit = Gio.Menu()
        if not automatic:
            edit.append("Rename", "pl.rename")
        edit.append("Duplicate", "pl.duplicate")
        self._menu_model.append_section(None, edit)
        if not automatic:
            danger = Gio.Menu()
            danger.append("Delete…", "pl.delete")
            self._menu_model.append_section(None, danger)

    def _chip(self, label: str, icon: str, target: str, tooltip: str, off: bool = False) -> Gtk.Button:
        chip = Gtk.Button(child=Adw.ButtonContent(icon_name=icon, label=label, can_shrink=True))
        chip.add_css_class("chip")
        chip.add_css_class("pl-chip")
        if off:
            chip.add_css_class("off")
        chip.set_tooltip_text(tooltip)
        chip.connect("clicked", lambda *_: self.state.navigate(target))
        return chip

    def _render_used(self) -> None:
        playlist, state = self.playlist, self.state
        _clear(self._used)
        chips: list[Gtk.Widget] = []
        for rule in state.rules:
            if rule.playlist == playlist.id:
                text = describe_rule(rule)
                chips.append(
                    self._chip(
                        text + ("" if rule.enabled else " · off"),
                        "x-office-calendar-symbolic",
                        "schedule",
                        "Schedule rule — open the Schedule"
                        if rule.enabled
                        else "This rule is turned off — open the Schedule",
                        off=not rule.enabled,
                    )
                )
        if state.fallback == playlist.id:
            chips.append(
                self._chip(
                    "When nothing is scheduled",
                    "x-office-calendar-symbolic",
                    "schedule",
                    "Plays whenever no schedule rule applies",
                )
            )
        for connector, assigned in state.assigned.items():
            if assigned == playlist.id:
                chips.append(
                    self._chip(
                        connector,
                        "video-display-symbolic",
                        "displays",
                        f"{connector} plays this playlist — open Displays",
                    )
                )
        lead = Gtk.Label(label="Used by" if chips else "Not in the schedule", valign=Gtk.Align.CENTER)
        lead.add_css_class("dimmed")
        lead.add_css_class("pl-used-label")
        self._used.append(lead)
        for chip in chips:
            self._used.append(chip)
        if not chips:
            link = Gtk.Button(label="Add to schedule…")
            link.add_css_class("flat")
            link.add_css_class("chip")
            link.add_css_class("pl-chip")
            link.set_tooltip_text("Open the Schedule to choose when it plays")
            link.connect("clicked", lambda *_: self.state.navigate("schedule"))
            self._used.append(link)

    def _render_status(self) -> None:
        state, pid = self.state, self._pid
        playing = [c for c in state.connectors() if state.effective_playlist(c) == pid]
        if not playing or not state.service_running:
            self._status.set_visible(False)
            return
        everywhere = len(playing) == len(state.connectors()) or state.display_mode == "mirrored"
        where = "On screen now" if everywhere else f"On {' & '.join(playing)} now"
        if any(c in state.manual for c in playing):
            why = "your pick"
        elif all(state.assigned.get(c) == pid for c in playing):
            why = "assigned"
        else:
            why = "from schedule"
        self._status_label.set_label(f"{where} · {why}")
        self._status.set_visible(True)

    def _render_entries(self) -> None:
        playlist, state = self.playlist, self.state
        automatic = bool(playlist.automatic)
        count = len(playlist.entries)
        self._count.get_last_child().set_label(str(count))
        self._density.set_visible(not automatic and count > 0 and not self._narrow)
        self._add_inline_content.set_label("Add…" if self._narrow else "Add wallpapers…")
        self._add_inline.set_visible(not automatic)
        if automatic:
            self._render_grid()
            self._area.set_visible_child_name("auto")
            return
        if self._list.dragging_by_hand:
            self._render_deferred = True  # never pull rows out from under the pointer
            return
        if self._list.flush():
            return  # committing the pending move already re-rendered
        focused = self._focused_index()
        if focused is not None and self._pending_focus is None:
            self._pending_focus = min(focused, max(0, count - 1))
        for row in self._rows:
            row.release()
        self._list.remove_all()
        self._rows = []
        self._render_deferred = False
        self._drop_line.set_visible(False)
        self._drag_from = None
        self._selected = {i for i in self._selected if i < count}
        if not count:
            if self._selecting:
                self._select.set_active(False)
            self._area.set_visible_child_name("empty")
            return
        self._area.set_visible_child_name("list")
        on_screen: dict[str, list[str]] = {}
        for connector in state.connectors():
            on_screen.setdefault(state.current[connector], []).append(connector)
        positions: dict[str, list[int]] = {}
        for index, wid in enumerate(playlist.entries):
            positions.setdefault(wid, []).append(index)
        badge_done: set[str] = set()
        for index, wid in enumerate(playlist.entries):
            if wid not in data.BY_ID:
                continue
            screens = on_screen.get(wid, []) if wid not in badge_done else []
            badge_done.add(wid)
            row = EntryRow(
                self,
                index,
                wid,
                total=count,
                on_screen=screens,
                repeats=[i for i in positions[wid] if i != index],
                compact=self._compact,
                narrow=self._narrow,
                selecting=self._selecting,
                selected=index in self._selected,
            )
            self._list.append(row, row.handle)
            self._rows.append(row)
        classes = ["boxed-list", "reorder-list", "pl-entries"] + (["pl-compact"] if self._compact else [])
        self._list.set_css_classes(classes)
        self._hint.set_visible(count > 1 and not self._selecting)
        self._update_selection_bar()
        self._apply_pending()

    def _focused_index(self) -> int | None:
        root = self.widget.get_root()
        widget = root.get_focus() if root else None
        while widget is not None and not isinstance(widget, EntryRow):
            widget = widget.get_parent()
        return widget.index if isinstance(widget, EntryRow) and widget in self._rows else None

    def _render_grid(self) -> None:
        self._grid.remove_all()
        playlist, state = self.playlist, self.state
        self._auto_label.set_label(playlist.automatic + ".")
        playing: dict[str, list[str]] = {}
        for connector, wid in state.current.items():
            playing.setdefault(wid, []).append(connector)
        for wid in playlist.entries:
            card = ui.WallpaperCard(
                data.BY_ID[wid],
                width=168,
                on_open=lambda w: self.state.navigate(f"library:{w.id}"),
                on_apply=lambda w: self.state.apply(w.id, self.state.scope),
                on_favorite=lambda w: self.state.toggle_favorite(w.id),
                **ui.card_colors(self.state, data.BY_ID[wid]),
            )
            screens = playing.get(wid)
            if screens:
                where = "On screen" if len(screens) > 1 else f"On {screens[0]}"
                badge = ui.pill(where, "media-playback-start-symbolic", "accent")
                badge.set_valign(Gtk.Align.END)
                badge.set_margin_start(8)
                badge.set_margin_bottom(8)
                card.frame.add_overlay(badge)
            card.set_tooltip_text(f"{data.BY_ID[wid].name} — open in the Library")
            self._grid.append(card)

    def _apply_pending(self) -> None:
        focus, flash, scroll = self._pending_focus, set(self._pending_flash), self._pending_scroll
        self._pending_focus, self._pending_flash, self._pending_scroll = None, set(), None
        rows = self._rows
        for index in flash:
            if index < len(rows):
                rows[index].add_css_class("pl-flash")
        if flash:
            GLib.timeout_add(700, self._unflash)
        if focus is None and scroll is None:
            return

        def settle() -> bool:
            if focus is not None and focus < len(self._rows):
                self._rows[focus].grab_focus()
            target = scroll if scroll is not None else focus
            if target is not None and target < len(self._rows):
                viewport = self._scroller.get_child()
                if isinstance(viewport, Gtk.Viewport):
                    viewport.scroll_to(self._rows[target], None)
            return False

        GLib.timeout_add(60, settle)

    def _unflash(self) -> bool:
        for row in self._rows:
            row.remove_css_class("pl-flash")
        return False

    def _set_narrow(self, narrow: bool) -> None:
        self._narrow = narrow
        self._render()

    # -- callbacks -------------------------------------------------------------------
    def _on_changed(self, _state, topic: str) -> None:
        if not self._alive():
            return
        if topic == "playlists":
            self._render()
        elif topic in ("now", "library", "system"):
            self._render_status()
            self._render_entries()
        elif topic in ("schedule", "displays"):
            self._render_used()
            self._render_status()

    def _on_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        self._title_reveal.set_reveal_child(adjustment.get_value() > 70)
        self._sync_header_add()

    def _sync_header_add(self) -> None:
        """The header "+" stands in for "Add wallpapers…" only while that is scrolled away."""
        if self._pid not in data.PLAYLIST_BY_ID or self.playlist.automatic:
            self._add_header.set_visible(False)
            return
        # Measure in content coordinates: they don't move when scrolling, while
        # positions relative to the scroller only update at the next layout.
        content = self._scroller.get_child().get_child()  # inside the viewport, which scrolls it
        ok, bounds = self._add_inline.compute_bounds(content)
        scrolled = self._scroller.get_vadjustment().get_value()
        self._add_header.set_visible(ok and bounds.get_y() + bounds.get_height() < scrolled)

    def _on_interval(self, row: Adw.ComboRow, _param) -> None:
        if self._building or not self._alive():
            return
        self.playlist.interval = INTERVALS[row.get_selected()][0]
        self._summary.set_label(summary(self.playlist))
        self.state.emit_changed("playlists", "playback")

    def _on_shuffle(self, row: Adw.SwitchRow, _param) -> None:
        if self._building or not self._alive():
            return
        self.playlist.shuffle = row.get_active()
        self._summary.set_label(summary(self.playlist))
        self.state.emit_changed("playlists", "playback")

    def _on_density(self, group: Adw.ToggleGroup, _param) -> None:
        self._compact = group.get_active_name() == "compact"
        if self._alive():
            self._render_entries()

    # -- playlist actions ------------------------------------------------------------
    def play(self) -> None:
        if self._alive() and self.playlist.entries:
            self.state.play_playlist(self._pid)

    def play_from(self, index: int) -> None:
        wid = self.playlist.entries[index]
        self.state.play_playlist(self._pid)
        for connector in self.state.targets("all"):
            self.state.current[connector] = wid
        self.state.emit_changed("now")

    def start_rename(self) -> None:
        if self._alive() and not self.playlist.automatic:
            self._scroller.get_vadjustment().set_value(0)
            self._name.start_editing()
            self._name.grab_focus()

    def _fit_name(self, label: Gtk.EditableLabel) -> None:
        # Size the field to the name so the pencil sits right after it.
        chars = max(4, min(32, len(label.get_text()) + 1))
        label.set_width_chars(min(chars, 6))  # minimum: may shrink at narrow widths
        label.set_max_width_chars(chars)  # natural: hugs the name

    def _on_name_editing(self, label: Gtk.EditableLabel, _param) -> None:
        if label.get_editing():
            self._editing_pid = self._pid
            return
        pid, self._editing_pid = self._editing_pid or self._pid, None
        playlist = data.PLAYLIST_BY_ID.get(pid)
        if playlist is None:
            return
        new, old = label.get_text().strip(), playlist.name
        if not new or new == old:
            if pid == self._pid:
                label.set_text(old)
            return
        playlist.name = new
        self.state.emit_changed("playlists")

        def undo() -> None:
            playlist.name = old
            self.state.emit_changed("playlists")

        self.state.toast(f"Renamed to “{new}”", undo)

    def duplicate(self) -> None:
        if not self._alive():
            return
        source, playlists = self.playlist, self.state.playlists
        base = f"{source.name} (copy)"
        name, number = base, 2
        names = {p.name for p in playlists}
        while name in names:
            name, number = f"{source.name} (copy {number})", number + 1
        pid, number = f"{source.id}-copy", 2
        while pid in data.PLAYLIST_BY_ID:
            pid, number = f"{source.id}-copy-{number}", number + 1
        copy = Playlist(pid, name, list(source.entries), interval=source.interval, shuffle=source.shuffle)
        user_count = len([p for p in playlists if not p.automatic])
        position = playlists.index(source) + 1 if not source.automatic else user_count
        playlists.insert(position, copy)
        data.PLAYLIST_BY_ID[pid] = copy
        self.state.navigate(f"playlist:{pid}")
        self.state.emit_changed("playlists")
        source_id = source.id

        def undo() -> None:
            if copy in playlists:
                playlists.remove(copy)
            data.PLAYLIST_BY_ID.pop(pid, None)
            if self._pid == pid:
                self.state.navigate(f"playlist:{source_id}")
            self.state.emit_changed("playlists")

        self.state.toast(f"Duplicated as “{name}”", undo)
        GLib.timeout_add(150, lambda: (self._pid == pid and self.start_rename(), False)[1])

    def confirm_delete(self) -> None:
        if not self._alive() or self.playlist.automatic:
            return
        playlist, state = self.playlist, self.state
        rules = [rule for rule in state.rules if rule.playlist == playlist.id]
        screens = [c for c, p in state.assigned.items() if p == playlist.id]
        body = ["Its wallpapers stay in your Library."]
        if rules:
            body.append(
                "1 schedule rule uses it and will be removed too."
                if len(rules) == 1
                else f"{len(rules)} schedule rules use it and will be removed too."
            )
        if screens:
            body.append(f"{' and '.join(screens)} will follow the schedule instead.")
        dialog = Adw.AlertDialog(heading=f"Delete “{playlist.name}”?", body=" ".join(body))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "delete" and self._delete())
        dialog.present(self.widget)
        self._dialog = dialog

    def _delete(self) -> None:
        playlist, state = self.playlist, self.state
        pid, playlists = playlist.id, state.playlists
        position = playlists.index(playlist)
        rules = [(i, rule) for i, rule in enumerate(state.rules) if rule.playlist == pid]
        assigned, manual = dict(state.assigned), dict(state.manual)
        for index, _rule in reversed(rules):
            del state.rules[index]
        for connector, value in state.assigned.items():
            if value == pid:
                state.assigned[connector] = ""
        for connector in [c for c, p in state.manual.items() if p == pid]:
            del state.manual[connector]
        playlists.remove(playlist)
        data.PLAYLIST_BY_ID.pop(pid, None)
        users = [p for p in playlists if not p.automatic]
        neighbor = users[min(position, len(users) - 1)] if users else None
        state.navigate(f"playlist:{neighbor.id}" if neighbor else "library")
        state.emit_changed("playlists", "schedule", "displays", "now", "playback")

        def undo() -> None:
            playlists.insert(min(position, len(playlists)), playlist)
            data.PLAYLIST_BY_ID[pid] = playlist
            for index, rule in rules:
                state.rules.insert(min(index, len(state.rules)), rule)
            state.assigned.clear()
            state.assigned.update(assigned)
            state.manual.clear()
            state.manual.update(manual)
            state.navigate(f"playlist:{pid}")
            state.emit_changed("playlists", "schedule", "displays", "now", "playback")

        state.toast(f"Deleted “{playlist.name}”", undo)

    # -- entry actions -----------------------------------------------------------------
    def position(self, row: EntryRow) -> int:
        """Where ``row`` is now, counting moves still rolling into place."""
        rows = self._list.get_rows()
        return rows.index(row) if row in rows else row.index

    def move_row(self, row: EntryRow, target: int) -> None:
        """Roll ``row`` to ``target``; the playlist changes once the rows settle."""
        self._list.move(row, target)

    def move_entry(self, index: int, target: int, flash: bool = False) -> None:
        rows = self._list.get_rows()
        if 0 <= index < len(rows):
            self.move_row(rows[index], target)

    def _renumber(self) -> None:
        for position, row in enumerate(self._list.get_rows()):
            row.number.set_label(str(position + 1))

    def _on_reordered(self, _list, row: EntryRow) -> None:
        """Commit a drag or keyboard move once its rows have settled."""
        if not self._alive():
            return
        rows = self._list.get_rows()
        self.playlist.entries[:] = [r.wid for r in rows]
        if self._selecting:
            self._selected.clear()
        target = rows.index(row)
        self._pending_focus = target
        self._pending_flash = {target}
        self.state.emit_changed("playlists")

    def _on_settled(self, _list) -> None:
        if self._render_deferred and self._alive():
            self._render()

    def remove_entries(self, indices: list[int]) -> None:
        self._list.flush()  # indices count rolled moves; commit them first
        playlist = self.playlist
        indices = sorted(i for i in set(indices) if 0 <= i < len(playlist.entries))
        if not indices:
            return
        removed = [(i, playlist.entries[i]) for i in indices]
        for index, _wid in reversed(removed):
            del playlist.entries[index]
        remaining = len(playlist.entries)
        if remaining:
            self._pending_focus = min(indices[0], remaining - 1)
        self._selected.clear()
        if self._selecting and remaining:
            self._select.set_active(False)
        self.state.emit_changed("playlists")
        what = f"“{data.BY_ID[removed[0][1]].name}”" if len(removed) == 1 else f"{len(removed)} wallpapers"

        def undo() -> None:
            for index, wid in removed:
                playlist.entries.insert(min(index, len(playlist.entries)), wid)
            if self._pid == playlist.id:
                self._pending_flash = {index for index, _ in removed}
                self._pending_scroll = removed[0][0]
            self.state.emit_changed("playlists")

        self.state.toast(f"Removed {what} from “{playlist.name}”", undo)

    def insert_entries(self, position: int, wids: list[str]) -> None:
        self._list.flush()
        playlist = self.playlist
        wids = [wid for wid in wids if wid in data.BY_ID]
        if not wids:
            return
        position = max(0, min(len(playlist.entries), position))
        if position == len(playlist.entries):
            self.add_entries(wids)
            return
        playlist.entries[position:position] = wids
        self._pending_flash = set(range(position, position + len(wids)))
        self._pending_scroll = position
        self.state.emit_changed("playlists")
        what = f"“{data.BY_ID[wids[0]].name}”" if len(wids) == 1 else f"{len(wids)} wallpapers"

        def undo() -> None:
            del playlist.entries[position : position + len(wids)]
            self.state.emit_changed("playlists")

        self.state.toast(f"Added {what} to “{playlist.name}”", undo)

    def add_entries(self, wids: list[str]) -> None:
        """Append (the picker, drops on the empty state). state.add_to_playlist toasts with Undo."""
        if not wids or not self._alive():
            return
        self._list.flush()
        start = len(self.playlist.entries)
        self._pending_flash = set(range(start, start + len(wids)))
        self._pending_scroll = start + len(wids) - 1
        self.state.add_to_playlist(self._pid, wids)

    def open_picker(self, preselect: list[str] | None = None) -> PlaylistPicker | None:
        if not self._alive() or self.playlist.automatic:
            return None
        self._picker = picker = PlaylistPicker(self.state, self.playlist, self.add_entries)
        picker.connect("closed", lambda *_: self._picker is picker and setattr(self, "_picker", None))
        if preselect:
            self._picker.pick(preselect)
        self._picker.present(self.widget)
        return self._picker

    # -- selection mode ------------------------------------------------------------------
    def _on_select_mode(self, button: Gtk.ToggleButton) -> None:
        self._selecting = button.get_active()
        self._selected.clear()
        if self._alive():
            self._render_entries()
        self._update_selection_bar()

    def set_entry_selected(self, index: int, selected: bool) -> None:
        (self._selected.add if selected else self._selected.discard)(index)
        self._update_selection_bar()

    def select_all(self) -> None:
        self._selected = set(range(len(self.playlist.entries)))
        for row in self._rows:
            if row.check:
                row.check.set_active(True)
        self._update_selection_bar()

    def _on_row_activated(self, _list, row) -> None:
        if self._selecting and isinstance(row, EntryRow) and row.check:
            row.check.set_active(not row.check.get_active())

    def _update_selection_bar(self) -> None:
        count = len(self._selected)
        self._selection_label.set_label("Pick rows to change" if not count else f"{count} selected")
        self._sel_remove.set_sensitive(bool(count))
        self._sel_top.set_sensitive(bool(count))
        self._action_bar.set_revealed(self._selecting)

    def _move_selected_to_top(self) -> None:
        self._list.flush()
        entries = self.playlist.entries
        chosen = sorted(self._selected)
        if not chosen:
            return
        picked = [entries[i] for i in chosen]
        rest = [wid for i, wid in enumerate(entries) if i not in self._selected]
        entries[:] = picked + rest
        self._pending_flash = set(range(len(picked)))
        self._pending_scroll = 0
        self._select.set_active(False)
        self.state.emit_changed("playlists")

    def _on_escape(self, *_args) -> bool:
        if self._selecting:
            self._select.set_active(False)
            return True
        return False

    def _on_select_all_key(self, *_args) -> bool:
        if self._selecting:
            self.select_all()
            return True
        return False

    # -- drag and drop -------------------------------------------------------------------
    def drag_begin(self, row: EntryRow, source: Gtk.DragSource, drag: Gdk.Drag) -> None:
        self._drag_from = row.index
        row.add_css_class("pl-dragging")
        icon = Gtk.DragIcon.get_for_drag(drag)
        icon.set_child(self._drag_card(row.wallpaper))
        drag.set_hotspot(24, 20)

    def drag_end(self) -> None:
        self._drag_from = None
        for row in self._rows:
            row.remove_css_class("pl-dragging")
        self._end_drop_feedback()

    @staticmethod
    def _drag_card(wallpaper: Wallpaper) -> Gtk.Widget:
        card = Gtk.Box(spacing=10)
        card.add_css_class("pl-drag-icon")
        card.append(ui.thumbnail(wallpaper, 64, 36, 6))
        card.append(Gtk.Label(label=wallpaper.name))
        return card

    def _attach_drop(self, widget: Gtk.Widget, positional: bool) -> None:
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        if positional:
            target.connect("enter", self._on_drop_motion)
            target.connect("motion", self._on_drop_motion)
            target.connect("drop", self._on_drop)
        else:
            target.connect("enter", lambda *_: (widget.add_css_class("pl-drop-active"), Gdk.DragAction.COPY)[1])
            target.connect("drop", lambda _t, value, _x, _y: self._on_append_drop(value))
        target.connect("leave", lambda *_: self._end_drop_feedback())
        widget.add_controller(target)

    def _drop_position(self, y: float) -> int:
        if not self._rows:
            return 0
        row = self._list.get_row_at_y(int(y))
        if not isinstance(row, EntryRow):
            return 0 if y <= 0 else len(self._rows)
        ok, rect = row.compute_bounds(self._list)
        middle = rect.get_y() + rect.get_height() / 2 if ok else y
        return row.index + (1 if y > middle else 0)

    def _show_drop_line(self, position: int) -> None:
        self._drop_at = position
        if not self._rows:
            return
        if position < len(self._rows):
            ok, rect = self._rows[position].compute_bounds(self._list)
            y = rect.get_y() if ok else 0
        else:
            ok, rect = self._rows[-1].compute_bounds(self._list)
            y = rect.get_y() + rect.get_height() if ok else 0
        self._drop_line.set_margin_top(max(0, int(y) - 5))
        self._drop_line.set_visible(True)

    def _on_drop_motion(self, _target, x: float, y: float) -> Gdk.DragAction:
        if self._drag_from is not None:
            return 0  # one of our own rows: reordering is the list's own drag
        self._show_drop_line(self._drop_position(y))
        ok, point = self._list_wrap.compute_point(self._scroller, Graphene.Point().init(x, y))
        if ok:
            self._pointer = (point.x, point.y)
            self._autoscroll(point.y)
        return Gdk.DragAction.COPY

    def _autoscroll(self, y: float) -> None:
        height, edge = self._scroller.get_height(), 56
        if y < edge:
            self._scroll_speed = -14 * (edge - y) / edge
        elif y > height - edge:
            self._scroll_speed = 14 * (y - (height - edge)) / edge
        else:
            self._scroll_speed = 0
        if self._scroll_speed and not self._scroll_source:
            self._scroll_source = GLib.timeout_add(16, self._scroll_tick)

    def _scroll_tick(self) -> bool:
        if not self._scroll_speed:
            self._scroll_source = 0
            return False
        adjustment = self._scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_value() + self._scroll_speed)
        if self._pointer:
            ok, point = self._scroller.compute_point(self._list_wrap, Graphene.Point().init(*self._pointer))
            if ok and self._drag_from is None:
                self._show_drop_line(self._drop_position(point.y))
        return True

    def _end_drop_feedback(self) -> None:
        self._drop_line.set_visible(False)
        self._empty.remove_css_class("pl-drop-active")
        self._scroll_speed = 0
        self._pointer = None

    def _on_drop(self, _target, value, _x: float, y: float) -> bool:
        position = self._drop_position(y)
        self._end_drop_feedback()
        if self._drag_from is not None:
            return False
        wid = value if isinstance(value, str) else ""
        if wid not in data.BY_ID:
            return False
        self.insert_entries(position, [wid])
        return True

    def _on_append_drop(self, value) -> bool:
        self._end_drop_feedback()
        wid = value if isinstance(value, str) else ""
        if wid not in data.BY_ID:
            return False
        self.add_entries([wid])
        return True

    # -- demo -------------------------------------------------------------------------------
    def demo(self, scene: str) -> None:
        what, _, arg = scene.partition(":")
        if what == "picker":
            self.open_picker(["northern-lights", "moon-bay", "lily-pond"])
        elif what == "picker-search":
            picker = self.open_picker(["frog-dusk"])
            if picker:
                picker.search.set_text(arg or "pond")
        elif what == "empty":
            self._demo_empty(arg or "Road trip")
        elif what == "menu":
            index = max(0, int(arg or "1") - 1)

            def open_menu() -> bool:
                row = self._rows[min(index, len(self._rows) - 1)]
                row.popup_context(row.get_width() - 250, row.get_height() / 2)
                return False

            GLib.timeout_add(300, open_menu)
        elif what == "playlist-menu":
            GLib.timeout_add(300, lambda: (self._menu.popup(), False)[1])
        elif what == "drag":
            self._demo_drag(*(int(part) for part in (arg or "6,2").split(",")))
        elif what == "select":
            self._select.set_active(True)
            for index in (1, 3, 4):
                if index < len(self._rows) and self._rows[index].check:
                    self._rows[index].check.set_active(True)
        elif what == "compact":
            self._density.set_active_name("compact")
        elif what == "rename":
            GLib.timeout_add(200, lambda: (self.start_rename(), False)[1])
        elif what == "delete":
            self.confirm_delete()
        elif what == "added":
            self.add_entries(["northern-lights", "moon-bay"])
        elif what == "removed":
            self.remove_entries([2])
        elif what == "scrolled":
            GLib.timeout_add(300, lambda: (self._scroller.get_vadjustment().set_value(int(arg or 400)), False)[1])
        elif what == "long-name":
            self.playlist.name = arg or "Rainy evenings by the old harbor"
            self.state.emit_changed("playlists")
        elif what == "assigned":
            self.state.assigned["HDMI-A-1"] = self._pid
            self.state.emit_changed("displays")

    def _demo_empty(self, name: str) -> None:
        pid = name.lower().replace(" ", "-")
        if pid not in data.PLAYLIST_BY_ID:
            playlist = Playlist(pid, name, [])
            self.state.playlists.insert(len([p for p in self.state.playlists if not p.automatic]), playlist)
            data.PLAYLIST_BY_ID[pid] = playlist
            self.state.emit_changed("playlists")
        self.state.navigate(f"playlist:{pid}")

    def _demo_drag(self, source: int, target: int) -> None:
        """Freeze a reorder mid-drag: row ``source`` (1-based) lifted and carried up
        to just above row ``target``'s middle, the rows between rolled down."""

        def show() -> bool:
            rows = self._list.get_rows()
            src, dst = source - 1, target - 1
            if not (0 <= src < len(rows) and 0 <= dst < len(rows)) or self._list.busy:
                return False
            heights = self._list.row_heights()
            tops = [sum(heights[:i]) for i in range(len(rows))]
            lifted = rows[src]
            self._list.begin_drag(lifted)
            # Carry its middle to just past row ``target``'s middle (above it when
            # moving up, below it when moving down) so they have traded places.
            lean = -0.12 if dst < src else 0.12
            middle = tops[dst] + heights[dst] * (0.5 + lean)
            self._list.update_drag(middle - (tops[src] + heights[src] / 2))
            return False

        GLib.timeout_add(250, show)


def create(state) -> Page:
    return PlaylistPage(state)
