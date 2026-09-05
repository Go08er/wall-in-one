"""The wallpaper grid: the app's main view.

Tiles are the wallpapers themselves. Everything else -- counts, settings,
palette -- is secondary and lives elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, Gtk, Pango

from wall_in_one import thumbnails as thumbnail_cache
from wall_in_one.library import filter as library_filter
from wall_in_one.library.model import Kind, MediaItem, Ownership
from wall_in_one.ui.thumbnails import ThumbnailLoader

#: Tiles keep the thumbnail's aspect ratio so the grid lines up.
TILE_WIDTH = thumbnail_cache.THUMBNAIL_WIDTH
TILE_HEIGHT = thumbnail_cache.THUMBNAIL_HEIGHT

#: A FlowBox child is a fairly deep GTK tree (picture, overlays, buttons,
#: badges, spinner, caption), and every child also starts a thumbnail request.
#: Keeping one widget per library item made opening a 4,096-item collection
#: take seconds and retain thousands of invisible widgets.  Seventy-two is
#: enough for several full 1100x760 viewports while keeping the initial work
#: independent of library size.  More pages are an explicit user action.
MEDIA_PAGE_SIZE: Final = 72


class _PreserveCurrent:
    """Sentinel distinguishing an ordinary rescan from an explicit clear."""


_PRESERVE_CURRENT: Final = _PreserveCurrent()


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

        self._frame = Gtk.Overlay()
        self._frame.set_child(self._picture)
        self._frame.set_tooltip_text("Click to edit · right-click for actions")

        # Until the thumbnail arrives, show something with the right footprint
        # so tiles do not jump around as they load in.
        self._spinner = Adw.Spinner()
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._spinner.set_valign(Gtk.Align.CENTER)
        self._frame.add_overlay(self._spinner)

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
        self._frame.add_overlay(self._star)

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
        self._frame.add_overlay(self._menu)

        badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        badges.set_halign(Gtk.Align.START)
        badges.set_valign(Gtk.Align.START)
        badges.set_margin_top(6)
        badges.set_margin_start(6)
        if item.is_moving:
            # Named for what it is, because a scene and a video are shown by
            # different programs and only one of them can be paused to a still
            # without help.
            what = "Scene" if item.kind is Kind.SCENE else "Video"
            badges.append(_badge(what if item.paired_still else f"{what} (no still)"))
        if item.ownership is Ownership.MANAGED:
            badges.append(_badge(item.provider))
        self._health_badge = _badge("Playback unavailable")
        self._health_badge.add_css_class("error")
        self._health_badge.set_visible(False)
        self._borked_reason: str | None = None
        badges.append(self._health_badge)
        self._frame.add_overlay(badges)

        caption = Gtk.Label(label=item.name)
        caption.set_ellipsize(Pango.EllipsizeMode.END)
        caption.set_max_width_chars(24)
        caption.add_css_class("caption")

        self.append(self._frame)
        self.append(caption)

        # Keep the two primary verbs visible without covering the preview.
        # These intentionally do not use wio-tile-action: that class fades
        # secondary overlay controls until hover/focus.
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._apply = Gtk.Button(label="Apply")
        self._apply.set_hexpand(True)
        self._apply.set_sensitive(False)
        self._apply.set_tooltip_text("Apply to all displays as Quick choice")
        self._apply_targets = Gtk.MenuButton(label="Apply to…")
        self._apply_targets.set_hexpand(True)
        self._apply_targets.set_visible(False)
        self._apply_targets.set_tooltip_text("Choose a display, or explicitly choose all displays")
        self._edit = Gtk.Button(label="Edit")
        self._edit.set_hexpand(True)
        self._edit.set_tooltip_text(f"Edit {item.name}: motion, still and colours")
        actions.append(self._apply)
        actions.append(self._apply_targets)
        actions.append(self._edit)
        self.append(actions)
        self._can_apply = False

        # Claim secondary presses before FlowBox sees them. Neither this
        # gesture nor the keyboard menu shortcut is a playback route.
        self._secondary_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        self._secondary_click.connect("pressed", self._on_secondary_pressed)
        self.add_controller(self._secondary_click)
        self._menu_keys = Gtk.EventControllerKey()
        self._menu_keys.connect("key-pressed", self._on_menu_key)
        self.add_controller(self._menu_keys)

    def show_thumbnail(self, texture: Gdk.Texture | None) -> None:
        """Take the decoded thumbnail. Already decoded, on a worker thread."""
        self._spinner.set_visible(False)
        if texture is None:
            # No preview available; the name still identifies it.
            self._picture.set_paintable(None)
            self.add_css_class("wio-tile-blank")
            return
        self._picture.set_paintable(texture)

    def set_current(self, current: bool) -> None:
        if current:
            self.add_css_class("wio-tile-current")
        else:
            self.remove_css_class("wio-tile-current")

    def set_borked(self, reason: str | None) -> None:
        """Expose a durable runtime incompatibility on the media itself."""
        if (self._borked_reason is None) != (reason is None):
            # An already-open healthy menu contains Quick choice actions;
            # discard it immediately when health crosses that boundary.
            self._menu.set_menu_model(None)
            self._apply_targets.set_menu_model(None)
        self._borked_reason = reason
        self._apply.set_sensitive(reason is None and self._can_apply)
        self._apply_targets.set_sensitive(reason is None and self._can_apply)
        self._apply.set_tooltip_text(
            f"Playback unavailable: {reason}" if reason else "Apply to all displays as Quick choice"
        )
        self._apply_targets.set_tooltip_text(
            f"Playback unavailable: {reason}"
            if reason
            else "Choose a display, or explicitly choose all displays"
        )
        self._health_badge.set_visible(reason is not None)
        self._health_badge.set_tooltip_text(reason)
        if reason is None:
            self.remove_css_class("wio-tile-borked")
            self._frame.set_tooltip_text("Click to edit · right-click for actions")
        else:
            self.add_css_class("wio-tile-borked")
            self._frame.set_tooltip_text(
                "Playback disabled · left-click for details and removal options"
            )

    def set_favourite(self, favourite: bool) -> None:
        """Show whether this one is starred. Never reads as a click."""
        self._reflecting = True
        try:
            self._star.set_active(favourite)
        finally:
            self._reflecting = False
        self._star.set_icon_name("starred-symbolic" if favourite else "non-starred-symbolic")
        self._star.set_tooltip_text("Remove from favourites" if favourite else "Add to favourites")

    def set_menu(self, build: Callable[[MediaItem], Gio.MenuModel]) -> None:
        """Build the action menu lazily, using current state on each opening.

        `set_menu_model` builds the popover there and then, which is 160 ms
        across six hundred tiles for menus almost none of which are ever
        opened -- more than half the cost of building the grid. Deferring it
        keeps that work off the grid population path. Rebuilding on opening
        also avoids stale favourite, pairing and display-target actions.
        """

        def create(button: Gtk.MenuButton) -> None:
            button.set_menu_model(build(self.item))

        self._menu.set_create_popup_func(create)

    def connect_actions(
        self,
        on_edit: Callable[[MediaItem], None],
        on_apply: Callable[[MediaItem], None] | None,
        apply_menu_for: Callable[[MediaItem], Gio.MenuModel] | None,
    ) -> None:
        """Native buttons consume their activation without activating FlowBox."""
        self._edit.connect("clicked", lambda _button: on_edit(self.item))
        if on_apply is not None:
            self._apply.connect("clicked", lambda _button: self._apply_if_healthy(on_apply))
        if apply_menu_for is not None:
            self._apply_targets.set_create_popup_func(
                lambda button: button.set_menu_model(apply_menu_for(self.item))
            )
        self._can_apply = on_apply is not None
        self.set_borked(self._borked_reason)

    def _apply_if_healthy(self, on_apply: Callable[[MediaItem], None]) -> None:
        # A display-mode change can hide the direct button while an activation
        # is pending. Never let that stale activation imply all displays.
        if self._borked_reason is None and self._apply.get_visible():
            on_apply(self.item)

    def set_apply_targeting(self, independent: bool) -> None:
        """Independent displays require a target; mirrored playback does not."""
        self._apply.set_visible(not independent)
        self._apply_targets.set_visible(independent)

    def _on_secondary_pressed(
        self, gesture: Gtk.GestureClick, _presses: int, _x: float, _y: float
    ) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._menu.popup()

    def _on_menu_key(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Menu or (
            keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK
        ):
            self._menu.popup()
            return True
        return False

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
    """A scrolling grid with separate editing, applying and context actions."""

    def __init__(
        self,
        loader: ThumbnailLoader,
        on_activate: Callable[[MediaItem], None],
        on_favourite: Callable[[MediaItem, bool], None] | None = None,
        menu_for: Callable[[MediaItem], Gio.MenuModel] | None = None,
        on_apply: Callable[[MediaItem], None] | None = None,
        apply_menu_for: Callable[[MediaItem], Gio.MenuModel] | None = None,
    ) -> None:
        super().__init__()
        self._loader = loader
        self._on_activate = on_activate
        self._on_apply = on_apply
        self._apply_menu_for = apply_menu_for
        self._independent = False
        self._on_favourite = on_favourite
        # The window builds the menus, because the window owns the actions they
        # point at. The grid only knows where to hang one.
        self._menu_for = menu_for
        #: Which paths are starred. Held rather than looked up per tile so the
        #: filter and the tiles cannot disagree within one pass.
        self._favourites: frozenset[Path] = frozenset()
        self._borked: dict[Path, str] = {}
        self._tiles: dict[Path, WallpaperTile] = {}
        self._items: tuple[MediaItem, ...] = ()
        self._matches: tuple[MediaItem, ...] = ()
        self._query = library_filter.Query()
        self._materialized_limit = MEDIA_PAGE_SIZE
        self._current: frozenset[Path] = frozenset()
        #: Where each visible item sits in the current order. Membership is the
        #: complete-inventory filter and the value is the sort, both decided by
        #: `library.filter`.  Only a bounded prefix owns GTK widgets.
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
        # The FlowBox does the hiding and reordering for the bounded widget
        # set. Matching and sorting themselves still cover the full inventory.
        self._flow.set_filter_func(self._is_visible)
        self._flow.set_sort_func(self._compare)

        self._more = Gtk.Button()
        self._more.set_halign(Gtk.Align.CENTER)
        self._more.set_margin_bottom(18)
        self._more.set_tooltip_text("Materialize another page of wallpaper previews")
        self._more.connect("clicked", self._show_more)

        self._page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._page.set_valign(Gtk.Align.START)
        self._page.append(self._flow)
        self._page.append(self._more)

        self._empty = Adw.StatusPage(
            title="No wallpapers found",
            description="Nothing under the configured roots. Choose or check them in Settings.",
            icon_name="image-x-generic-symbolic",
        )

        # A search that matches nothing is not an empty library, and saying so
        # in the empty state above would tell a user with six hundred
        # wallpapers to go and check their wallpaper directory.
        self._unmatched = Adw.StatusPage(icon_name="system-search-symbolic")

        self._stack = Gtk.Stack()
        self._stack.add_named(self._page, "grid")
        self._stack.add_named(self._empty, "empty")
        self._stack.add_named(self._unmatched, "unmatched")

        self.set_child(self._stack)
        self.set_vexpand(True)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    def _on_child_activated(self, _flow: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        tile = child.get_child()
        if isinstance(tile, WallpaperTile):
            self._on_activate(tile.item)

    def populate(
        self,
        items: tuple[MediaItem, ...],
        current: Path | _PreserveCurrent | None = _PRESERVE_CURRENT,
    ) -> None:
        """Bring the grid into line with ``items``, keeping search and sort.

        The model is the complete library; the widget tree is only the loaded
        prefix of the filtered/sorted result.  Rescans are not rare -- one
        follows every download and every batch of generated stills -- so the
        loaded-page limit and every unchanged tile survive them.

        A tile is reused when its `MediaItem` is unchanged. The comparison is
        the whole frozen dataclass on purpose: size and mtime are what the
        thumbnail cache is keyed on, and kind, pairing, ownership and provider
        are what the badges say, so anything that moves is something the tile
        is drawing.
        """
        self._items = items
        if not isinstance(current, _PreserveCurrent):
            self._current = frozenset(() if current is None else (current,))
        self._apply_query()

    def _on_thumbnail(self, item: MediaItem, texture: Gdk.Texture | None) -> None:
        tile = self._tiles.get(item.path)
        # A rescan may replace an item at the same path while its old decode is
        # still in flight. Never paint that stale result onto the new tile.
        if tile is not None and tile.item == item:
            tile.show_thumbnail(texture)

    # -- searching, filtering, sorting -------------------------------------

    def set_query(self, query: library_filter.Query) -> None:
        """Narrow or reorder the complete inventory.

        A changed query starts a fresh bounded page.  Matching and ordering
        still inspect every `MediaItem`, so a result at position 4,095 appears
        immediately without constructing the preceding 4,095 tiles.
        """
        if query != self._query:
            self._materialized_limit = MEDIA_PAGE_SIZE
        self._query = query
        self._apply_query()

    @property
    def visible_count(self) -> int:
        """How many full-inventory items match the current query."""
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

    def set_borked(self, records: dict[Path, str]) -> None:
        """Update health badges without replacing thumbnails or losing scroll."""
        changed = self._borked.keys() ^ records.keys()
        changed.update(
            path
            for path in self._borked.keys() & records.keys()
            if self._borked[path] != records[path]
        )
        self._borked = dict(records)
        for path in changed:
            tile = self._tiles.get(path)
            if tile is not None:
                tile.set_borked(self._borked.get(path))

    def set_apply_targeting(self, independent: bool) -> None:
        self._independent = independent
        for tile in self._tiles.values():
            tile.set_apply_targeting(independent)

    def _apply_query(self) -> None:
        self._matches = library_filter.apply(self._items, self._query, self._favourites)
        self._positions = {item.path: index for index, item in enumerate(self._matches)}
        self._reconcile_tiles()

        if not self._items:
            self._stack.set_visible_child_name("empty")
        elif not self._matches:
            self._unmatched.set_title(f"No {library_filter.describe(self._query)}")
            self._unmatched.set_description(
                "Nothing in your library matches. Clear the search, or widen the filter."
            )
            self._stack.set_visible_child_name("unmatched")
        else:
            self._stack.set_visible_child_name("grid")

    def _reconcile_tiles(self) -> None:
        """Diff the bounded materialized page against the full result model."""
        wanted_paths = {item.path for item in self._matches[: self._materialized_limit]}

        # Current wallpapers are the grid's most important status.  Keep the
        # (normally one to three) current paths visible even when the chosen
        # sort puts them beyond the first page.  Likewise, if a rescan lands
        # while focus is inside a tile that still matches, do not destroy the
        # widget from under the keyboard merely because its rank crossed the
        # page boundary.
        wanted_paths.update(path for path in self._current if path in self._positions)
        focused = self._focused_tile_path()
        if focused is not None and focused in self._positions:
            wanted_paths.add(focused)

        wanted = {item.path: item for item in self._matches if item.path in wanted_paths}
        for path, tile in list(self._tiles.items()):
            replacement = wanted.get(path)
            if replacement is None or replacement != tile.item:
                self._flow.remove(tile)
                del self._tiles[path]

        for item in self._matches:
            if item.path not in wanted or item.path in self._tiles:
                continue
            tile = WallpaperTile(item)
            if self._on_favourite is not None:
                tile.connect_favourite(self._on_favourite)
            if self._menu_for is not None:
                tile.set_menu(self._menu_for)
            tile.connect_actions(self._on_activate, self._on_apply, self._apply_menu_for)
            tile.set_apply_targeting(self._independent)
            tile.set_favourite(item.path in self._favourites)
            tile.set_borked(self._borked.get(item.path))
            tile.set_current(item.path in self._current)
            self._tiles[item.path] = tile
            self._flow.append(tile)
            # Keyboard navigation initially focuses FlowBoxChild itself, not
            # its inner tile. Listen on that common ancestor so Menu/Shift+F10
            # also work there, and bubble from Apply/Edit exactly once.
            child = tile.get_parent()
            if isinstance(child, Gtk.FlowBoxChild):
                tile.remove_controller(tile._menu_keys)
                child.add_controller(tile._menu_keys)
            self._loader.request(item, self._on_thumbnail)

        # Reused tiles retain their current marker; update it after every diff
        # because runtime truth can change without the media model changing.
        for path, tile in self._tiles.items():
            tile.set_current(path in self._current)
        self._flow.invalidate_filter()
        self._flow.invalidate_sort()
        self._update_more()

    def _focused_tile_path(self) -> Path | None:
        """Return the tile containing keyboard focus, if this grid has it."""
        root = self.get_root()
        if not isinstance(root, Gtk.Window):
            return None
        focused = root.get_focus()
        while focused is not None:
            if isinstance(focused, WallpaperTile):
                return focused.item.path
            focused = focused.get_parent()
        return None

    def _update_more(self) -> None:
        shown = len(self._tiles)
        remaining = max(0, len(self._matches) - shown)
        next_limit = min(len(self._matches), self._materialized_limit + MEDIA_PAGE_SIZE)
        materialized = self._tiles.keys()
        amount = sum(item.path not in materialized for item in self._matches[:next_limit])
        self._more.set_visible(remaining > 0)
        self._more.set_label(f"Load {amount} more · {shown} of {len(self._matches)} shown")

    def _show_more(self, _button: Gtk.Button) -> None:
        """Materialize one more page without replacing the existing page."""
        self._materialized_limit += MEDIA_PAGE_SIZE
        self._reconcile_tiles()

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
        self.set_current_many(() if current is None else (current,))

    def set_current_many(self, current: Iterable[Path]) -> None:
        """Highlight every wallpaper currently shown across the displays."""
        selected = frozenset(current)
        if selected == self._current:
            return
        self._current = selected
        self._reconcile_tiles()
