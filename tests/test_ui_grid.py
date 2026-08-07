"""The grid's diffing, which is the difference between 15 ms and 620 ms.

These are the first tests in the suite that build real widgets, so they carry
the `gui` marker the packaged build excludes with `-m "not gui"`: GTK needs a
display and the Nix check sandbox has none. Everything they touch is in
process -- no window is ever presented, nothing is drawn, and nothing outside
`tmp_path` is read or written.

What is being pinned is `populate`. It used to tear down every tile and build
them again, which was invisible at five wallpapers and about six hundred
milliseconds of frozen window at six hundred. Rescans are not rare any more --
one follows every download and every batch of generated stills -- so the diff
has to be right as well as quick: a tile wrongly reused shows stale badges, and
a tile wrongly rebuilt throws away a decoded texture for nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from wall_in_one.library.filter import Query  # noqa: E402
from wall_in_one.library.model import Kind, MediaItem, Ownership  # noqa: E402
from wall_in_one.ui.grid import WallpaperGrid, WallpaperTile  # noqa: E402
from wall_in_one.ui.thumbnails import Callback, ThumbnailLoader  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    """A display, or these do not run at all.

    The `gui` marker keeps them out of the packaged build, which has no
    display; this is the belt for anyone running the suite by hand somewhere
    headless, where `Gtk.init` raises rather than returning.
    """
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class CountingLoader(ThumbnailLoader):
    """A loader that records what it was asked for and fetches nothing."""

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.requested: list[Path] = []

    def request(self, item: MediaItem, callback: Callback) -> None:
        del callback
        self.requested.append(item.path)


@pytest.fixture
def loader() -> CountingLoader:
    return CountingLoader()


@pytest.fixture
def grid(loader: CountingLoader) -> WallpaperGrid:
    return WallpaperGrid(loader, lambda _item: None)


def item(
    name: str,
    kind: Kind = Kind.STILL,
    *,
    mtime: int = 0,
    still: Path | None = None,
    ownership: Ownership = Ownership.USER,
) -> MediaItem:
    suffix = ".mp4" if kind is Kind.VIDEO else ".png"
    return MediaItem(
        path=Path("/w") / f"{name}{suffix}",
        kind=kind,
        size=1,
        mtime=mtime,
        ownership=ownership,
        paired_still=still,
    )


def tiles_in(grid: WallpaperGrid) -> list[str]:
    """Tile names actually parented by the FlowBox, not just remembered."""
    found = []
    child = grid._flow.get_first_child()
    while child is not None:
        inner = child.get_child() if isinstance(child, Gtk.FlowBoxChild) else None
        if isinstance(inner, WallpaperTile):
            found.append(inner.item.name)
        child = child.get_next_sibling()
    return sorted(found)


# -- the diff -------------------------------------------------------------


def test_populating_an_empty_grid_builds_every_tile(
    grid: WallpaperGrid, loader: CountingLoader
) -> None:
    grid.populate((item("a"), item("b")))
    assert tiles_in(grid) == ["a", "b"]
    assert len(loader.requested) == 2


def test_populating_again_with_the_same_items_builds_nothing(
    grid: WallpaperGrid, loader: CountingLoader
) -> None:
    """The whole point: a rescan that changed nothing must cost nothing."""
    items = (item("a"), item("b"))
    grid.populate(items)
    loader.requested.clear()
    grid.populate(items)
    assert tiles_in(grid) == ["a", "b"]
    assert loader.requested == []


def test_a_reused_tile_is_the_same_widget(grid: WallpaperGrid) -> None:
    """Rebuilding it would throw away a decoded texture for nothing."""
    items = (item("a"),)
    grid.populate(items)
    before = grid._tiles[items[0].path]
    grid.populate(items)
    assert grid._tiles[items[0].path] is before


def test_a_removed_wallpaper_loses_its_tile(grid: WallpaperGrid) -> None:
    grid.populate((item("a"), item("b")))
    grid.populate((item("a"),))
    assert tiles_in(grid) == ["a"]
    assert Path("/w/b.png") not in grid._tiles


def test_a_new_wallpaper_gains_one(grid: WallpaperGrid, loader: CountingLoader) -> None:
    grid.populate((item("a"),))
    loader.requested.clear()
    grid.populate((item("a"), item("b")))
    assert tiles_in(grid) == ["a", "b"]
    assert loader.requested == [Path("/w/b.png")]


def test_a_wallpaper_edited_in_place_is_rebuilt(
    grid: WallpaperGrid, loader: CountingLoader
) -> None:
    """mtime is what the thumbnail cache is keyed on, so a tile that kept its
    old texture would be showing the previous picture."""
    grid.populate((item("a", mtime=1),))
    before = grid._tiles[Path("/w/a.png")]
    loader.requested.clear()
    grid.populate((item("a", mtime=2),))
    assert grid._tiles[Path("/w/a.png")] is not before
    assert loader.requested == [Path("/w/a.png")]


def test_a_video_that_gains_a_still_is_rebuilt(grid: WallpaperGrid) -> None:
    """Its badge says "Video (no still)" until it is, which is exactly what
    the background still maker changes underneath a running grid."""
    grid.populate((item("clip", Kind.VIDEO),))
    before = grid._tiles[Path("/w/clip.mp4")]
    grid.populate((item("clip", Kind.VIDEO, still=Path("/w/clip.png")),))
    assert grid._tiles[Path("/w/clip.mp4")] is not before


def test_everything_going_away_leaves_no_tiles(grid: WallpaperGrid) -> None:
    grid.populate((item("a"), item("b")))
    grid.populate(())
    assert tiles_in(grid) == []
    assert grid._tiles == {}


def test_a_wholesale_replacement_keeps_nothing(grid: WallpaperGrid) -> None:
    """Pointing the app at a different folder shares no paths with the old one."""
    grid.populate((item("a"), item("b")))
    grid.populate((item("c"), item("d")))
    assert tiles_in(grid) == ["c", "d"]


# -- what survives a diff -------------------------------------------------


def test_the_highlight_follows_the_current_wallpaper(grid: WallpaperGrid) -> None:
    """A reused tile keeps whatever it was told last time, and the wallpaper
    may well have moved since."""
    items = (item("a"), item("b"))
    grid.populate(items, current=Path("/w/a.png"))
    assert grid._tiles[Path("/w/a.png")].has_css_class("wio-tile-current")
    grid.populate(items, current=Path("/w/b.png"))
    assert not grid._tiles[Path("/w/a.png")].has_css_class("wio-tile-current")
    assert grid._tiles[Path("/w/b.png")].has_css_class("wio-tile-current")


def test_a_new_tile_arrives_already_starred(grid: WallpaperGrid) -> None:
    grid.set_favourites(frozenset({Path("/w/a.png")}))
    grid.populate((item("a"), item("b")))
    assert grid._tiles[Path("/w/a.png")]._star.get_active()
    assert not grid._tiles[Path("/w/b.png")]._star.get_active()


def test_stars_survive_a_rescan(grid: WallpaperGrid) -> None:
    items = (item("a"),)
    grid.populate(items)
    grid.set_favourites(frozenset({Path("/w/a.png")}))
    grid.populate(items)
    assert grid._tiles[Path("/w/a.png")]._star.get_active()


def test_the_query_survives_a_rescan(grid: WallpaperGrid) -> None:
    """Otherwise a download would silently clear the user's search."""
    grid.populate((item("snowy-village"), item("cozy-campfire")))
    grid.set_query(Query(text="snow"))
    assert grid.visible_count == 1
    grid.populate((item("snowy-village"), item("cozy-campfire"), item("snowy-peak")))
    assert grid.visible_count == 2
