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

from gi.repository import Adw, Gio, Gtk  # noqa: E402

from wall_in_one.library.filter import Query  # noqa: E402
from wall_in_one.library.model import Kind, MediaItem, Ownership  # noqa: E402
from wall_in_one.ui.grid import WallpaperGrid, WallpaperTile  # noqa: E402
from wall_in_one.ui.thumbnails import Callback, ThumbnailLoader  # noqa: E402
from wall_in_one.ui.window import ACCELERATORS  # noqa: E402


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


# -- accelerators ---------------------------------------------------------
#
# One table drives the keys, the shortcuts dialogue and this. The failure it
# guards against is the ordinary one: a key renamed in the handler and left
# alone in the dialogue, so the app advertises a shortcut that does nothing.


def test_every_accelerator_names_an_action_the_window_has() -> None:
    """`window.close` is GTK's own; everything else has to be ours."""
    window_actions = {action for _s, _a, action, _d in ACCELERATORS if action.startswith("win.")}
    missing = {action for action in window_actions if not _action_exists(action)}
    assert missing == set()


def _action_exists(qualified: str) -> bool:
    from wall_in_one.ui import window as window_module

    name = qualified.removeprefix("win.")
    source = Path(window_module.__file__).read_text(encoding="utf-8")
    return f'"{name}"' in source


def test_no_accelerator_is_bound_twice() -> None:
    """Two actions on one key means one of them silently never fires."""
    keys = [accelerator for _s, accelerator, _a, _d in ACCELERATORS]
    assert len(keys) == len(set(keys))


def test_every_accelerator_parses() -> None:
    from gi.repository import Gtk as _Gtk

    for _section, accelerator, _action, description in ACCELERATORS:
        ok, key, _mods = _Gtk.accelerator_parse(accelerator)
        assert ok and key, f"{accelerator!r} for {description!r} is not a valid accelerator"


def test_every_accelerator_is_modified() -> None:
    """The search box holds focus for seconds at a time, and a bare key would
    land in it rather than changing the wallpaper."""
    for _section, accelerator, _action, description in ACCELERATORS:
        assert accelerator.startswith(("<", "F")), f"{description!r} uses a bare key"


def test_the_shortcuts_dialogue_shows_every_accelerator() -> None:
    from wall_in_one.ui.window import _SHORTCUTS

    listed = {
        accelerator for _title, entries in _SHORTCUTS for accelerator, _description in entries
    }
    assert listed == {accelerator for _s, accelerator, _a, _d in ACCELERATORS}


def test_every_declared_accelerator_is_actually_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declaring a key and never binding it is the failure mode that looks
    fine everywhere: the dialogue lists it, the action exists, and pressing it
    does nothing.

    Compared in GTK's normalised spelling, because `set_accels_for_action`
    stores `<Shift><Control>r` for what the table calls `<Control><Shift>R`.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from wall_in_one.ui.app import Application

    application = Application()
    application._install_accelerators()

    def normalised(accelerator: str) -> str | None:
        ok, key, modifiers = Gtk.accelerator_parse(accelerator)
        return Gtk.accelerator_name(key, modifiers) if ok else None

    for _section, accelerator, action, description in ACCELERATORS:
        bound = {normalised(each) for each in application.get_accels_for_action(action)}
        assert normalised(accelerator) in bound, f"{description!r} is declared but not bound"


def test_service_mode_suppresses_the_initial_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from wall_in_one.ui.app import Application

    service = Application(service=True)
    graphical = Application()

    assert service.get_flags() & Gio.ApplicationFlags.IS_SERVICE
    assert not (graphical.get_flags() & Gio.ApplicationFlags.IS_SERVICE)


def test_closing_the_window_keeps_only_the_service_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from wall_in_one.ui.app import Application

    application = Application()
    window = object()
    application._window = window  # type: ignore[assignment]

    assert application._on_close_request(window) is False  # type: ignore[arg-type]
    assert application._window is None


# -- decoding off the main thread -----------------------------------------
#
# `ThumbnailLoader` hands back a decoded `Gdk.Texture` rather than a path, and
# does the decoding on its pool. A warm cache of six hundred thumbnails is
# 372 ms of `Gdk.Texture.new_from_filename` on the main thread otherwise --
# more than building all the widgets. What is tested here is the worker half,
# which needs no main loop to run.


def _png(path: Path) -> Path:
    """A real, decodable PNG. One pixel is enough to prove a decode happened."""
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=8x8:d=1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_the_worker_returns_a_texture_not_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gi.repository import Gdk

    from wall_in_one.ui.thumbnails import ThumbnailLoader as Loader

    picture = _png(tmp_path / "thumb.png")
    monkeypatch.setattr("wall_in_one.ui.thumbnails.thumbnails.lookup", lambda _item: picture)
    texture = Loader._texture_for(item("a"))
    assert isinstance(texture, Gdk.Texture)
    assert (texture.get_width(), texture.get_height()) == (8, 8)


def test_a_texture_made_off_the_main_thread_goes_into_a_widget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole premise. GDK textures are immutable and safe to create on a
    worker, and one made there has to drop straight into a `Gtk.Picture`."""
    from concurrent.futures import ThreadPoolExecutor

    from wall_in_one.ui.thumbnails import ThumbnailLoader as Loader

    picture = _png(tmp_path / "thumb.png")
    monkeypatch.setattr("wall_in_one.ui.thumbnails.thumbnails.lookup", lambda _item: picture)
    with ThreadPoolExecutor(max_workers=1) as pool:
        texture = pool.submit(Loader._texture_for, item("a")).result()
    tile = WallpaperTile(item("a"))
    tile.show_thumbnail(texture)
    assert tile._picture.get_paintable() is texture


def test_a_file_gdk_cannot_decode_is_a_blank_tile_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg and GDK do not agree on every format, and the tile still has a
    name under it either way."""
    from wall_in_one.ui.thumbnails import ThumbnailLoader as Loader

    rubbish = tmp_path / "thumb.png"
    rubbish.write_bytes(b"not a picture")
    monkeypatch.setattr("wall_in_one.ui.thumbnails.thumbnails.lookup", lambda _item: rubbish)
    assert Loader._texture_for(item("a")) is None


def test_a_wallpaper_that_cannot_be_thumbnailed_is_a_blank_tile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wall_in_one import thumbnails as cache
    from wall_in_one.ui.thumbnails import ThumbnailLoader as Loader

    monkeypatch.setattr("wall_in_one.ui.thumbnails.thumbnails.lookup", lambda _item: None)

    def refuse(_item: MediaItem) -> Path:
        raise cache.ThumbnailError("nope")

    monkeypatch.setattr("wall_in_one.ui.thumbnails.thumbnails.generate", refuse)
    assert Loader._texture_for(item("a")) is None


# -- the window and the store ---------------------------------------------


def test_showing_the_library_repushes_the_favourites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A star can move without this window touching it -- `ctl favourite`
    reaches the session's store directly -- so the tiles have to be told again
    every time the library is shown, not only when the star itself is clicked.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", lambda *a, **k: None)

    from wall_in_one import config
    from wall_in_one.library import scan
    from wall_in_one.session import Session
    from wall_in_one.theme import source
    from wall_in_one.ui.window import MainWindow

    root = tmp_path / "lib"
    root.mkdir()
    wallpaper = root / "one.png"
    _png(wallpaper)

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.RepushTest")
            self.settings = config.Settings()
            self.resolved_palette = source.resolve()
            self.session = Session(self.settings, scanner=lambda _roots: scan.scan((root,)))
            self.session.refresh()

        def refresh_library(self) -> None: ...

    application = FakeApp()
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    window.show_library(application.session)
    tile = window._grid._tiles[wallpaper]
    assert not tile._star.get_active()

    # Somebody else stars it -- the socket, not this window.
    application.session.favourites.add(wallpaper)
    window.show_library(application.session)

    assert window._grid._tiles[wallpaper]._star.get_active()
    application.session.shutdown()


def test_main_window_keeps_pairings_inside_the_media_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pairings are edited from Media rather than duplicated as another tab."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    from wall_in_one import config
    from wall_in_one.library.model import Library
    from wall_in_one.session import Session
    from wall_in_one.theme import source
    from wall_in_one.ui.window import MainWindow

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.ManagementPagesTest")
            self.settings = config.Settings()
            self.resolved_palette = source.resolve()
            self.session = Session(
                self.settings,
                scanner=lambda _roots: Library(roots=(), items=()),
            )
            self.session.refresh()

        def refresh_library(self) -> None: ...

    application = FakeApp()
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    window.show_library(application.session)

    for page in ("browse", "media", "playlists", "schedules", "settings"):
        assert window._stack.get_child_by_name(page) is not None
    assert window._stack.get_child_by_name("pairings") is None
    assert window._content_stack.get_child_by_name("pairing-editor") is window._pairings_page

    window.destroy()
    application.session.shutdown()


def test_management_pages_render_real_pairing_playlist_and_schedule_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-empty store reaches every page without falling back to ``ctl``."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "wall_in_one.ui.pairings_page.SchemePreviewLoader.request",
        lambda *_arguments: None,
    )

    from wall_in_one import config
    from wall_in_one.library import scan
    from wall_in_one.session import Session
    from wall_in_one.ui.window import MainWindow

    root = tmp_path / "library"
    root.mkdir()
    wallpaper = root / "one.png"
    _png(wallpaper)

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.ManagementDataTest")
            self.settings = config.Settings(roots=(root,))
            self.resolved_palette = None
            self.session = Session(self.settings, scanner=lambda _roots: scan.scan((root,)))
            self.session.refresh()

        def refresh_library(self) -> None: ...

        def window_report(self, _message: str) -> None: ...

    application = FakeApp()
    made = application.session.playlists.create("Evening")
    application.session.playlists.add(made.id, wallpaper)
    application.session.schedules.add(made.id, weekdays=["sat"], start="22:00", end="06:00")
    application.session.displays.assign("DP-1", made.id)
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    window.show_library(application.session)

    window._on_tile_activated(application.session.library.items[0])
    assert window._content_stack.get_visible_child_name() == "pairing-editor"
    assert window._pairings_page._selected is not None

    window._close_pairing_editor()
    for page in ("playlists", "schedules"):
        window._stack.set_visible_child_name(page)
        assert window._stack.get_visible_child_name() == page
    assert window._playlists_page._selected == made.id
    assert application.session.schedules.rules[0].describe() == "sat 22:00-06:00"
    schedule_editor = window._schedules_page
    assert len(schedule_editor._months) == 12
    assert len(schedule_editor._weekdays) == 7
    assert all(isinstance(button, Gtk.ToggleButton) for button in schedule_editor._months)
    assert isinstance(schedule_editor._start_hour, Gtk.DropDown)
    assert isinstance(schedule_editor._start_minute, Gtk.DropDown)
    window.destroy()
    application.session.shutdown()
