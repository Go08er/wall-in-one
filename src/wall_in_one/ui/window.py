"""The main window: a grid of wallpapers, and the controls to move through it.

The wallpapers are the content. Settings live in a dialog behind the header bar
menu, because a wallpaper manager whose main view is a preferences page is a
settings app wearing a costume.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, Gtk

from wall_in_one import config
from wall_in_one.library import filter as library_filter
from wall_in_one.library.model import MediaItem
from wall_in_one.session import Session
from wall_in_one.theme import source
from wall_in_one.ui.browse_dialog import BrowseDialog
from wall_in_one.ui.grid import WallpaperGrid
from wall_in_one.ui.palette_browser import PaletteBrowserDialog
from wall_in_one.ui.preferences import PreferencesDialog
from wall_in_one.ui.thumbnails import ThumbnailLoader

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

        self.set_title("Wall-in-One")
        self.set_default_size(1100, 760)

        self._grid = WallpaperGrid(self._loader, self._on_tile_activated)
        self._subtitle = Adw.WindowTitle(title="Wall-in-One", subtitle=self._summary)
        self._toast = Adw.ToastOverlay()

        self.set_content(self._build_content())
        self.connect("destroy", self._on_destroy)

    # -- construction ----------------------------------------------------

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(self._subtitle)

        navigation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        navigation.add_css_class("linked")
        for icon, tooltip, verb in (
            ("go-previous-symbolic", "Previous wallpaper", "previous"),
            ("media-playlist-shuffle-symbolic", "Random wallpaper", "random"),
            ("go-next-symbolic", "Next wallpaper", "next"),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
            button.connect("clicked", self._make_navigator(verb))
            navigation.append(button)
        header.pack_start(navigation)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan the library")
        refresh.connect("clicked", lambda _button: self._app.refresh_library())
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
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Main menu")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        for name, opener in (
            ("preferences", self.open_preferences),
            ("palettes", self.open_palette_browser),
            ("browse", self.open_browse),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._make_opener(opener))
            self.add_action(action)

        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self._build_library_bar())
        self._toast.set_child(self._grid)
        toolbar.set_content(self._toast)
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

    def _make_navigator(self, verb: str) -> Any:
        def navigate(_button: Gtk.Button) -> None:
            # The application owns the session and the error handling; the
            # window only says which direction.
            response = self._app.apply(getattr(self._app.session, verb))
            if not response.ok:
                self.report(response.message)

        return navigate

    # -- behaviour -------------------------------------------------------

    def _on_tile_activated(self, item: MediaItem) -> None:
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

    @property
    def settings(self) -> config.Settings:
        return self._settings

    def apply_settings(self, settings: config.Settings) -> None:
        self._settings = settings

    # -- display ---------------------------------------------------------

    def show_library(self, session: Session) -> None:
        library = session.library
        cursor = session.cursor
        self._grid.populate(session.playlist.items, cursor.path if cursor else None)

        self._playable = len(session.playlist)
        summary = (
            f"{len(session.playlist)} of {len(library)} playable "
            f"({len(library.videos)} video, {len(library.stills)} still)"
        )
        if library.skipped:
            summary += f" - {len(library.skipped)} skipped"
        self._summary = summary
        self._update_subtitle()

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

    def show_current(self, session: Session) -> None:
        """Move the highlight without rebuilding the grid."""
        cursor = session.cursor
        self._grid.set_current(cursor.path if cursor else None)

    def show_palette(self, resolved: source.ResolvedPalette) -> None:
        if self._preferences is not None:
            self._preferences.show_palette(resolved)
        if self._palettes is not None:
            self._palettes.show_palette()
