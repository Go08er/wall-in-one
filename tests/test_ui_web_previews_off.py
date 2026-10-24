"""A web wallpaper's tile, in both interfaces, while web wallpapers are off.

The classic grid's tile and the next interface's card are plain placeholders:
they look like the "Can't play" tiles, keep the "Scene \u00b7 web" badge and say
"Web wallpapers are off". Neither interface asks for a picture of one. Fixture:
`tests/web_previews.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.web_previews import Fixture, _Watch  # noqa: E402
from wall_in_one.library import workshop  # noqa: E402
from wall_in_one.library.model import MediaItem  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _labels(widget: Any) -> list[str]:
    found: list[str] = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label) and child.get_visible():
            found.append(child.get_label())
        found.extend(_labels(child))
        child = child.get_next_sibling()
    return found


def test_the_classic_tile_is_a_plain_placeholder_and_the_grid_asks_for_no_picture(
    web_folder: Fixture, watch: _Watch
) -> None:
    from wall_in_one.ui.grid import WallpaperGrid, WallpaperTile
    from wall_in_one.ui.thumbnails import ThumbnailLoader

    tile = WallpaperTile(web_folder.web)
    assert tile.previews_off
    labels = _labels(tile)
    assert workshop.WEB_PREVIEW_OFF_NOTE in labels
    assert "Can\u2019t play" in labels
    assert any("web" in label.casefold() and "scene" in label.casefold() for label in labels)
    tile.show_thumbnail(object())  # type: ignore[arg-type]  # a late result is not painted
    assert tile._picture.get_paintable() is None
    assert not WallpaperTile(web_folder.web, allow_web=True).previews_off

    class Recording(ThumbnailLoader):
        def __init__(self) -> None:
            super().__init__(max_workers=1)
            self.requested: list[Path] = []

        def request(self, item: MediaItem, callback: Any) -> None:
            self.requested.append(item.path)

    loader = Recording()
    grid = WallpaperGrid(loader, lambda item: None)
    grid.populate((web_folder.web, web_folder.preset, web_folder.scene))
    assert loader.requested == [web_folder.scene.path]
    grid.set_web_allowed(True)
    assert sorted(loader.requested) == sorted(
        [web_folder.scene.path, web_folder.web.path, web_folder.preset.path]
    )
    assert not any(tile.previews_off for tile in grid._tiles.values())
    grid.set_web_allowed(False)
    assert {path for path, tile in grid._tiles.items() if tile.previews_off} == {
        web_folder.web.path,
        web_folder.preset.path,
    }
    loader.shutdown()
    assert watch.clean(), (watch.opens, watch.started)
    assert watch.clean(), (watch.opens, watch.started)


def test_the_next_interface_gives_a_web_page_no_picture_and_says_why(
    web_folder: Fixture, watch: _Watch
) -> None:
    from wall_in_one.library import pairings
    from wall_in_one.ui.next.library_thumbnails import LibraryThumbnails
    from wall_in_one.ui.next.real_state import wallpaper_view
    from wall_in_one.ui.next.widgets import WallpaperCard

    class Loader:
        def __init__(self) -> None:
            self.requested: list[MediaItem] = []

        def request(self, item: MediaItem, callback: Any) -> None:
            self.requested.append(item)

        def shutdown(self) -> None:
            pass

    web = {"on": False}
    loader = Loader()
    provider = LibraryThumbnails(loader, web_allowed=lambda: web["on"])  # type: ignore[arg-type]
    delivered: list[object] = []
    assert provider.request(web_folder.web, 480, 270, delivered.append) is None
    assert loader.requested == [] and provider.pending() == 0
    # What was drawn while web was on is not handed out while it is off.
    provider._ready[web_folder.web] = object()  # type: ignore[assignment]
    assert provider.cached(web_folder.web, 480, 270) is None
    assert provider.request(web_folder.preset, 480, 270, delivered.append) is None
    assert loader.requested == []
    web["on"] = True
    provider.web_wallpapers_changed()
    assert provider.request(web_folder.web, 480, 270, delivered.append) is None
    assert loader.requested == [web_folder.web]
    web["on"] = False
    provider.shutdown()

    view = wallpaper_view(
        web_folder.web, favourite=False, pairing=None, health=pairings.Health(), allow_web=False
    )
    card = WallpaperCard(view)
    labels = _labels(card)
    assert workshop.WEB_PREVIEW_OFF_NOTE in labels
    assert "Can\u2019t play" in labels
    assert any(label.startswith("Scene") for label in labels)
    on = wallpaper_view(
        web_folder.web, favourite=False, pairing=None, health=pairings.Health(), allow_web=True
    )
    assert workshop.WEB_PREVIEW_OFF_NOTE not in _labels(WallpaperCard(on))
    for refused in (web_folder.malformed, web_folder.orphan):
        refused_view = wallpaper_view(
            refused, favourite=False, pairing=None, health=pairings.Health(), allow_web=False
        )
        assert workshop.WEB_PREVIEW_OFF_NOTE in _labels(WallpaperCard(refused_view))
    control = wallpaper_view(
        web_folder.bad_scene,
        favourite=False,
        pairing=None,
        health=pairings.Health(),
        allow_web=False,
    )
    assert workshop.WEB_PREVIEW_OFF_NOTE not in _labels(WallpaperCard(control))
    assert watch.clean(), (watch.opens, watch.started)
