"""Playlists: one playlist, where it is used, how it plays, and its ordered wallpapers.

The sidebar lists the playlists; this page shows the one chosen. Entries are
ordered and may repeat a wallpaper (each entry is its own row, with its own
id). Everything the page changes goes through `PlaylistEditing`
(`AppState.playlist_editing`); the page shows what that says is saved:

* **Reordering** is a handle-driven live list (`reorder.ReorderList`): drag a
  row by its handle, or press Ctrl+↑/↓ (Ctrl+Home/End for the ends) or use
  its menu. A drag shows where the row was dropped while the move is
  saved; a key or menu move shows when it is saved. Each is one relative
  move, queued in order (`PlaylistEditing.move_entry`).
* **Removing** (the row's button, Delete, or several in selection mode) is
  one save; once saved, a toast offers Undo and keyboard focus moves to a
  neighbour.
* **Adding** is the picker ("Add wallpapers…", several in one save) or a
  wallpaper dragged in from the Library, which lands where it is dropped.
* **Renaming** is the title, in place. **Duplicate** and **Delete…** are in
  the header menu; Delete says everything it removes with the playlist.
* **How it plays**: its own interval and shuffle, or the defaults.
* **Play now** and **Use as schedule default**, with the classic rules for
  when they are off.

Long playlists build 72 rows at a time ("Show more"). Automatic playlists
("All wallpapers") are a read-only grid with "Duplicate to edit". While
files from a newer version exist, the page says so and changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk, Pango

from wall_in_one.ui.next import widgets
from wall_in_one.ui.next.catalog import KIND_LABEL, quoted
from wall_in_one.ui.next.page import Page, not_ported
from wall_in_one.ui.next.playlists_picker import PlaylistPicker
from wall_in_one.ui.next.playlists_state import (
    EDITS_TOPIC,
    INTERVALS,
    EntryView,
    PlaylistDetail,
    PlaylistEditing,
    interval_words,
)
from wall_in_one.ui.next.reorder import ReorderList
from wall_in_one.ui.next.state import AppState, WallpaperView

#: Rows built at a time, like the classic editor: rows are real widget trees.
ENTRY_PAGE_SIZE: Final = 72
#: Cards of an automatic playlist built at a time.
GRID_PAGE_SIZE: Final = 72
#: The topics that can change what the page shows.
_TOPICS: Final = frozenset(
    {
        "playlists",
        EDITS_TOPIC,
        "library",
        "now",
        "system",
        "settings",
        "schedule",
        "displays",
    }
)

_CSS: Final = """
.pl-cover {
  border-radius: 16px;
  box-shadow: 0 1px 3px alpha(black, 0.20), 0 8px 20px alpha(black, 0.16);
}
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
.pl-entries > row.pl-selected { background-color: alpha(var(--accent-bg-color), 0.12); }
.pl-entries.pl-compact > row > box.header { min-height: 42px; }
.pl-number { font-feature-settings: "tnum"; font-weight: 700; font-size: 0.92em; opacity: 0.55; }
.pl-row-button { min-width: 32px; min-height: 32px; padding: 0; }
.pl-entries > row.pl-removing { opacity: 0.45; }
.pl-missing {
  border-radius: 8px; background-color: alpha(currentColor, 0.07);
  color: alpha(currentColor, 0.45);
}
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
.pl-list-wrap:drop(active), .pl-drop-zone:drop(active) { box-shadow: none; outline: none; }
.pl-drop-zone { border: 2px dashed alpha(currentColor, 0.16); border-radius: 16px; }
.pl-drop-zone.pl-drop-active {
  border-color: var(--accent-bg-color); background-color: alpha(var(--accent-bg-color), 0.08);
}
.pl-auto-note {
  padding: 10px 12px; border-radius: 12px; background-color: alpha(currentColor, 0.05);
}
"""
_styled = False


def summary(detail: PlaylistDetail) -> str:
    """The hero's subtitle: how many, and how many playback skips."""
    count = len(detail.entries)
    if not count:
        return "No wallpapers yet"
    text = "1 wallpaper · a fixed choice" if count == 1 else f"{count} wallpapers"
    if detail.unavailable:
        text += f" · {detail.unavailable} unavailable"
    return text


def _clear(container: Gtk.Box | Adw.WrapBox) -> None:
    while (child := container.get_first_child()) is not None:
        container.remove(child)


def _wrap_labels(widget: Gtk.Widget) -> None:
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label):
            child.set_wrap(True)
            child.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            child.set_xalign(0)
        _wrap_labels(child)
        child = child.get_next_sibling()


# ---------------------------------------------------------------------------
# One entry
# ---------------------------------------------------------------------------


class EntryRow(Adw.ActionRow):
    """An entry: handle · position · picture · name · status · remove · menu."""

    def __init__(
        self,
        page: PlaylistPage,
        entry: EntryView,
        *,
        compact: bool,
        narrow: bool,
        selecting: bool,
        selected: bool,
    ) -> None:
        super().__init__(use_markup=False, title_lines=1, subtitle_lines=1)
        self.page = page
        self.entry = entry
        #: What the row was built from; a different one rebuilds it.
        self.signature: tuple[object, ...] = (entry, compact, narrow, selecting)
        self.check: Gtk.CheckButton | None = None
        self.handle: Gtk.Widget | None = None
        self.menu_button: Gtk.MenuButton | None = None
        self._context: Gtk.PopoverMenu | None = None
        state = page.state
        wallpaper = (
            state.wallpaper(entry.wallpaper) if state.has_wallpaper(entry.wallpaper) else None
        )
        self.wallpaper: WallpaperView | None = wallpaper
        if entry.missing:
            self.set_title(f"Missing · {entry.name}")
            self.set_tooltip_text(
                f"{entry.wallpaper} isn\u2019t in the library now. The entry stays, in case "
                "the file comes back (a drive that isn\u2019t mounted)."
            )
        elif entry.problem:
            self.set_title(f"Playback unavailable · {entry.name}")
            self.set_tooltip_text(entry.problem)
        else:
            self.set_title(entry.name)
        self._compact = compact
        self._bits: list[str] = []
        if wallpaper is not None:
            self._bits.append(KIND_LABEL.get(wallpaper.kind, wallpaper.kind.capitalize()))
            if wallpaper.duration:
                self._bits.append(wallpaper.duration)

        # -- prefixes (add_prefix prepends, so right to left) ----------------------
        width, height = (56, 32) if compact else (80, 45) if narrow else (96, 54)
        radius = 6 if compact else 8
        if wallpaper is not None:
            picture: Gtk.Widget = widgets.thumbnail(wallpaper, width, height, radius)
        else:
            image = Gtk.Image.new_from_icon_name("image-missing-symbolic")
            image.set_size_request(width, height)
            image.add_css_class("pl-missing")
            picture = image
        picture.set_valign(Gtk.Align.CENTER)
        self.add_prefix(picture)
        self.number = Gtk.Label(xalign=1)
        self.number.add_css_class("pl-number")
        self.add_prefix(self.number)
        if selecting:
            self.check = Gtk.CheckButton(active=selected, valign=Gtk.Align.CENTER)
            self.check.update_property([Gtk.AccessibleProperty.LABEL], [f"Select {entry.name}"])
            self.check.connect("toggled", self._on_check)
            if selected:
                self.add_css_class("pl-selected")
            self.add_prefix(self.check)
            self.set_activatable_widget(self.check)
        else:
            # A passive picture, not a button: the list's gesture (on the
            # scrolled window) recognises a press on it and does the rest.
            handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
            handle.set_tooltip_text("Drag up or down to reorder (Ctrl+↑ / Ctrl+↓)")
            handle.update_property([Gtk.AccessibleProperty.LABEL], [f"Reorder {entry.name}"])
            self.add_prefix(handle)
            self.handle = handle

        # -- suffixes ------------------------------------------------------------------
        self._pills = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.add_suffix(self._pills)
        self._narrow = narrow
        if selecting:
            return
        remove = widgets.icon_button(
            "list-remove-symbolic",
            "Remove from playlist (Delete)",
            lambda *_: page.remove_entries([entry.id]),
            "flat",
            "circular",
            "pl-row-button",
        )
        remove.set_valign(Gtk.Align.CENTER)
        self.add_suffix(remove)
        self.menu_button = Gtk.MenuButton(
            icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, menu_model=self._menu_model()
        )
        for css in ("flat", "circular", "pl-row-button"):
            self.menu_button.add_css_class(css)
        self.menu_button.set_tooltip_text("More")
        self.menu_button.update_property([Gtk.AccessibleProperty.LABEL], [f"More for {entry.name}"])
        self.add_suffix(self.menu_button)
        self._actions = self._install_actions()
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        secondary = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        secondary.connect("pressed", lambda _g, _n, x, y: self.popup_context(x, y))
        self.add_controller(secondary)

    @property
    def entry_id(self) -> str:
        return self.entry.id

    def set_place(self, index: int, total: int) -> None:
        """Its position (1-based on screen) and which moves its menu offers."""
        self.number.set_label(str(index + 1))
        self.number.set_width_chars(max(1, len(str(total))))
        actions = getattr(self, "_actions", None)
        if isinstance(actions, Gio.SimpleActionGroup):
            for name, enabled in (
                ("top", index > 0),
                ("up", index > 0),
                ("down", index < total - 1),
                ("bottom", index < total - 1),
            ):
                action = actions.lookup_action(name)
                if isinstance(action, Gio.SimpleAction):
                    action.set_enabled(enabled)

    def set_status(self, on_screen: Sequence[str], repeats: Sequence[int]) -> None:
        """Where it is on screen now, and where else this wallpaper is in the list."""
        bits = list(self._bits)
        if repeats:
            bits.append("also at " + ", ".join(str(index + 1) for index in repeats))
        text = " · ".join(bits)
        if self._compact:
            self.set_subtitle("")
            if not self.entry.problem and not self.entry.missing:
                self.set_tooltip_text(" · ".join([self.entry.name, *bits]))
        else:
            self.set_subtitle(text)
        _clear(self._pills)
        if self.entry.problem:
            skipped = widgets.pill(
                "" if self._narrow else "Skipped", "dialog-warning-symbolic", "warning"
            )
            skipped.set_valign(Gtk.Align.CENTER)
            skipped.set_tooltip_text(
                f"Playback skips it: {self.entry.problem} \u2014 retry it from the Library"
            )
            self._pills.append(skipped)
        if on_screen:
            where = "On screen" if len(on_screen) > 1 else f"On {on_screen[0]}"
            badge = widgets.pill(
                "" if self._narrow else where, "media-playback-start-symbolic", "accent"
            )
            badge.set_valign(Gtk.Align.CENTER)
            badge.set_tooltip_text(f"Showing now on {' and '.join(on_screen)}")
            self._pills.append(badge)

    def set_removing(self, removing: bool) -> None:
        """Its removal is being saved: dimmed, its handle inert, focus kept."""
        if removing:
            self.add_css_class("pl-removing")
        else:
            self.remove_css_class("pl-removing")
        if self.handle is not None:
            self.handle.set_can_target(not removing)

    def _on_check(self, button: Gtk.CheckButton) -> None:
        if button.get_active():
            self.add_css_class("pl-selected")
        else:
            self.remove_css_class("pl-selected")
        self.page.set_entry_selected(self.entry.id, button.get_active())

    def popup_context(self, x: float, y: float) -> None:
        """Right-click: the same menu as ⋮, at the pointer."""
        if self.menu_button is None:
            return
        if self._context is None:
            model = self.menu_button.get_menu_model()
            if model is None:
                return
            self._context = Gtk.PopoverMenu.new_from_model(model)
            self._context.set_has_arrow(False)
            self._context.set_halign(Gtk.Align.START)
            self._context.set_parent(self)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self._context.set_pointing_to(rect)
        self._context.popup()

    def release(self) -> None:
        if self._context is not None:
            self._context.unparent()
            self._context = None

    @staticmethod
    def _menu_model() -> Gio.Menu:
        def item(label: str, action: str, accel: str | None = None) -> Gio.MenuItem:
            entry = Gio.MenuItem.new(label, action)
            if accel:
                entry.set_attribute_value("accel", GLib.Variant.new_string(accel))
            return entry

        menu = Gio.Menu()
        move = Gio.Menu()
        move.append_item(item("Move to top", "row.top", "<Control>Home"))
        move.append_item(item("Move up", "row.up", "<Control>Up"))
        move.append_item(item("Move down", "row.down", "<Control>Down"))
        move.append_item(item("Move to bottom", "row.bottom", "<Control>End"))
        menu.append_section(None, move)
        other = Gio.Menu()
        other.append_item(item("Show in Library", "row.library"))
        other.append_item(item("Remove from playlist", "row.remove", "Delete"))
        menu.append_section(None, other)
        return menu

    def _install_actions(self) -> Gio.SimpleActionGroup:
        page, entry = self.page, self.entry
        group = Gio.SimpleActionGroup()
        callbacks: tuple[tuple[str, Callable[[], None]], ...] = (
            ("top", lambda: page.move_to_end(entry.id, -1)),
            ("up", lambda: page.move(entry.id, -1)),
            ("down", lambda: page.move(entry.id, 1)),
            ("bottom", lambda: page.move_to_end(entry.id, 1)),
            ("library", lambda: page.state.navigate(f"library:{entry.wallpaper}")),
            ("remove", lambda: page.remove_entries([entry.id])),
        )
        for name, callback in callbacks:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, run=callback: run())
            group.add_action(action)
        library = group.lookup_action("library")
        if isinstance(library, Gio.SimpleAction):
            library.set_enabled(self.wallpaper is not None)
        self.insert_action_group("row", group)
        return group

    def _on_key(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        mods: Gdk.ModifierType,
    ) -> bool:
        page, entry_id = self.page, self.entry.id
        # Ctrl is the classic editor's key; Alt the prototype's. Both work.
        moving = bool(mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK))
        plain = not (
            mods
            & (
                Gdk.ModifierType.ALT_MASK
                | Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.SHIFT_MASK
            )
        )
        if moving and keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            page.move(entry_id, -1)
        elif moving and keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            page.move(entry_id, 1)
        elif moving and keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            page.move_to_end(entry_id, -1)
        elif moving and keyval in (Gdk.KEY_End, Gdk.KEY_KP_End):
            page.move_to_end(entry_id, 1)
        elif plain and keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            page.remove_entries([entry_id])
        elif keyval == Gdk.KEY_Menu or (
            keyval == Gdk.KEY_F10 and mods & Gdk.ModifierType.SHIFT_MASK
        ):
            if self.menu_button is not None:
                self.menu_button.popup()
        else:
            return False
        # Keyboard interaction starts on the row itself, even from its buttons.
        self.grab_focus()
        return True


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


class PlaylistPage(Page):
    name = "playlist"
    title = "Playlist"

    def __init__(self, state: AppState, editing: PlaylistEditing) -> None:
        super().__init__(state)
        global _styled
        if not _styled:
            widgets.add_css(_CSS)
            _styled = True
        self.editing = editing
        self._pid: str | None = None
        self._narrow = False
        self._compact = False
        self._selecting = False
        self._selected: set[str] = set()
        self._rows: dict[str, EntryRow] = {}
        self._limit = ENTRY_PAGE_SIZE
        self._grid_limit = GRID_PAGE_SIZE
        self._building = False
        self._render_deferred = False
        #: A dropped row is shown where it was dropped until the move settles.
        self._held_order = False
        #: Entries asked to be removed; their rows stay, insensitive, until saved.
        self._removing: set[str] = set()
        self._drop_at: int | None = None
        self._picker: PlaylistPicker | None = None
        self._dialog: Adw.AlertDialog | None = None
        self._editing_pid: str | None = None
        self._order: list[str] = []
        self._last_ids: list[str] = []
        #: A new playlist the page waits for, to select its sidebar row once it is saved.
        self._awaiting: str | None = None
        self._navigation = 0
        self._interval_values: list[int | None] = []
        # What the cover, the header menu and an automatic grid were last built
        # from: a status poll re-renders the page, and none of them is rebuilt
        # (or a popped-up menu disturbed) unless that changed.
        self._cover_key: tuple[object, ...] = ()
        self._menu_kind = ""
        self._grid_key: tuple[object, ...] = ()

        # -- header ----------------------------------------------------------------
        self._add_header = widgets.icon_button(
            "list-add-symbolic", "Add wallpapers", lambda *_: self._open_picker()
        )
        self._add_header.set_visible(False)
        self._select = widgets.select_toggle()
        self._select.set_tooltip_text("Select several to remove them together")
        self._select.connect("toggled", self._on_select_mode)
        self._menu_model = Gio.Menu()
        self._menu = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=self._menu_model)
        self._menu.set_tooltip_text("Playlist menu")
        self._title_reveal = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.CROSSFADE, child=self._title
        )

        # -- body -------------------------------------------------------------------
        self._notice = Adw.Banner()
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
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller.set_child(Adw.Clamp(maximum_size=980, tightening_threshold=760, child=body))
        self.scroller.get_vadjustment().connect("value-changed", self._on_scrolled)
        # The reorder gesture lives on the scrolled window: nothing a drag
        # moves (rows, or the list under an auto-scroll) can move it.
        self.list.attach(self.scroller)

        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._content.append(self._notice)
        self._content.append(self.scroller)
        self._content.append(self._build_selection_bar())
        self._waiting = Adw.StatusPage(
            icon_name="view-list-symbolic",
            title="Saving the new playlist…",
            description="It opens here as soon as it is saved",
        )
        self._waiting.set_child(Adw.Spinner(halign=Gtk.Align.CENTER))
        self._none = Adw.StatusPage(
            icon_name="view-list-symbolic",
            title="Make a playlist",
            description="A playlist is your own list of wallpapers, played in order",
        )
        create = Gtk.Button(label="New playlist…", halign=Gtk.Align.CENTER)
        create.add_css_class("pill")
        create.add_css_class("suggested-action")
        create.connect("clicked", lambda *_: self.ask_new())
        self._none.set_child(create)
        self._outer = Gtk.Stack()
        self._outer.add_named(self._content, "playlist")
        self._outer.add_named(self._waiting, "waiting")
        self._outer.add_named(self._none, "none")

        breakpoint_bin = Adw.BreakpointBin(width_request=340, height_request=300, child=self._outer)
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
        for trigger, callback in (
            ("Escape", self._on_escape),
            ("<Control>a", self._on_select_all_key),
        ):
            shortcuts.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(trigger), Gtk.CallbackAction.new(callback)
                )
            )
        self.widget.add_controller(shortcuts)
        state.connect("changed", self._on_changed)

    # -- Page API ---------------------------------------------------------------------
    def title_widget(self) -> Gtk.Widget:
        return self._title_reveal

    def header_start(self) -> list[Gtk.Widget]:
        return [self._add_header]

    def header_end(self) -> list[Gtk.Widget]:
        return [self._menu]

    def activate(self, argument: str | None) -> None:
        for dialog in (self._picker, self._dialog):
            if dialog is not None and dialog.get_parent() is not None:
                dialog.force_close()
        self._picker = self._dialog = None
        self._menu.popdown()
        pid = argument if argument else self._pid
        if pid is None or not (self.state.has_playlist(pid) or self.editing.creating(pid)):
            pid = self._first()
        switched = pid != self._pid
        if switched:
            if self._name.get_editing():
                self._name.stop_editing(True)  # commits to the playlist being edited
            self._pid = pid
            self._limit = ENTRY_PAGE_SIZE
            self._grid_limit = GRID_PAGE_SIZE
            self._held_order = False
            self._removing.clear()
            self._clear_rows()
        if self._select.get_active():
            self._select.set_active(False)  # re-renders
        self._selected.clear()
        self.render()
        if switched:
            adjustment = self.scroller.get_vadjustment()
            adjustment.set_value(0)

    def _first(self) -> str | None:
        playlists = self.state.playlists
        own = next((playlist.id for playlist in playlists if not playlist.automatic), None)
        return own or next((playlist.id for playlist in playlists), None)

    @property
    def pid(self) -> str | None:
        """The playlist shown (or waited for)."""
        return self._pid

    def detail(self) -> PlaylistDetail | None:
        return self.editing.detail(self._pid) if self._pid is not None else None

    def _automatic(self) -> bool:
        pid = self._pid
        return (
            pid is not None
            and self.state.has_playlist(pid)
            and bool(self.state.playlist(pid).automatic)
        )

    # -- building: hero -----------------------------------------------------------------
    def _build_hero(self) -> Gtk.Widget:
        self._hero = Gtk.Box(spacing=32)
        identity = Gtk.Box(spacing=20, hexpand=True)
        self._cover = Adw.Bin(valign=Gtk.Align.START)
        identity.append(self._cover)

        text = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, valign=Gtk.Align.CENTER
        )
        name_row = Gtk.Box(spacing=2)
        self._name = Gtk.EditableLabel(valign=Gtk.Align.CENTER)
        self._name.connect("changed", self._fit_name)
        _wrap_labels(self._name)  # long names wrap instead of widening the page
        self._name.add_css_class("pl-name")
        self._name.set_tooltip_text("Click to rename")
        self._name.connect("notify::editing", self._on_name_editing)
        self._name_static = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, wrap=False)
        self._name_static.add_css_class("pl-name")
        self._rename = widgets.icon_button(
            "document-edit-symbolic", "Rename", lambda *_: self.start_rename(), "flat", "circular"
        )
        self._rename.set_valign(Gtk.Align.CENTER)
        name_row.append(self._name)
        name_row.append(self._name_static)
        name_row.append(self._rename)
        text.append(name_row)
        self._summary = widgets.dim("")
        self._summary.add_css_class("numeric")
        text.append(self._summary)
        self._used = Adw.WrapBox(child_spacing=6, line_spacing=6)
        text.append(self._used)

        actions = Adw.WrapBox(child_spacing=14, line_spacing=8, margin_top=6)
        self._play_content = Adw.ButtonContent(
            icon_name="media-playback-start-symbolic", label="Play now"
        )
        self._play = Gtk.Button(child=self._play_content)
        for css in ("pill", "suggested-action", "pl-play"):
            self._play.add_css_class(css)
        self._play.connect("clicked", lambda *_: self.play())
        actions.append(self._play)
        self._default = Gtk.Button(label="Use as schedule default", valign=Gtk.Align.CENTER)
        self._default.add_css_class("pill")
        self._default.connect("clicked", lambda *_: self.make_default())
        actions.append(self._default)
        self._status = Gtk.Box(spacing=7, valign=Gtk.Align.CENTER)
        dot = Gtk.Box(valign=Gtk.Align.CENTER)
        dot.add_css_class("status-dot")
        self._status.append(dot)
        self._status_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._status_label.add_css_class("pl-status")
        self._status.append(self._status_label)
        actions.append(self._status)
        text.append(actions)
        identity.append(text)
        self._hero.append(identity)

        # How this playlist plays: its own interval and shuffle, or the defaults.
        self._playback = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.START
        )
        self._playback.set_size_request(300, -1)
        label = Gtk.Label(label="PLAYBACK", xalign=0)
        label.add_css_class("section-label")
        self._playback.append(label)
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        self.interval_row = Adw.ComboRow(title="Change")
        self.interval_row.set_tooltip_text("How often the next wallpaper comes on")
        self.interval_row.connect("notify::selected", self._on_interval)
        group.append(self.interval_row)
        self.shuffle_row = Adw.ComboRow(title="Order")
        self.shuffle_row.connect("notify::selected", self._on_shuffle)
        group.append(self.shuffle_row)
        self._playback.append(group)
        self._rotation_note = widgets.dim("")
        self._rotation_note.add_css_class("caption")
        self._playback.append(self._rotation_note)
        self._hero.append(self._playback)
        return self._hero

    # -- building: entries -----------------------------------------------------------------
    def _build_entries(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        # The right end stays free for the Select pill hanging over the list's corner.
        head = Gtk.Box(spacing=8, margin_end=widgets.CORNER_RESERVE + 10)
        heading = Gtk.Label(label="Wallpapers", xalign=0)
        heading.add_css_class("title-4")
        head.append(heading)
        self._count = Gtk.Label(label="0", valign=Gtk.Align.CENTER)
        self._count.add_css_class("pl-count")
        self._count.add_css_class("dimmed")
        head.append(self._count)
        self._saving = Gtk.Label(label="Saving…", valign=Gtk.Align.CENTER, visible=False)
        self._saving.add_css_class("dimmed")
        self._saving.add_css_class("caption")
        head.append(self._saving)
        head.append(Gtk.Box(hexpand=True))
        self._density = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self._density.add_css_class("flat")
        self._density.add(
            Adw.Toggle(name="large", icon_name="view-list-symbolic", tooltip="Large rows")
        )
        self._density.add(
            Adw.Toggle(
                name="compact", icon_name="view-list-bullet-symbolic", tooltip="Compact rows"
            )
        )
        self._density.set_active_name("large")
        self._density.connect("notify::active-name", self._on_density)
        head.append(self._density)
        self._add_inline_content = Adw.ButtonContent(
            icon_name="list-add-symbolic", label="Add wallpapers…"
        )
        self._add_inline = Gtk.Button(child=self._add_inline_content)
        self._add_inline.set_tooltip_text("Pick several wallpapers to add")
        self._add_inline.connect("clicked", lambda *_: self._open_picker())
        head.append(self._add_inline)
        box.append(head)

        # The ordered list; the drop line marks where a wallpaper dragged in
        # from the Library will land.
        self.list = ReorderList()
        self.list.add_css_class("pl-entries")
        self.list.connect("order-changed", lambda *_: self._renumber())
        self.list.connect("reordered", self._on_reordered)
        self.list.connect("settled", self._on_settled)
        self.list.connect("row-activated", self._on_row_activated)
        self._drop_line = Gtk.Box(valign=Gtk.Align.START, can_target=False, visible=False)
        self._drop_line.add_css_class("pl-drop-line")
        dot = Gtk.Box(valign=Gtk.Align.CENTER)
        dot.add_css_class("pl-drop-dot")
        bar = Gtk.Box(hexpand=True, valign=Gtk.Align.CENTER)
        bar.add_css_class("pl-drop-bar")
        self._drop_line.append(dot)
        self._drop_line.append(bar)
        self._list_wrap = Gtk.Overlay(child=self.list)
        self._list_wrap.add_css_class("pl-list-wrap")
        self._list_wrap.add_overlay(self._drop_line)
        self._attach_drop(self._list_wrap, positional=True)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        list_box.append(self._list_wrap)
        self.more = Gtk.Button(halign=Gtk.Align.CENTER, visible=False)
        self.more.add_css_class("pill")
        self.more.connect("clicked", lambda *_: self.show_more())
        list_box.append(self.more)
        self._hint = Gtk.Label(
            label="Drag a row by its handle to reorder · Ctrl+↑ / Ctrl+↓ with the keyboard",
            xalign=0,
        )
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
        self._empty_add = Gtk.Button(label="Add wallpapers…", halign=Gtk.Align.CENTER)
        self._empty_add.add_css_class("pill")
        self._empty_add.add_css_class("suggested-action")
        self._empty_add.connect("clicked", lambda *_: self._open_picker())
        self._empty.set_child(self._empty_add)
        self._attach_drop(self._empty, positional=False)

        # Automatic playlist: a read-only grid of everything, a page at a time.
        auto = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        note = Gtk.Box(spacing=10)
        note.add_css_class("pl-auto-note")
        icon = Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic")
        icon.add_css_class("dimmed")
        note.append(icon)
        self._auto_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        note.append(self._auto_label)
        self._duplicate_auto = Gtk.Button(label="Duplicate to edit", valign=Gtk.Align.CENTER)
        self._duplicate_auto.set_tooltip_text("Make your own copy you can reorder")
        self._duplicate_auto.connect("clicked", lambda *_: self.duplicate())
        note.append(self._duplicate_auto)
        auto.append(note)
        self.grid = widgets.CardGrid(min_width=176, max_columns=6, valign=Gtk.Align.START)
        auto.append(self.grid)
        self.grid_more = Gtk.Button(halign=Gtk.Align.CENTER, visible=False)
        self.grid_more.add_css_class("pill")
        self.grid_more.connect("clicked", lambda *_: self._show_more_cards())
        auto.append(self.grid_more)

        self._area = Gtk.Stack(vhomogeneous=False, hhomogeneous=False)
        self._area.add_named(list_box, "list")
        self._area.add_named(self._empty, "empty")
        self._area.add_named(auto, "auto")
        box.append(self._area)
        hanging = Gtk.Overlay(child=box)
        widgets.hang_on_corner(hanging, self._list_wrap, self._select, inset=14)
        return hanging

    def _build_selection_bar(self) -> Gtk.Widget:
        self._action_bar = Gtk.ActionBar(revealed=False)
        select_all = Gtk.Button(label="Select all")
        select_all.add_css_class("flat")
        select_all.connect("clicked", lambda *_: self.select_all())
        self._action_bar.pack_start(select_all)
        self._selection_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END)
        self._action_bar.set_center_widget(self._selection_label)
        self._remove_selected = Gtk.Button(label="Remove")
        self._remove_selected.add_css_class("destructive-action")
        self._remove_selected.connect(
            "clicked", lambda *_: self.remove_entries(sorted(self._selected, key=self._index))
        )
        self._action_bar.pack_end(self._remove_selected)
        return self._action_bar

    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        for name, callback in (
            ("rename", self.start_rename),
            ("duplicate", self.duplicate),
            ("delete", self.confirm_delete),
            ("add", self._open_picker),
            ("play", self.play),
            ("new", self.ask_new),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, run=callback: run())
            group.add_action(action)
        self._page_actions = group
        for widget in (self.widget, self._menu, self._add_header):
            widget.insert_action_group("pl", group)

    # -- rendering ------------------------------------------------------------------------
    def render(self) -> None:
        """Show the playlist as it is saved (and what is still being saved)."""
        pid = self._pid
        if pid is None:
            self._outer.set_visible_child_name("none")
            self.set_title("Playlists")
            self._menu.set_visible(False)
            self._add_header.set_visible(False)
            return
        if not self.state.has_playlist(pid):
            if self.editing.creating(pid):
                self._awaiting = pid
                self._outer.set_visible_child_name("waiting")
                self.set_title("New playlist")
                return
            self._fall_back()
            return
        if self._awaiting == pid:
            # Saved: let the sidebar (rebuilt after this signal) select its row.
            self._awaiting = None
            self._navigate_soon(f"playlist:{pid}")
        self._outer.set_visible_child_name("playlist")
        self._menu.set_visible(True)
        blocked = self.editing.editing_blocked()
        self._notice.set_title(blocked)
        self._notice.set_revealed(bool(blocked))
        if self._automatic():
            self._render_automatic()
            return
        detail = self.detail()
        if detail is None:
            self._fall_back()
            return
        self._render_hero(detail, blocked)
        self._render_entries(detail, blocked)

    def _fall_back(self) -> None:
        """The playlist shown is gone (deleted, or never saved): show a neighbour.

        The page switches at once; while it is the shell's current page the
        shell is asked to follow (after this signal, so its rebuilt sidebar
        selects the neighbour's row). A page that is not on screen, or a
        window that is away, just shows the neighbour next time.
        """
        gone = self._pid
        ids = [playlist.id for playlist in self.state.playlists]
        neighbour: str | None = None
        if gone in self._last_ids:
            at = self._last_ids.index(gone)
            for candidate in self._last_ids[at + 1 :] + self._last_ids[:at][::-1]:
                if candidate in ids and not self.state.playlist(candidate).automatic:
                    neighbour = candidate
                    break
        neighbour = neighbour or self._first()
        self._awaiting = None
        self._clear_rows()
        self._pid = neighbour
        self._limit = ENTRY_PAGE_SIZE
        self._held_order = False
        self._removing.clear()
        if neighbour is None:
            self.render()
            return
        self.render()
        self._navigate_soon(f"playlist:{neighbour}")

    def _navigate_soon(self, key: str) -> None:
        """Ask the shell to show ``key`` once the signal being handled is done
        (the shell rebuilds its sidebar after the page hears a change), and
        only while this page is the one on screen."""
        if self._navigation:
            GLib.source_remove(self._navigation)
            self._navigation = 0
        if not self._shown():
            return

        def go() -> bool:
            self._navigation = 0
            if self._shown():
                self.state.navigate(key)
            return GLib.SOURCE_REMOVE

        self._navigation = GLib.idle_add(go, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _render_hero(self, detail: PlaylistDetail, blocked: str) -> None:
        self.set_title(detail.name, "Playlist")
        size = 96 if self._narrow else 128
        first = next(
            (
                entry.wallpaper
                for entry in detail.entries
                if self.state.has_wallpaper(entry.wallpaper)
            ),
            "",
        )
        cover_key = (detail.id, first, size)
        if cover_key != self._cover_key:
            self._cover_key = cover_key
            self._cover.set_child(self._cover_widget(detail, size))
        self._name.set_visible(True)
        self._rename.set_visible(True)
        self._name_static.set_visible(False)
        self._play.set_visible(True)
        self._default.set_visible(True)
        self._name.set_sensitive(not blocked)
        self._rename.set_sensitive(not blocked)
        if not self._name.get_editing():
            self._name.set_text(detail.name)
        self._summary.set_label(summary(detail))

        self._play.set_sensitive(not detail.play_blocked and not detail.playing)
        self._play_content.set_label("Playing now" if detail.playing else "Play now")
        if detail.play_blocked:
            self._play.set_tooltip_text(detail.play_blocked)
        elif detail.playing:
            self._play.set_tooltip_text("It is your pick on every display now")
        elif detail.unavailable:
            noun = "entry is" if detail.unavailable == 1 else "entries are"
            self._play.set_tooltip_text(f"{detail.unavailable} unavailable {noun} skipped")
        else:
            self._play.set_tooltip_text(
                "Show this playlist on every display until you resume the schedule"
            )
        self._default.set_label(
            "Schedule default" if detail.is_default else "Use as schedule default"
        )
        self._default.set_sensitive(not detail.is_default and not detail.default_blocked)
        self._default.set_tooltip_text(
            "It plays whenever no schedule rule applies"
            if detail.is_default
            else detail.default_blocked or "Play it whenever no schedule rule applies"
        )
        self._render_used(detail)
        self._status_label.set_label(detail.on_screen)
        self._status.set_visible(bool(detail.on_screen))
        self._render_rotation(detail, blocked)

        self._sync_header_add()
        self._select.set_visible(bool(detail.entries) and not blocked)
        self._set_menu("playlist")
        for name in ("rename", "duplicate", "delete", "add", "new"):
            action = self._page_actions.lookup_action(name)
            if isinstance(action, Gio.SimpleAction):
                action.set_enabled(not blocked)

    def _cover_widget(self, detail: PlaylistDetail, size: int) -> Gtk.Widget:
        paintable = self.state.playlist_cover(detail.id, 256)
        if paintable is not None:
            cover: Gtk.Widget = widgets.texture_picture(paintable, size, size, radius=16)
            cover.add_css_class("pl-cover")
            return cover
        first = next(
            (
                self.state.wallpaper(entry.wallpaper)
                for entry in detail.entries
                if self.state.has_wallpaper(entry.wallpaper)
            ),
            None,
        )
        if first is not None:
            cover = widgets.thumbnail(first, size, size, 16)
            cover.add_css_class("pl-cover")
            return cover
        image = Gtk.Image.new_from_icon_name("view-list-symbolic")
        image.set_pixel_size(40)
        image.set_size_request(size, size)
        image.add_css_class("pl-cover-empty")
        return image

    def _chip(self, label: str, icon: str, target: str, tooltip: str, off: bool) -> Gtk.Button:
        chip = Gtk.Button(child=Adw.ButtonContent(icon_name=icon, label=label, can_shrink=True))
        chip.add_css_class("chip")
        chip.add_css_class("pl-chip")
        if off:
            chip.add_css_class("off")
        chip.set_tooltip_text(tooltip)
        chip.connect("clicked", lambda *_: self.state.navigate(target))
        return chip

    def _render_used(self, detail: PlaylistDetail) -> None:
        _clear(self._used)
        lead = Gtk.Label(
            label="Used by" if detail.usage else "Not in the schedule", valign=Gtk.Align.CENTER
        )
        lead.add_css_class("dimmed")
        lead.add_css_class("pl-used-label")
        self._used.append(lead)
        for usage in detail.usage:
            icon = (
                "video-display-symbolic"
                if usage.kind == "display"
                else "x-office-calendar-symbolic"
            )
            text = usage.label + (" · off" if usage.off else "")
            self._used.append(self._chip(text, icon, usage.target, usage.tooltip, usage.off))
        if not detail.usage:
            link = Gtk.Button(label="Add to schedule…")
            for css in ("flat", "chip", "pl-chip"):
                link.add_css_class(css)
            link.set_tooltip_text("Open the Schedule to choose when it plays")
            link.connect("clicked", lambda *_: self.state.navigate("schedule"))
            self._used.append(link)

    def _render_rotation(self, detail: PlaylistDetail, blocked: str) -> None:
        rotation = detail.rotation
        self._building = True
        try:
            values: list[int | None] = [None, *INTERVALS]
            if rotation.interval is not None and rotation.interval not in INTERVALS:
                values.append(rotation.interval)
            default = interval_words(rotation.default_interval).lower()
            words = [f"Default ({default})", *(interval_words(v) for v in values[1:] if v)]
            if values != self._interval_values or self._interval_words() != words:
                self._interval_values = values
                self.interval_row.set_model(Gtk.StringList.new(words))
            wanted = values.index(rotation.interval)
            if self.interval_row.get_selected() != wanted:
                self.interval_row.set_selected(wanted)
            order = "shuffled" if rotation.default_shuffle else "in order"
            shuffle_words = [f"Default ({order})", "Shuffle", "In order"]
            model = self.shuffle_row.get_model()
            current = (
                [model.get_string(i) or "" for i in range(model.get_n_items())]
                if isinstance(model, Gtk.StringList)
                else []
            )
            if current != shuffle_words:
                self.shuffle_row.set_model(Gtk.StringList.new(shuffle_words))
            wanted = {None: 0, True: 1, False: 2}[rotation.shuffle]
            if self.shuffle_row.get_selected() != wanted:
                self.shuffle_row.set_selected(wanted)
        finally:
            self._building = False
        self.interval_row.set_sensitive(not blocked)
        self.shuffle_row.set_sensitive(not blocked)
        self._rotation_note.set_label(rotation.note)
        self._rotation_note.set_visible(bool(rotation.note))
        self._playback.set_visible(True)

    def _interval_words(self) -> list[str]:
        model = self.interval_row.get_model()
        if not isinstance(model, Gtk.StringList):
            return []
        return [model.get_string(index) or "" for index in range(model.get_n_items())]

    def _render_entries(self, detail: PlaylistDetail, blocked: str) -> None:
        count = len(detail.entries)
        self._count.set_label(str(count))
        self._saving.set_visible(detail.busy)
        self._density.set_visible(count > 0 and not self._narrow)
        self._add_inline_content.set_label("Add…" if self._narrow else "Add wallpapers…")
        self._add_inline.set_visible(True)
        self._add_inline.set_sensitive(not blocked)
        self._empty_add.set_sensitive(not blocked)
        self.list.set_sensitive(not blocked)
        self._remember_ids()
        if self.list.busy:
            self._render_deferred = True  # never pull rows out from under the pointer
            return
        if detail.busy and self._held_order:
            return  # the dropped row stays where it was dropped until the move settles
        self._held_order = False
        self._render_deferred = False
        if not detail.busy:
            self._removing.clear()
        if not count:
            self._clear_rows()
            if self._selecting:
                self._select.set_active(False)
            self._area.set_visible_child_name("empty")
            self.more.set_visible(False)
            self._hint.set_visible(False)
            return
        self._area.set_visible_child_name("list")
        self._sync_rows(detail)
        self._hint.set_visible(count > 1 and not self._selecting)
        self._update_selection_bar()

    def _remember_ids(self) -> None:
        self._last_ids = [playlist.id for playlist in self.state.playlists]

    def _sync_rows(self, detail: PlaylistDetail) -> None:
        """Rows for the shown entries, reusing every row whose entry is unchanged."""
        focused = self._focused_entry_id()
        index_of = {entry.id: index for index, entry in enumerate(detail.entries)}
        if focused in index_of:
            # A focused row moved past the shown page: keep showing it.
            self._limit = max(self._limit, index_of[focused] + 1)
        shown = detail.entries[: self._limit]
        wanted = {entry.id for entry in shown}
        old_order = list(self._order)
        for entry_id, row in list(self._rows.items()):
            if entry_id not in wanted:
                row.release()
                self.list.remove_row(row)
                del self._rows[entry_id]
        rows: list[EntryRow] = []
        for entry in shown:
            signature = (entry, self._compact, self._narrow, self._selecting)
            existing = self._rows.get(entry.id)
            if existing is not None and existing.signature != signature:
                if focused == entry.id:
                    focused = ""
                existing.release()
                self.list.remove_row(existing)
                existing = None
            if existing is None:
                existing = EntryRow(
                    self,
                    entry,
                    compact=self._compact,
                    narrow=self._narrow,
                    selecting=self._selecting,
                    selected=entry.id in self._selected,
                )
                self.list.append(existing, existing.handle)
                self._rows[entry.id] = existing
            rows.append(existing)
        self.list.set_order(rows)
        self._order = [row.entry_id for row in rows]
        css = ["boxed-list", "reorder-list", "pl-entries"] + (
            ["pl-compact"] if self._compact else []
        )
        self.list.set_css_classes(css)
        positions: dict[str, list[int]] = {}
        for index, entry in enumerate(detail.entries):
            positions.setdefault(entry.wallpaper, []).append(index)
        showing: dict[str, list[str]] = {}
        for entry_id, connector in detail.showing:
            showing.setdefault(entry_id, []).append(connector)
        total = len(detail.entries)
        for index, row in enumerate(rows):
            entry = row.entry
            row.set_place(index, total)
            row.set_status(
                showing.get(entry.id, []),
                [other for other in positions.get(entry.wallpaper, []) if other != index],
            )
            row.set_removing(entry.id in self._removing)
        self._selected &= wanted
        remaining = total - len(rows)
        self.more.set_visible(remaining > 0)
        if remaining:
            self.more.set_label(
                f"Show {min(ENTRY_PAGE_SIZE, remaining)} more · {len(rows)} of {total} shown"
            )
        if focused and focused not in self._rows and focused in old_order:
            # Its row went away (removed, or rebuilt): keep the keyboard on a neighbour.
            at = old_order.index(focused)
            candidates = [entry_id for entry_id in old_order[at + 1 :] if entry_id in self._rows]
            candidates += [entry_id for entry_id in old_order[:at][::-1] if entry_id in self._rows]
            if candidates:
                self._rows[candidates[0]].grab_focus()

    def _clear_rows(self) -> None:
        if self.list.dragging:
            self.list.cancel_drag()
        for row in self._rows.values():
            row.release()
        self.list.remove_all()
        self._rows.clear()
        self._order = []

    def _renumber(self) -> None:
        total = len(self._order)
        detail = self.detail()
        if detail is not None:
            total = len(detail.entries)
        for index, row in enumerate(self.list.get_rows()):
            if isinstance(row, EntryRow):
                row.set_place(index, total)

    def _render_automatic(self) -> None:
        assert self._pid is not None
        playlist = self.state.playlist(self._pid)
        self.set_title(playlist.name, "Automatic playlist")
        size = 96 if self._narrow else 128
        image = Gtk.Image.new_from_icon_name(playlist.icon or "view-grid-symbolic")
        image.set_pixel_size(40)
        image.set_size_request(size, size)
        image.add_css_class("pl-cover-empty")
        cover_key = (playlist.id, playlist.icon, size)
        if cover_key != self._cover_key:
            self._cover_key = cover_key
            self._cover.set_child(image)
        self._name.set_visible(False)
        self._rename.set_visible(False)
        self._name_static.set_visible(True)
        self._name_static.set_label(playlist.name)
        count = len(playlist.entries)
        self._summary.set_label("1 wallpaper" if count == 1 else f"{count} wallpapers")
        _clear(self._used)
        self._play.set_visible(False)
        self._default.set_visible(False)
        self._status.set_visible(False)
        self._playback.set_visible(False)
        self._select.set_visible(False)
        self._add_header.set_visible(False)
        self._add_inline.set_visible(False)
        self._density.set_visible(False)
        self._saving.set_visible(False)
        self._count.set_label(str(count))
        self._auto_label.set_label(playlist.automatic + ".")
        self._duplicate_auto.set_sensitive(not self.editing.editing_blocked())
        self._set_menu("automatic")
        for name in ("duplicate", "new"):
            action = self._page_actions.lookup_action(name)
            if isinstance(action, Gio.SimpleAction):
                action.set_enabled(not self.editing.editing_blocked())
        self._clear_rows()
        self._area.set_visible_child_name("auto")
        current = tuple(sorted(self.state.current.items()))
        grid_key = (playlist.id, tuple(playlist.entries), self._grid_limit, current)
        if grid_key != self._grid_key:
            self._grid_key = grid_key
            self._render_grid(playlist.entries)

    def _set_menu(self, kind: str) -> None:
        """The header menu for a playlist of one's own, or an automatic one."""
        if kind == self._menu_kind:
            return
        self._menu_kind = kind
        self._menu_model.remove_all()
        if kind == "automatic":
            edit = Gio.Menu()
            edit.append("Duplicate", "pl.duplicate")
            edit.append("New playlist…", "pl.new")
            self._menu_model.append_section(None, edit)
            return
        edit = Gio.Menu()
        edit.append("Rename", "pl.rename")
        edit.append("Duplicate", "pl.duplicate")
        self._menu_model.append_section(None, edit)
        danger = Gio.Menu()
        danger.append("Delete…", "pl.delete")
        self._menu_model.append_section(None, danger)
        new = Gio.Menu()
        new.append("New playlist…", "pl.new")
        self._menu_model.append_section(None, new)

    def _render_grid(self, wids: Sequence[str]) -> None:
        self.grid.remove_all()
        state = self.state
        playing: dict[str, list[str]] = {}
        for connector, wid in state.current.items():
            playing.setdefault(wid, []).append(connector)
        shown = [wid for wid in wids if state.has_wallpaper(wid)][: self._grid_limit]
        for wid in shown:
            wallpaper = state.wallpaper(wid)
            card = widgets.WallpaperCard(
                wallpaper,
                width=168,
                playing_on=playing.get(wid),
                on_open=lambda chosen: self.state.navigate(f"library:{chosen.id}"),
                on_apply=lambda chosen: self.state.apply(chosen.id, self.state.scope),
                apply_blocked=state.apply_blocked(wid),
                **widgets.card_colors(state, wallpaper),
            )
            card.set_tooltip_text(f"{wallpaper.name} \u2014 open in the Library")
            self.grid.append(card)
        remaining = len(wids) - len(shown)
        self.grid_more.set_visible(remaining > 0)
        if remaining > 0:
            self.grid_more.set_label(
                f"Show {min(GRID_PAGE_SIZE, remaining)} more · {len(shown)} of {len(wids)} shown"
            )

    def _show_more_cards(self) -> None:
        self._grid_limit += GRID_PAGE_SIZE
        self.render()

    def show_more(self) -> None:
        """One more page of rows, without rebuilding the ones shown."""
        self._limit += ENTRY_PAGE_SIZE
        self.render()

    def _set_narrow(self, narrow: bool) -> None:
        self._narrow = narrow
        self.render()

    # -- callbacks -------------------------------------------------------------------------
    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic in _TOPICS and self._pid is not None:
            self.render()

    def _on_settled(self, _list: ReorderList) -> None:
        if self._render_deferred:
            self.render()

    def _on_reordered(self, _list: ReorderList, row: object, origin: int, target: int) -> None:
        """A drag settled: one relative move, from where the entry is when it is saved."""
        if not isinstance(row, EntryRow) or self._pid is None or origin == target:
            return
        self._order = [r.entry_id for r in self.list.get_rows() if isinstance(r, EntryRow)]
        self._held_order = True
        self.editing.move_entry(self._pid, row.entry_id, target - origin)
        row.grab_focus()

    def _on_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        self._title_reveal.set_reveal_child(adjustment.get_value() > 70)
        self._sync_header_add()

    def _sync_header_add(self) -> None:
        """The header "+" stands in for "Add wallpapers…" only while that is scrolled away."""
        if self._pid is None or self._automatic() or self.editing.editing_blocked():
            self._add_header.set_visible(False)
            return
        viewport = self.scroller.get_child()
        content = viewport.get_child() if isinstance(viewport, Gtk.Viewport) else None
        if content is None:
            self._add_header.set_visible(False)
            return
        ok, bounds = self._add_inline.compute_bounds(content)
        scrolled = self.scroller.get_vadjustment().get_value()
        self._add_header.set_visible(ok and bounds.get_y() + bounds.get_height() < scrolled)

    def _on_interval(self, row: Adw.ComboRow, _param: object) -> None:
        if self._building or self._pid is None:
            return
        index = row.get_selected()
        if 0 <= index < len(self._interval_values):
            self.editing.set_playlist_interval(self._pid, self._interval_values[index])

    def _on_shuffle(self, row: Adw.ComboRow, _param: object) -> None:
        if self._building or self._pid is None:
            return
        choices: tuple[bool | None, ...] = (None, True, False)
        index = row.get_selected()
        if 0 <= index < len(choices):
            self.editing.set_playlist_shuffle(self._pid, choices[index])

    def _on_density(self, group: Adw.ToggleGroup, _param: object) -> None:
        self._compact = group.get_active_name() == "compact"
        self.render()

    # -- playlist actions ---------------------------------------------------------------------
    def play(self) -> None:
        if self._pid is not None and not self._automatic():
            self.editing.play(self._pid)

    def make_default(self) -> None:
        if self._pid is not None and not self._automatic():
            self.editing.make_default(self._pid)

    def start_rename(self) -> None:
        if self._pid is None or self._automatic() or self.editing.editing_blocked():
            return
        self.scroller.get_vadjustment().set_value(0)
        self._name.start_editing()
        self._name.grab_focus()

    def _fit_name(self, label: Gtk.EditableLabel) -> None:
        # Size the field to the name so the pencil sits right after it.
        chars = max(4, min(32, len(label.get_text()) + 1))
        label.set_width_chars(min(chars, 6))  # minimum: may shrink at narrow widths
        label.set_max_width_chars(chars)  # natural: hugs the name

    def _on_name_editing(self, label: Gtk.EditableLabel, _param: object) -> None:
        if label.get_editing():
            self._editing_pid = self._pid
            return
        pid, self._editing_pid = self._editing_pid or self._pid, None
        if pid is None:
            return
        detail = self.editing.detail(pid)
        if detail is None:
            return
        new = " ".join(label.get_text().split())
        if not new or new == detail.name:
            if pid == self._pid:
                label.set_text(detail.name)
            return
        self.editing.rename_playlist(pid, new)

    def duplicate(self) -> None:
        if self._pid is None or self.editing.editing_blocked():
            return
        copy = self.editing.duplicate_playlist(self._pid)
        if copy:
            self.state.navigate(f"playlist:{copy}")

    def ask_new(self) -> None:
        """Name a new playlist; it opens here once it is saved."""
        blocked = self.editing.editing_blocked()
        if blocked:
            self.state.toast(blocked)
            return
        dialog = Adw.AlertDialog(
            heading="New playlist", body="Give it a name. You can add wallpapers next."
        )
        entry = Gtk.Entry(placeholder_text="Playlist name", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")

        def done(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "create":
                self.create(entry.get_text().strip() or "Untitled playlist")

        dialog.connect("response", done)
        dialog.present(self.widget)
        self._dialog = dialog

    def create(self, name: str) -> str:
        """Ask for a playlist called ``name`` and open it (once saved)."""
        pid = self.editing.create_playlist(name)
        self.state.navigate(f"playlist:{pid}")
        return pid

    def confirm_delete(self) -> None:
        if self._pid is None or self._automatic():
            return
        detail = self.detail()
        if detail is None:
            return
        dialog = Adw.AlertDialog(
            heading=f"Delete {quoted(detail.name)}?", body=self.editing.delete_warning(detail.id)
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        pid = detail.id

        def respond(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "delete":
                self.editing.delete_playlist(pid)

        dialog.connect("response", respond)
        dialog.present(self.widget)
        self._dialog = dialog

    # -- entry actions -------------------------------------------------------------------------
    def _index(self, entry_id: str) -> int:
        detail = self.detail()
        if detail is None:
            return 0
        for index, entry in enumerate(detail.entries):
            if entry.id == entry_id:
                return index
        return len(detail.entries)

    def move(self, entry_id: str, step: int) -> None:
        """One queued relative move; the row moves when the move is saved."""
        if self._pid is not None and step:
            self.editing.move_entry(self._pid, entry_id, step)

    def move_to_end(self, entry_id: str, direction: int) -> None:
        """To the top (``direction`` < 0) or the bottom, from where it is when saved."""
        detail = self.detail()
        if detail is None:
            return
        index = self._index(entry_id)
        step = -index if direction < 0 else len(detail.entries) - 1 - index
        self.move(entry_id, step)

    def remove_entries(self, entry_ids: Sequence[str]) -> None:
        if self._pid is None or not entry_ids:
            return
        if self.editing.editing_blocked():
            self.state.toast(self.editing.editing_blocked())
            return
        entry_ids = [entry_id for entry_id in entry_ids if entry_id not in self._removing]
        if not entry_ids:
            return  # already asked for
        self._removing.update(entry_ids)
        for entry_id in entry_ids:
            row = self._rows.get(entry_id)
            if row is not None:
                row.set_removing(True)
        if self._selecting:
            self._selected.difference_update(entry_ids)
            self._select.set_active(False)
        self.editing.remove_entries(self._pid, list(entry_ids))

    def add_entries(self, wids: Sequence[str], position: int | None = None) -> None:
        if self._pid is None or self._automatic():
            return
        chosen = [wid for wid in wids if self.state.has_wallpaper(wid)]
        if chosen:
            self.editing.add_entries(self._pid, chosen, position)

    def _open_picker(self) -> None:
        self.open_picker()

    def open_picker(self, preselect: Sequence[str] | None = None) -> PlaylistPicker | None:
        detail = self.detail()
        if detail is None or self._automatic() or self.editing.editing_blocked():
            return None
        picker = PlaylistPicker(
            self.state,
            detail.name,
            [entry.wallpaper for entry in detail.entries],
            self.add_entries,
        )
        self._picker = picker

        def closed(_dialog: Adw.Dialog) -> None:
            if self._picker is picker:
                self._picker = None

        picker.connect("closed", closed)
        if preselect:
            picker.pick_wallpapers(preselect)
        picker.present(self.widget)
        return picker

    @property
    def picker(self) -> PlaylistPicker | None:
        return self._picker

    @property
    def rows(self) -> list[EntryRow]:
        """The rows shown, in order."""
        return [row for row in self.list.get_rows() if isinstance(row, EntryRow)]

    def row(self, entry_id: str) -> EntryRow | None:
        return self._rows.get(entry_id)

    def _focused_entry_id(self) -> str:
        root = self.widget.get_root()
        widget = root.get_focus() if isinstance(root, Gtk.Window) else None
        while widget is not None and not isinstance(widget, EntryRow):
            widget = widget.get_parent()
        return widget.entry_id if isinstance(widget, EntryRow) else ""

    # -- selection mode -----------------------------------------------------------------------
    def _on_select_mode(self, button: Gtk.ToggleButton) -> None:
        self._selecting = button.get_active()
        self._selected.clear()
        self.render()
        self._update_selection_bar()

    def set_entry_selected(self, entry_id: str, selected: bool) -> None:
        if selected:
            self._selected.add(entry_id)
        else:
            self._selected.discard(entry_id)
        self._update_selection_bar()

    def select_all(self) -> None:
        """Every row shown."""
        for row in self._rows.values():
            if row.check is not None:
                row.check.set_active(True)
        self._selected = set(self._rows)
        self._update_selection_bar()

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if self._selecting and isinstance(row, EntryRow) and row.check is not None:
            row.check.set_active(not row.check.get_active())

    def _update_selection_bar(self) -> None:
        count = len(self._selected)
        self._selection_label.set_label("Pick rows to remove" if not count else f"{count} selected")
        self._remove_selected.set_sensitive(bool(count))
        self._action_bar.set_revealed(self._selecting)

    def _on_escape(self, *_args: Any) -> bool:
        if self._selecting:
            self._select.set_active(False)
            return True
        return False

    def _on_select_all_key(self, *_args: Any) -> bool:
        if self._selecting:
            self.select_all()
            return True
        return False

    # -- dropping wallpapers in from the Library ----------------------------------------------
    def _attach_drop(self, widget: Gtk.Widget, positional: bool) -> None:
        target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        # Motion decides where the line goes from the payload; GTK refuses a
        # drop after a motion that answered "no action", so motion always
        # answers COPY (fail open) and the payload is preloaded.
        target.set_preload(True)
        if positional:
            target.connect("enter", self._on_drop_motion)
            target.connect("motion", self._on_drop_motion)
            target.connect("drop", self._on_drop)
        else:

            def enter(_target: Gtk.DropTarget, _x: float, _y: float) -> Gdk.DragAction:
                widget.add_css_class("pl-drop-active")
                return Gdk.DragAction.COPY

            target.connect("enter", enter)
            target.connect("motion", lambda *_: Gdk.DragAction.COPY)
            target.connect("drop", lambda _t, value, _x, _y: self._on_append_drop(value))
        target.connect("leave", lambda *_: self._end_drop_feedback())
        widget.add_controller(target)

    def drop_position(self, y: float) -> int:
        """The slot a drop at list ``y`` lands in: before the row whose middle is below it."""
        rows = self.rows
        for index, row in enumerate(rows):
            ok, bounds = row.compute_bounds(self.list)
            if ok and y < bounds.get_y() + bounds.get_height() / 2:
                return index
        return len(rows)

    def _show_drop_line(self, position: int) -> None:
        self._drop_at = position
        rows = self.rows
        if not rows:
            return
        if position < len(rows):
            ok, bounds = rows[position].compute_bounds(self.list)
            y = bounds.get_y() if ok else 0
        else:
            ok, bounds = rows[-1].compute_bounds(self.list)
            y = bounds.get_y() + bounds.get_height() if ok else 0
        self._drop_line.set_margin_top(max(0, int(y) - 5))
        self._drop_line.set_visible(True)

    def _on_drop_motion(self, _target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        ok, point = self._list_wrap.compute_point(self.list, Graphene.Point().init(x, y))
        self._show_drop_line(self.drop_position(point.y if ok else y))
        return Gdk.DragAction.COPY

    def _end_drop_feedback(self) -> None:
        self._drop_line.set_visible(False)
        self._empty.remove_css_class("pl-drop-active")
        self._drop_at = None

    def _on_drop(self, _target: Gtk.DropTarget, value: object, x: float, y: float) -> bool:
        ok, point = self._list_wrap.compute_point(self.list, Graphene.Point().init(x, y))
        position = self.drop_position(point.y if ok else y)
        self._end_drop_feedback()
        wid = value if isinstance(value, str) else ""
        if not self.state.has_wallpaper(wid):
            return False
        # Below the last row shown is the end of the playlist: rows past a
        # long playlist's shown page are not built, so that slot's index
        # would land mid-list.
        self.add_entries([wid], position if position < len(self.rows) else None)
        return True

    def _on_append_drop(self, value: object) -> bool:
        self._end_drop_feedback()
        wid = value if isinstance(value, str) else ""
        if not self.state.has_wallpaper(wid):
            return False
        self.add_entries([wid])
        return True

    # -- the prototype's screenshot scenes ---------------------------------------------------------
    def demo(self, scene: str) -> None:
        what, _, arg = scene.partition(":")
        if what == "picker":
            self.open_picker(["northern-lights", "moon-bay", "lily-pond"])
        elif what == "picker-search":
            picker = self.open_picker(["frog-dusk"])
            if picker is not None:
                picker.search.set_text(arg or "pond")
        elif what == "menu":
            index = max(0, int(arg or "1") - 1)

            def open_menu() -> None:
                rows = self.rows
                if rows:
                    row = rows[min(index, len(rows) - 1)]
                    row.popup_context(row.get_width() - 250, row.get_height() / 2)

            self._later(300, open_menu)
        elif what == "playlist-menu":
            self._later(300, self._menu.popup)
        elif what == "drag":
            self._demo_drag(*(int(part) for part in (arg or "6,2").split(",")))
        elif what == "select":
            self._select.set_active(True)
            for row in self.rows[1:5:2]:
                if row.check is not None:
                    row.check.set_active(True)
        elif what == "compact":
            self._density.set_active_name("compact")
        elif what == "rename":
            self._later(200, self.start_rename)
        elif what == "delete":
            self.confirm_delete()
        elif what == "new":
            self.ask_new()
        elif what == "added":
            self.add_entries(["northern-lights", "moon-bay"])
        elif what == "removed":
            rows = self.rows
            if len(rows) > 2:
                self.remove_entries([rows[2].entry_id])
        elif what == "scrolled":
            self._later(300, lambda: self.scroller.get_vadjustment().set_value(int(arg or 400)))

    def _shown(self) -> bool:
        """This page is the shell's current one: its header widgets are in the header."""
        return self._menu.get_root() is not None and self.widget.get_mapped()

    def _later(self, milliseconds: int, run: Callable[[], None]) -> None:
        """Run a scene's step a moment later, only if the page is still on screen."""

        def once() -> bool:
            if self._shown():
                run()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(milliseconds, once)

    def _demo_drag(self, source: int, target: int) -> None:
        """Freeze a reorder mid-drag: row ``source`` (1-based) lifted and carried
        to just past row ``target``'s middle, the rows between rolled aside."""

        def show() -> bool:
            rows = self.rows
            src, dst = source - 1, target - 1
            if not (0 <= src < len(rows) and 0 <= dst < len(rows)) or self.list.busy:
                return False
            heights = self.list.row_heights()
            tops = [sum(heights[:index]) for index in range(len(rows))]
            self.list.begin_drag(rows[src])
            lean = -0.12 if dst < src else 0.12
            middle = tops[dst] + heights[dst] * (0.5 + lean)
            self.list.update_drag(middle - (tops[src] + heights[src] / 2))
            return False

        GLib.timeout_add(250, lambda: show() if self._shown() else False)


def create(state: AppState) -> Page:
    """The Playlists page, or its placeholder when the app can't edit playlists here."""
    editing = state.playlist_editing
    if editing is None:
        return not_ported("playlist", "Playlists", "view-list-symbolic")(state)
    return PlaylistPage(state, editing)
