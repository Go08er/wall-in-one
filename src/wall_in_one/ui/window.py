"""The main window: a grid of wallpapers, and the controls to move through it.

The wallpapers are the content. Settings live in a dialog behind the header bar
menu, because a wallpaper manager whose main view is a preferences page is a
settings app wearing a costume.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk

from wall_in_one import config
from wall_in_one.library import favourites, manage, pairings
from wall_in_one.library import filter as library_filter
from wall_in_one.library.model import IMAGE_EXTENSIONS, MediaItem
from wall_in_one.session import Session
from wall_in_one.theme import palettes, source
from wall_in_one.ui.browse_dialog import BrowseDialog
from wall_in_one.ui.grid import WallpaperGrid
from wall_in_one.ui.pairings_page import PairingsPage
from wall_in_one.ui.palette_browser import PaletteBrowserDialog
from wall_in_one.ui.playlists_page import PlaylistsPage
from wall_in_one.ui.preferences import PreferencesDialog
from wall_in_one.ui.schedules_page import SchedulesPage
from wall_in_one.ui.thumbnails import ThumbnailLoader

#: A palette submenu is a menu, not a list: past this many a person is
#: scrolling rather than choosing, and the palette browser is the right place
#: for a collection that size.
MAX_PALETTES_PER_ORIGIN: Final = 24

#: Accelerator, action, and what to call it in the shortcuts dialogue. One
#: table, so a key that works and a key the dialogue claims cannot drift apart.
#:
#: Everything is modified. The search box takes focus for whole seconds at a
#: time and a bare `n` for "next wallpaper" would land in it, which is the
#: kind of shortcut people learn once and then resent.
ACCELERATORS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("Wallpaper", "<Control>Right", "win.next", "Next wallpaper"),
    ("Wallpaper", "<Control>Left", "win.previous", "Previous wallpaper"),
    ("Wallpaper", "<Control><Shift>R", "win.random", "Random wallpaper"),
    ("Library", "<Control>F", "win.search", "Search the library"),
    ("Library", "F5", "win.refresh", "Rescan the library"),
    ("Library", "<Control>B", "win.browse", "Find wallpapers online"),
    ("Application", "<Control>comma", "win.preferences", "Settings"),
    ("Application", "<Control>P", "win.palettes", "Palettes"),
    ("Application", "<Control>question", "win.shortcuts", "Keyboard shortcuts"),
    ("Application", "<Control>W", "window.close", "Close the window"),
)


def _sections() -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """`_ACCELERATORS` regrouped for the dialogue, keeping the order above."""
    grouped: dict[str, list[tuple[str, str]]] = {}
    for section, accelerator, _action, description in ACCELERATORS:
        grouped.setdefault(section, []).append((accelerator, description))
    return tuple((title, tuple(entries)) for title, entries in grouped.items())


_SHORTCUTS: Final = _sections()


if TYPE_CHECKING:
    from wall_in_one.ui.app import Application

_Choice = TypeVar("_Choice")


def _chosen(dropdown: Gtk.DropDown, choices: tuple[_Choice, ...]) -> _Choice:
    """What ``dropdown`` is pointing at.

    GTK answers `GTK_INVALID_LIST_POSITION` when nothing is selected, which is
    an unsigned -1 and would index off the end of a three-item tuple.
    """
    index = dropdown.get_selected()
    return choices[index] if index < len(choices) else choices[0]


class MainWindow(Adw.ApplicationWindow):
    """Application window."""

    def __init__(self, application: Application, settings: config.Settings) -> None:
        super().__init__(application=application)
        self._app = application
        self._settings = settings
        self._loader = ThumbnailLoader()
        self._preferences: PreferencesDialog | None = None
        self._palettes: PaletteBrowserDialog | None = None
        self._browse: BrowseDialog | None = None
        # How the library is being looked at. Deliberately not in
        # `config.Settings`: a search is about the next thirty seconds, and
        # reopening the app to yesterday's filter still applied -- with most of
        # the library missing and no obvious reason why -- is a bug report.
        # Surviving a rescan is enough, and the grid keeps it for that.
        self._query = library_filter.Query()
        self._playable = 0
        self._summary = "No library loaded"
        self._management_session: Session | None = None

        self.set_title("Wall-in-One")
        self.set_default_size(1100, 760)

        # The session's, not one of our own: it builds the rotation from these,
        # so a second copy here would let the grid and what actually cycles
        # disagree.
        self._favourites = application.session.favourites
        self._grid = WallpaperGrid(
            self._loader, self._on_tile_activated, self._on_favourite, self._menu_for
        )
        self._grid.set_favourites(self._favourites.paths)
        self._toast = Adw.ToastOverlay()
        self._pairings_page = PairingsPage(application)
        self._playlists_page = PlaylistsPage(application)
        self._schedules_page = SchedulesPage(application)

        self.set_content(self._build_content())
        self.connect("destroy", self._on_destroy)

    # -- construction ----------------------------------------------------

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._stack = Adw.ViewStack()
        self._subtitle = Adw.WindowTitle(title="Wall-in-One", subtitle=self._summary)
        header.set_title_widget(self._subtitle)

        navigation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        navigation.add_css_class("linked")
        for icon, tooltip, verb in (
            ("go-previous-symbolic", "Previous wallpaper", "previous"),
            ("media-playlist-shuffle-symbolic", "Random wallpaper", "random"),
            ("go-next-symbolic", "Next wallpaper", "next"),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
            navigate = self._make_navigator(verb)
            button.connect("clicked", lambda _button, run=navigate: run())
            navigation.append(button)
        header.pack_start(navigation)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan the library")
        refresh.connect("clicked", lambda _button: self._app.refresh_library())
        refresh.set_tooltip_text("Rescan the library (F5)")
        header.pack_start(refresh)

        browse = Gtk.Button(
            icon_name="system-search-symbolic", tooltip_text="Find wallpapers online"
        )
        browse.connect("clicked", lambda _button: self.open_browse())
        header.pack_end(browse)

        menu = Gio.Menu()
        menu.append("Find wallpapers", "win.browse")
        menu.append("Palettes", "win.palettes")
        menu.append("Settings", "win.preferences")
        menu.append("Keyboard Shortcuts", "win.shortcuts")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Main menu")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        for name, opener in (
            ("preferences", self.open_preferences),
            ("palettes", self.open_palette_browser),
            ("browse", self.open_browse),
            ("shortcuts", self.open_shortcuts),
            ("next", self._make_navigator("next")),
            ("previous", self._make_navigator("previous")),
            ("random", self._make_navigator("random")),
            ("refresh", self._app.refresh_library),
            ("search", self._focus_search),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._make_opener(opener))
            self.add_action(action)

        # Parameterised, so one action serves every tile: the alternative is a
        # pair of actions per wallpaper, registered and torn down on every
        # rescan.
        for name, handler in (
            ("apply-wallpaper", self._on_apply_path),
            ("remove-wallpaper", self._on_remove_path),
            ("favourite-wallpaper", self._on_favourite_path),
            ("choose-still", self._on_choose_still),
            ("reset-pairing", self._on_reset_pairing),
        ):
            targeted = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            targeted.connect("activate", handler)
            self.add_action(targeted)

        # Two strings rather than one: a palette choice is about a wallpaper
        # *and* a policy, and packing them into one string would need an
        # escape rule for a separator that palette names are allowed to contain.
        palette_action = Gio.SimpleAction.new("palette-wallpaper", GLib.VariantType.new("(ss)"))
        palette_action.connect("activate", self._on_palette_path)
        self.add_action(palette_action)

        toolbar.add_top_bar(header)
        media = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        media.append(self._build_library_bar())
        self._toast.set_child(self._grid)
        media.append(self._toast)
        self._stack.add_titled_with_icon(media, "media", "Media", "image-x-generic-symbolic")
        self._stack.add_titled_with_icon(
            self._pairings_page,
            "pairings",
            "Pairings",
            "preferences-color-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._playlists_page,
            "playlists",
            "Playlists",
            "view-list-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._schedules_page,
            "schedules",
            "Display schedules",
            "video-display-symbolic",
        )
        switcher = Adw.ViewSwitcherBar(stack=self._stack, reveal=True)
        self._stack.connect("notify::visible-child-name", self._on_page_changed)
        toolbar.add_bottom_bar(switcher)
        toolbar.set_content(self._stack)
        return toolbar

    def _build_library_bar(self) -> Gtk.Widget:
        """Search, kind, and sort, on a row of their own under the header.

        A second top bar rather than a revealed search bar, because two of the
        three controls are useful without typing anything: a sort hidden behind
        a search button is a sort nobody finds. It is the same row the browse
        dialog puts its query and its filters on, so the two views of
        wallpapers -- yours and the internet's -- are driven the same way.
        """
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        bar.set_margin_start(12)
        bar.set_margin_end(12)

        self._search = Gtk.SearchEntry(hexpand=True)
        self._search.set_placeholder_text("Search your library")
        # `search-changed` rather than `activate`: GTK debounces it, so
        # filtering as the user types costs one pass per pause, not per key.
        self._search.connect("search-changed", self._on_controls_changed)
        self._search.connect("stop-search", self._on_search_stopped)
        bar.append(self._search)

        self._kinds = Gtk.DropDown.new_from_strings(
            [choice.label for choice in library_filter.KIND_CHOICES]
        )
        self._kinds.set_tooltip_text("Show stills, videos, or everything")
        self._kinds.connect("notify::selected", self._on_controls_changed)
        bar.append(self._kinds)

        self._sorts = Gtk.DropDown.new_from_strings(
            [choice.label for choice in library_filter.SORT_CHOICES]
        )
        self._sorts.set_tooltip_text("Sort the grid")
        self._sorts.connect("notify::selected", self._on_controls_changed)
        bar.append(self._sorts)
        return bar

    def _on_controls_changed(self, *_arguments: object) -> None:
        self._query = library_filter.Query(
            text=self._search.get_text(),
            kinds=_chosen(self._kinds, library_filter.KIND_CHOICES),
            sort=_chosen(self._sorts, library_filter.SORT_CHOICES),
        )
        self._grid.set_query(self._query)
        self._update_subtitle()

    def _on_search_stopped(self, _entry: Gtk.SearchEntry) -> None:
        # Escape in the search box means "show me everything again".
        self._search.set_text("")

    def _make_opener(self, opener: Callable[[], None]) -> Any:
        # Bound now rather than read from the loop variable when the action
        # fires, which would open whichever dialog came last.
        def activate(*_arguments: object) -> None:
            opener()

        return activate

    def _make_navigator(self, verb: str) -> Callable[[], None]:
        def navigate() -> None:
            # The application owns the session and the error handling; the
            # window only says which direction.
            response = self._app.apply(getattr(self._app.session, verb))
            if not response.ok:
                self.report(response.message)

        return navigate

    def _focus_search(self) -> None:
        """Put the cursor in the search box, selecting whatever is in it.

        Selecting rather than clearing: pressing the shortcut twice by reflex
        should not be the same as having typed the query again, and typing
        replaces the selection anyway.
        """
        self._search.grab_focus()
        self._search.select_region(0, -1)

    def open_shortcuts(self) -> None:
        """The list of accelerators, since nothing else announces them.

        `attr-defined` is silenced because the installed pygobject-stubs
        predate libadwaita 1.9, which is where `ShortcutsDialog` arrived. The
        three classes are checked for at runtime rather than assumed, so an
        older libadwaita gets a toast instead of a traceback.
        """
        if not hasattr(Adw, "ShortcutsDialog"):
            self.report("This libadwaita is too old to show the shortcut list")
            return
        dialog = Adw.ShortcutsDialog()
        for title, entries in _SHORTCUTS:
            section = Adw.ShortcutsSection(title=title)  # type: ignore[attr-defined]
            for accelerator, description in entries:
                section.add(
                    Adw.ShortcutsItem(  # type: ignore[attr-defined]
                        title=description, accelerator=accelerator
                    )
                )
            dialog.add(section)
        dialog.present(self)

    # -- behaviour -------------------------------------------------------

    def _on_tile_activated(self, item: MediaItem) -> None:
        """Play Media through the explicit one-entry ``Quick choice`` list."""
        response = self._app.apply(lambda: self._app.session.select(item.path))
        if not response.ok:
            self.report(response.message)

    def open_preferences(self) -> None:
        dialog = PreferencesDialog(self._app)
        self._preferences = dialog
        dialog.connect("closed", self._on_preferences_closed)
        dialog.present(self)

    def _on_preferences_closed(self, _dialog: Adw.PreferencesDialog) -> None:
        self._preferences = None

    def open_palette_browser(self) -> None:
        dialog = PaletteBrowserDialog(self._app)
        self._palettes = dialog
        dialog.connect("closed", self._on_palette_browser_closed)
        dialog.present(self)

    def _on_palette_browser_closed(self, _dialog: Adw.Dialog) -> None:
        self._palettes = None

    def open_browse(self) -> None:
        """Open the search-and-download dialog, one at a time.

        Reusing the open one keeps its results and its preview cache, so
        pressing the button again does not throw away a page of downloads.
        """
        if self._browse is not None:
            self._browse.present(self)
            return
        dialog = BrowseDialog(self._app)
        self._browse = dialog
        dialog.connect("closed", self._on_browse_closed)
        dialog.present(self)

    def _on_browse_closed(self, _dialog: Adw.Dialog) -> None:
        self._browse = None

    def report(self, message: str) -> None:
        """Surface a failure where the user will actually see it."""
        self._toast.add_toast(Adw.Toast.new(message))

    def _on_destroy(self, _window: Gtk.Window) -> None:
        self._loader.shutdown()
        self._pairings_page.shutdown()

    @property
    def settings(self) -> config.Settings:
        return self._settings

    def apply_settings(self, settings: config.Settings) -> None:
        self._settings = settings

    # -- display ---------------------------------------------------------

    def show_library(self, session: Session) -> None:
        library = session.library
        cursor = session.cursor
        # Before `populate`, so a tile built in this pass arrives already
        # starred. The store is the session's and anything may have moved it
        # since we last looked -- `ctl favourite` reaches it without going
        # anywhere near this window.
        self._grid.set_favourites(self._favourites.paths)
        # Media is the complete crafting library, never a disguised view of
        # whichever playlist happens to be playing. Activating one item makes
        # the visible one-entry Quick choice playlist; it does not bypass the
        # playlist model.
        self._grid.populate(library.items, cursor.path if cursor else None)

        self._playable = len(library)
        active = session.playlists.get(session.active_playlist())
        playing = active.name if active is not None else "All media"
        summary = (
            f"{len(library)} media · playing {playing} "
            f"({len(library.videos)} video, {len(library.stills)} still)"
        )
        if library.skipped:
            summary += f" - {len(library.skipped)} skipped"
        self._summary = summary
        self._update_subtitle()
        self._management_session = session
        self._refresh_visible_page()

    def _on_page_changed(self, _stack: Adw.ViewStack, _property: object) -> None:
        self._refresh_visible_page()

    def _refresh_visible_page(self) -> None:
        """Build only the page being viewed; palette previews stay truly lazy."""
        session = self._management_session
        if session is None:
            return
        shown = self._stack.get_visible_child_name()
        if shown == "pairings":
            self._pairings_page.refresh(session)
        elif shown == "playlists":
            self._playlists_page.refresh(session)
        elif shown == "schedules":
            self._schedules_page.refresh(session)

    def _update_subtitle(self) -> None:
        """Report what is on screen without misreporting the library.

        A filtered view says both numbers. The counts behind it are facts about
        the library and do not change because something is being hidden, so a
        search that matches two wallpapers must not leave the window claiming
        the user owns two -- which is what replacing the count would do, and it
        would be indistinguishable from a scan that had just lost most of their
        collection.
        """
        summary = self._summary
        if self._query.narrows:
            summary = f"Showing {self._grid.visible_count} of {self._playable} - {summary}"
        self._subtitle.set_subtitle(summary)

    def _on_favourite(self, item: MediaItem, wanted: bool) -> None:
        """Star or unstar one wallpaper, and tell the user if it did not stick."""
        try:
            if wanted:
                self._favourites.add(item.path)
            else:
                self._favourites.discard(item.path)
        except favourites.FavouritesError:
            # The store keeps the change in memory whatever the disk did, so
            # the star stays where the user put it; this only says that it
            # will not outlive the session.
            self.report(f"{item.name} is a favourite for now, but could not be saved")
        self._grid.set_favourites(self._favourites.paths)
        self._app.session.favourites_changed()
        self._update_subtitle()

    # -- the per-tile menu -----------------------------------------------

    def _menu_for(self, item: MediaItem) -> Gio.MenuModel:
        """The actions offered for one wallpaper.

        The removal verb is named for what it does to *that* file. "Remove"
        for something we downloaded means gone; "Move to Trash" for the user's
        own means recoverable, and the two must not be spelled the same, since
        one of them cannot be undone.
        """
        menu = Gio.Menu()
        target = GLib.Variant.new_string(str(item.path))
        apply_item = Gio.MenuItem.new("Play as Quick choice", None)
        apply_item.set_action_and_target_value("win.apply-wallpaper", target)
        menu.append_item(apply_item)

        # Also in the menu, not only on the star. The star is hidden until the
        # tile is hovered or focused, which keeps the grid readable but leaves
        # favouriting undiscoverable for anyone who never hovers -- and the
        # menu is where someone looks for "what can I do with this one".
        starred = item.path in self._favourites.paths
        favourite_item = Gio.MenuItem.new(
            "Remove from favourites" if starred else "Add to favourites", None
        )
        favourite_item.set_action_and_target_value("win.favourite-wallpaper", target)
        menu.append_item(favourite_item)
        bundle = self._app.session.pairings.resolve(item, self._app.session.library.roots)
        still_item = Gio.MenuItem.new("Choose a still...", None)
        still_item.set_action_and_target_value("win.choose-still", target)
        menu.append_item(still_item)

        if bundle.customized:
            # Only offered when there is something to undo. A reset that does
            # nothing is a menu entry teaching people the menu lies.
            reset_item = Gio.MenuItem.new("Reset to defaults", None)
            reset_item.set_action_and_target_value("win.reset-pairing", target)
            menu.append_item(reset_item)

        menu.append_submenu("Colours", self._palette_menu(item))

        remove_item = Gio.MenuItem.new("Remove" if item.deletable else "Move to Trash", None)
        remove_item.set_action_and_target_value("win.remove-wallpaper", target)
        menu.append_item(remove_item)
        return menu

    def _palette_menu(self, item: MediaItem) -> Gio.MenuModel:
        """Which colours this one wallpaper asks Noctalia for.

        Built per tile rather than once, because the discovered palettes can
        change while the window is open -- and because the menus are built on
        first click now, so a submenu costs nothing until somebody opens it.
        """
        menu = Gio.Menu()
        source = str(item.path)

        fixed = Gio.Menu()
        for label, policy in (
            ("Adaptive", pairings.PalettePolicy()),
            ("Keep current", pairings.PalettePolicy(kind=pairings.KEEP)),
        ):
            fixed_item = Gio.MenuItem.new(label, None)
            fixed_item.set_action_and_target_value(
                "win.palette-wallpaper",
                GLib.Variant("(ss)", (source, policy.encode())),
            )
            fixed.append_item(fixed_item)
        menu.append_section(None, fixed)

        found = palettes.discover()
        for origin in (palettes.Origin.BUILTIN, palettes.Origin.COMMUNITY, palettes.Origin.CUSTOM):
            entries = [entry for entry in found.entries if entry.origin is origin]
            if not entries:
                continue
            section = Gio.Menu()
            for entry in entries[:MAX_PALETTES_PER_ORIGIN]:
                policy = pairings.PalettePolicy(origin.value, entry.name)
                chosen = Gio.MenuItem.new(entry.name, None)
                chosen.set_action_and_target_value(
                    "win.palette-wallpaper",
                    GLib.Variant("(ss)", (source, policy.encode())),
                )
                section.append_item(chosen)
            menu.append_submenu(origin.label, section)
        return menu

    def _on_choose_still(self, _action: Gio.SimpleAction, raw: GLib.Variant | None) -> None:
        """Pick the picture that stands in for this wallpaper.

        Any image will do, including one outside the library: a representative
        is a picture, not a library entry, and refusing an outside one would
        mean the only way to use a photo is to import it first.
        """
        item = self._item_at(raw)
        if item is None:
            return
        dialog = Gtk.FileDialog(title=f"Choose a still for {item.name}", modal=True)
        images = Gtk.FileFilter()
        images.set_name("Images")
        for extension in sorted(IMAGE_EXTENSIONS):
            images.add_pattern(f"*{extension}")
            images.add_pattern(f"*{extension.upper()}")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)
        dialog.open(self._window_for_dialog(), None, self._make_still_receiver(item))

    def _window_for_dialog(self) -> Gtk.Window | None:
        return self

    def _make_still_receiver(self, item: MediaItem) -> Any:
        def chosen(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                picked = dialog.open_finish(result)
            except GLib.Error:
                # Dismissed. Saying so would be noise.
                return
            path = picked.get_path() if picked is not None else None
            if path is None:
                self.report("That file is not on this machine's filesystem")
                return
            self._store_still(item, Path(path))

        return chosen

    def _store_still(self, item: MediaItem, still: Path | None) -> None:
        try:
            self._app.session.pairings.choose_still(item, still)
        except pairings.PairingError:
            self.report(f"{item.name} uses it for now, but the choice could not be saved")
        self._reapply_if_current(item)
        self._app.refresh_library()

    def _on_reset_pairing(self, _action: Gio.SimpleAction, raw: GLib.Variant | None) -> None:
        """Forget everything chosen for one wallpaper."""
        item = self._item_at(raw)
        if item is None:
            return
        try:
            if not self._app.session.pairings.reset(item):
                return
        except pairings.PairingError:
            self.report(f"{item.name} is back to its defaults for now, but that was not saved")
        self.report(f"{item.name} is back to its defaults")
        self._reapply_if_current(item)
        self._app.refresh_library()

    def _reapply_if_current(self, item: MediaItem) -> None:
        """Show a changed pairing at once, but only if it is what is on screen.

        Changing the wallpaper somebody is not looking at would be a surprise;
        leaving the one they *are* looking at stale would be a bug.
        """
        cursor = self._app.session.cursor
        if cursor is not None and cursor.path == item.path:
            self._app.apply(self._app.session.apply_current)

    def _on_palette_path(self, _action: Gio.SimpleAction, raw: GLib.Variant | None) -> None:
        """Record which colours a wallpaper asks for, and show them now.

        Applied immediately only when it is the wallpaper on screen: changing
        the colours of something you are not looking at would be a surprise.
        """
        if raw is None:
            return
        source, encoded = raw.unpack()
        item = self._app.session.library.find(Path(source))
        if item is None:
            return
        policy = pairings.PalettePolicy.decode(encoded)
        try:
            self._app.session.pairings.choose_palette(item, policy)
        except pairings.PairingError:
            self.report(f"{item.name} keeps those colours for now, but they could not be saved")

        self._reapply_if_current(item)
        self._app.refresh_library()

    def _item_at(self, raw: GLib.Variant | None) -> MediaItem | None:
        if raw is None:
            return None
        return self._app.session.library.find(Path(raw.get_string()))

    def _on_apply_path(self, _action: Gio.SimpleAction, raw: GLib.Variant | None) -> None:
        item = self._item_at(raw)
        if item is not None:
            self._on_tile_activated(item)

    def _on_favourite_path(self, _action: Gio.SimpleAction, raw: GLib.Variant | None) -> None:
        """Flip the star from the menu. Same path as clicking it."""
        item = self._item_at(raw)
        if item is not None:
            self._on_favourite(item, item.path not in self._favourites.paths)

    def _on_remove_path(self, _action: Gio.SimpleAction, raw: GLib.Variant | None) -> None:
        """Ask before an unlink, but not before a trip to the trash.

        Confirming everything trains people to confirm everything, so the
        prompt is spent where it buys something: `manage.remove` unlinks and
        cannot be undone, while `manage.trash` is recoverable from the file
        manager and asking about it would be theatre.
        """
        item = self._item_at(raw)
        if item is None:
            return
        if not item.deletable:
            self._trash(item)
            return

        dialog = Adw.AlertDialog(
            heading=f"Remove {item.name}?",
            body=f"{item.path} will be deleted. This cannot be undone.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_remove_confirmed, item)
        dialog.present(self)

    def _on_remove_confirmed(
        self, _dialog: Adw.AlertDialog, response: str, item: MediaItem
    ) -> None:
        if response != "remove":
            return
        try:
            result = manage.remove(item, self._app.session.library.roots)
        except manage.ManageError as error:
            self.report(str(error))
            return
        self._forget(item)
        self.report(result.describe())

    def _trash(self, item: MediaItem) -> None:
        try:
            manage.trash(item.path, self._app.session.library.roots)
        except manage.ManageError as error:
            self.report(str(error))
            return
        self._forget(item)
        self.report(f"{item.name} moved to the trash")

    def _forget(self, item: MediaItem) -> None:
        """Drop a removed wallpaper from the favourites, then rescan.

        The star and the pairing are what survive the file, and keeping either
        for something the app itself destroyed would be pointless: the reason
        they outlive a missing file is that the file might come back, which is
        not true of one we just deleted.
        """
        with contextlib.suppress(favourites.FavouritesError):
            self._favourites.discard(item.path)
        with contextlib.suppress(pairings.PairingError):
            self._app.session.pairings.forget_path(item.path)
        self._grid.set_favourites(self._favourites.paths)
        self._app.refresh_library()

    def show_current(self, session: Session) -> None:
        """Move the highlight without rebuilding the grid."""
        cursor = session.cursor
        self._grid.set_current(cursor.path if cursor else None)

    def show_palette(self, resolved: source.ResolvedPalette) -> None:
        if self._preferences is not None:
            self._preferences.show_palette(resolved)
        if self._palettes is not None:
            self._palettes.show_palette()
