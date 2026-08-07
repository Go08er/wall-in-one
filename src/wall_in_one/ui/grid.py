"""The wallpaper grid: the app's main view.

Tiles are the wallpapers themselves. Everything else -- counts, settings,
palette -- is secondary and lives elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from wall_in_one import thumbnails as thumbnail_cache
from wall_in_one.library import filter as library_filter
from wall_in_one.library.model import Kind, MediaItem, Ownership
from wall_in_one.ui.thumbnails import ThumbnailLoader

#: Tiles keep the thumbnail's aspect ratio so the grid lines up.
TILE_WIDTH = thumbnail_cache.THUMBNAIL_WIDTH
TILE_HEIGHT = thumbnail_cache.THUMBNAIL_HEIGHT


class WallpaperTile(Gtk.Box):
    """One wallpaper: preview, name, and what kind of thing it is."""

    def __init__(self, item: MediaItem) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.item = item
        self.add_css_class("wio-tile")

        self._picture = Gtk.Picture()
        self._picture.set_size_request(TILE_WIDTH, TILE_HEIGHT)
        self._picture.set_content_fit(Gtk.ContentFit.COVER)
        self._picture.add_css_class("wio-tile-image")

        frame = Gtk.Overlay()
        frame.set_child(self._picture)

        # Until the thumbnail arrives, show something with the right footprint
        # so tiles do not jump around as they load in.
        self._spinner = Adw.Spinner()
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._spinner.set_valign(Gtk.Align.CENTER)
        frame.add_overlay(self._spinner)

        # Top-right, opposite the badges, so that a long provider name can
        # never push it off the tile. A button rather than a decoration,
        # because marking a favourite should not mean finding a menu first --
        # and a `ToggleButton` is focusable, so the keyboard reaches it too.
        self._star = Gtk.ToggleButton(icon_name="non-starred-symbolic")
        self._star.set_halign(Gtk.Align.END)
        self._star.set_valign(Gtk.Align.START)
        self._star.set_margin_top(6)
        self._star.set_margin_end(6)
        self._star.add_css_class("circular")
        self._star.add_css_class("osd")
        self._star.add_css_class("wio-star")
        self._star.add_css_class("wio-tile-action")
        self._star.set_tooltip_text("Add to favourites")
        #: Set while the star is being moved to match the store rather than by
        #: a click. Without it, reflecting the state would look like a click
        #: and write the value straight back -- harmless once, and an endless
        #: exchange the first time anything else moves the state.
        self._reflecting = False
        frame.add_overlay(self._star)

        # Bottom-right, below the star. A `MenuButton` rather than right-click
        # alone: a right-click-only action is unreachable for anyone driving
        # the app from the keyboard, and this is where the only destructive
        # verb in the program lives.
        self._menu = Gtk.MenuButton(icon_name="view-more-symbolic")
        self._menu.set_halign(Gtk.Align.END)
        self._menu.set_valign(Gtk.Align.END)
        self._menu.set_margin_bottom(6)
        self._menu.set_margin_end(6)
        self._menu.add_css_class("circular")
        self._menu.add_css_class("osd")
        self._menu.add_css_class("wio-tile-action")
        self._menu.set_tooltip_text(f"Actions for {item.name}")
        frame.add_overlay(self._menu)

        badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        badges.set_halign(Gtk.Align.START)
        badges.set_valign(Gtk.Align.START)
        badges.set_margin_top(6)
        badges.set_margin_start(6)
        if item.kind is Kind.VIDEO:
            badges.append(_badge("Video" if item.paired_still else "Video (no still)"))
        if item.ownership is Ownership.MANAGED:
            badges.append(_badge(item.provider))
        frame.add_overlay(badges)

        caption = Gtk.Label(label=item.name)
        caption.set_ellipsize(Pango.EllipsizeMode.END)
        caption.set_max_width_chars(24)
        caption.add_css_class("caption")

        self.append(frame)
        self.append(caption)

    def show_thumbnail(self, path: Path | None) -> None:
        self._spinner.set_visible(False)
        if path is None:
            # No preview available; the name still identifies it.
            self._picture.set_paintable(None)
            self.add_css_class("wio-tile-blank")
            return
        try:
            texture = Gdk.Texture.new_from_filename(str(path))
        except GLib.Error:
            self.add_css_class("wio-tile-blank")
            return
        self._picture.set_paintable(texture)

    def set_current(self, current: bool) -> None:
        if current:
            self.add_css_class("wio-tile-current")
        else:
            self.remove_css_class("wio-tile-current")

    def set_favourite(self, favourite: bool) -> None:
        """Show whether this one is starred. Never reads as a click."""
        self._reflecting = True
        try:
            self._star.set_active(favourite)
        finally:
            self._reflecting = False
        self._star.set_icon_name("starred-symbolic" if favourite else "non-starred-symbolic")
        self._star.set_tooltip_text("Remove from favourites" if favourite else "Add to favourites")

    def set_menu(self, menu: Gio.MenuModel) -> None:
        """Give the tile its action menu, and let a right-click raise it too."""
        self._menu.set_menu_model(menu)
        gesture = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        gesture.connect("pressed", lambda *_arguments: self._menu.popup())
        self.add_controller(gesture)

    def connect_favourite(self, on_toggle: Callable[[MediaItem, bool], None]) -> None:
        """Say who to tell when the star is clicked."""

        def toggled(button: Gtk.ToggleButton) -> None:
            if self._reflecting:
                return
            on_toggle(self.item, button.get_active())

        self._star.connect("toggled", toggled)


def _badge(text: str) -> Gtk.Widget:
    label = Gtk.Label(label=text)
    label.add_css_class("caption")
    label.add_css_class("wio-badge")
    return label


class WallpaperGrid(Gtk.ScrolledWindow):
    """A scrolling grid of tiles. Activating one applies that wallpaper."""

    def __init__(
        self,
        loader: ThumbnailLoader,
        on_activate: Callable[[MediaItem], None],
        on_favourite: Callable[[MediaItem, bool], None] | None = None,
        menu_for: Callable[[MediaItem], Gio.MenuModel] | None = None,
    ) -> None:
        super().__init__()
        self._loader = loader
        self._on_activate = on_activate
        self._on_favourite = on_favourite
        # The window builds the menus, because the window owns the actions they
        # point at. The grid only knows where to hang one.
        self._menu_for = menu_for
        #: Which paths are starred. Held rather than looked up per tile so the
        #: filter and the tiles cannot disagree within one pass.
        self._favourites: frozenset[Path] = frozenset()
        self._tiles: dict[Path, WallpaperTile] = {}
        self._items: tuple[MediaItem, ...] = ()
        self._query = library_filter.Query()
        #: Where each visible item sits in the current order. Membership is the
        #: filter and the value is the sort, both decided by `library.filter`.
        self._positions: dict[Path, int] = {}

        self._flow = Gtk.FlowBox()
        self._flow.set_valign(Gtk.Align.START)
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_homogeneous(True)
        self._flow.set_column_spacing(12)
        self._flow.set_row_spacing(12)
        self._flow.set_margin_top(12)
        self._flow.set_margin_bottom(12)
        self._flow.set_margin_start(12)
        self._flow.set_margin_end(12)
        self._flow.connect("child-activated", self._on_child_activated)
        # The FlowBox does the hiding and the reordering itself, from the two
        # lookups below. Rebuilding the children instead would be a fresh set
        # of tiles on every keystroke.
        self._flow.set_filter_func(self._is_visible)
        self._flow.set_sort_func(self._compare)

        self._empty = Adw.StatusPage(
            title="No wallpapers found",
            description="Nothing under the configured roots. Check Noctalia's wallpaper directory.",
            icon_name="image-x-generic-symbolic",
        )

        # A search that matches nothing is not an empty library, and saying so
        # in the empty state above would tell a user with six hundred
        # wallpapers to go and check their wallpaper directory.
        self._unmatched = Adw.StatusPage(icon_name="system-search-symbolic")

        self._stack = Gtk.Stack()
        self._stack.add_named(self._flow, "grid")
        self._stack.add_named(self._empty, "empty")
        self._stack.add_named(self._unmatched, "unmatched")

        self.set_child(self._stack)
        self.set_vexpand(True)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    def _on_child_activated(self, _flow: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        tile = child.get_child()
        if isinstance(tile, WallpaperTile):
            self._on_activate(tile.item)

    def populate(self, items: tuple[MediaItem, ...], current: Path | None = None) -> None:
        """Bring the grid into line with ``items``, keeping search and sort.

        A diff rather than a rebuild. Rescans are not rare any more -- one
        follows every download and every batch of generated stills -- and
        tearing down six hundred tiles to build six hundred identical ones
        costs about six hundred milliseconds of frozen window each time, plus
        a re-decode of every thumbnail texture that was already on screen.

        A tile is reused when its `MediaItem` is unchanged. The comparison is
        the whole frozen dataclass on purpose: size and mtime are what the
        thumbnail cache is keyed on, and kind, pairing, ownership and provider
        are what the badges say, so anything that moves is something the tile
        is drawing.
        """
        incoming = {item.path: item for item in items}
        for path, tile in list(self._tiles.items()):
            replacement = incoming.get(path)
            if replacement is None or replacement != tile.item:
                self._flow.remove(tile)
                del self._tiles[path]

        self._items = items
        for item in items:
            if item.path in self._tiles:
                continue
            tile = WallpaperTile(item)
            if self._on_favourite is not None:
                tile.connect_favourite(self._on_favourite)
            if self._menu_for is not None:
                tile.set_menu(self._menu_for(item))
            tile.set_favourite(item.path in self._favourites)
            self._tiles[item.path] = tile
            self._flow.append(tile)
            self._loader.request(item, self._on_thumbnail)

        # After the diff, because a reused tile keeps whatever it was told last
        # time and the highlight may have moved since.
        self.set_current(current)
        self._apply_query()

    def _on_thumbnail(self, item: MediaItem, path: Path | None) -> None:
        tile = self._tiles.get(item.path)
        if tile is not None:
            tile.show_thumbnail(path)

    # -- searching, filtering, sorting -------------------------------------

    def set_query(self, query: library_filter.Query) -> None:
        """Narrow or reorder what is already on screen.

        Nothing is built and nothing is fetched: the tiles all exist from
        `populate`, and this only settles which of them the FlowBox shows and
        in what order. That matters more than it looks -- `ThumbnailLoader`
        answers a cache hit straight away, but an item whose thumbnail could
        not be generated has no cache entry, so rebuilding tiles per keystroke
        would put the same failing ffmpeg call back on the pool for every
        letter typed.
        """
        self._query = query
        self._apply_query()

    @property
    def visible_count(self) -> int:
        """How many tiles the current query leaves showing."""
        return len(self._positions)

    def set_favourites(self, favourites: frozenset[Path]) -> None:
        """Adopt a new set of starred paths, restarring the tiles it changes.

        Only the tiles whose state actually moved are touched. Restarring all
        of them would be correct and would also mean a widget write per
        wallpaper every time one star is clicked.
        """
        changed = self._favourites ^ favourites
        self._favourites = favourites
        for path in changed:
            tile = self._tiles.get(path)
            if tile is not None:
                tile.set_favourite(path in favourites)
        # Only the favourites view is narrowed by this, but re-filtering is
        # cheap and getting it wrong means a starred wallpaper that will not
        # appear until something else happens to invalidate the filter.
        self._apply_query()

    def _apply_query(self) -> None:
        visible = library_filter.apply(self._items, self._query, self._favourites)
        self._positions = {item.path: index for index, item in enumerate(visible)}
        self._flow.invalidate_filter()
        self._flow.invalidate_sort()

        if not self._items:
            self._stack.set_visible_child_name("empty")
        elif not visible:
            self._unmatched.set_title(f"No {library_filter.describe(self._query)}")
            self._unmatched.set_description(
                "Nothing in your library matches. Clear the search, or widen the filter."
            )
            self._stack.set_visible_child_name("unmatched")
        else:
            self._stack.set_visible_child_name("grid")

    def _is_visible(self, child: Gtk.FlowBoxChild) -> bool:
        return self._position(child) is not None

    def _compare(self, first: Gtk.FlowBoxChild, second: Gtk.FlowBoxChild) -> int:
        # The order was decided in `library.filter`; this only reads off the
        # positions it produced. Hidden children sort past the end, where their
        # order does not matter because nothing draws them.
        return self._rank(first) - self._rank(second)

    def _rank(self, child: Gtk.FlowBoxChild) -> int:
        position = self._position(child)
        return len(self._positions) if position is None else position

    def _position(self, child: Gtk.FlowBoxChild) -> int | None:
        tile = child.get_child()
        if not isinstance(tile, WallpaperTile):
            return None
        return self._positions.get(tile.item.path)

    def set_current(self, current: Path | None) -> None:
        """Move the "this one is up" highlight without rebuilding anything."""
        for path, tile in self._tiles.items():
            tile.set_current(path == current)
