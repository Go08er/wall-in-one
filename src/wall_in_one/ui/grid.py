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

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from wall_in_one import thumbnails as thumbnail_cache
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


def _badge(text: str) -> Gtk.Widget:
    label = Gtk.Label(label=text)
    label.add_css_class("caption")
    label.add_css_class("wio-badge")
    return label


class WallpaperGrid(Gtk.ScrolledWindow):
    """A scrolling grid of tiles. Activating one applies that wallpaper."""

    def __init__(self, loader: ThumbnailLoader, on_activate: Callable[[MediaItem], None]) -> None:
        super().__init__()
        self._loader = loader
        self._on_activate = on_activate
        self._tiles: dict[Path, WallpaperTile] = {}

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

        self._empty = Adw.StatusPage(
            title="No wallpapers found",
            description="Nothing under the configured roots. Check Noctalia's wallpaper directory.",
            icon_name="image-x-generic-symbolic",
        )

        self._stack = Gtk.Stack()
        self._stack.add_named(self._flow, "grid")
        self._stack.add_named(self._empty, "empty")

        self.set_child(self._stack)
        self.set_vexpand(True)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    def _on_child_activated(self, _flow: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        tile = child.get_child()
        if isinstance(tile, WallpaperTile):
            self._on_activate(tile.item)

    def populate(self, items: tuple[MediaItem, ...], current: Path | None = None) -> None:
        """Rebuild the grid for ``items``."""
        while (child := self._flow.get_first_child()) is not None:
            self._flow.remove(child)
        self._tiles.clear()

        self._stack.set_visible_child_name("grid" if items else "empty")

        for item in items:
            tile = WallpaperTile(item)
            tile.set_current(item.path == current)
            self._tiles[item.path] = tile
            self._flow.append(tile)
            self._loader.request(item, self._on_thumbnail)

    def _on_thumbnail(self, item: MediaItem, path: Path | None) -> None:
        tile = self._tiles.get(item.path)
        if tile is not None:
            tile.show_thumbnail(path)

    def set_current(self, current: Path | None) -> None:
        """Move the "this one is up" highlight without rebuilding anything."""
        for path, tile in self._tiles.items():
            tile.set_current(path == current)
