"""The grid's bounded, identity-preserving materialisation.

These are the first tests in the suite that build real widgets, so they carry
the `gui` marker the packaged build excludes with `-m "not gui"`: GTK needs a
display and the Nix check sandbox has none. Everything they touch is in
process -- no window is ever presented, nothing is drawn, and nothing outside
`tmp_path` is read or written.

What is being pinned is `populate`. It must not tear down unchanged tiles, and
it must not build one tile per item: a 4,096-item library is a small tuple but a
huge GTK tree. Rescans are not rare -- one follows every download and every
batch of generated stills -- so the bounded diff has to be right as well as
quick: a tile wrongly reused shows stale badges, and a tile wrongly rebuilt
throws away a decoded texture for nothing.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from wall_in_one.library.filter import Query, Sort  # noqa: E402
from wall_in_one.library.model import Kind, MediaItem, Ownership  # noqa: E402
from wall_in_one.ui.grid import MEDIA_PAGE_SIZE, WallpaperGrid, WallpaperTile  # noqa: E402
from wall_in_one.ui.thumbnails import Callback, ThumbnailLoader  # noqa: E402
from wall_in_one.ui.window import ACCELERATORS, MainWindow  # noqa: E402


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
    size: int = 1,
    still: Path | None = None,
    ownership: Ownership = Ownership.USER,
) -> MediaItem:
    suffix = ".mp4" if kind is Kind.VIDEO else ".png"
    return MediaItem(
        path=Path("/w") / f"{name}{suffix}",
        kind=kind,
        size=size,
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


def test_populating_a_small_grid_builds_every_tile(
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


def test_borked_health_updates_the_existing_tile_in_place(grid: WallpaperGrid) -> None:
    media = item("a")
    grid.populate((media,))
    tile = grid._tiles[media.path]
    healthy_menu = Gio.Menu()
    healthy_menu.append("Play as Quick choice", "win.apply-wallpaper")
    tile._menu.set_menu_model(healthy_menu)

    grid.set_borked({media.path: "renderer crashed on this wallpaper"})
    assert grid._tiles[media.path] is tile
    assert tile._health_badge.get_visible()
    assert tile._health_badge.get_tooltip_text() == "renderer crashed on this wallpaper"
    assert tile.has_css_class("wio-tile-borked")
    assert tile._menu.get_menu_model() is None
    assert "Playback disabled" in (tile._frame.get_tooltip_text() or "")

    tile._menu.set_menu_model(Gio.Menu())
    grid.set_borked({})
    assert grid._tiles[media.path] is tile
    assert not tile._health_badge.get_visible()
    assert tile._menu.get_menu_model() is None


def _settle_widgets() -> None:
    """Allow native button activation animations and allocation to finish."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.002)


@pytest.mark.parametrize("independent", [False, True])
def test_visible_tile_actions_fit_compact_layout_and_keyboard_activation_dispatches_once(
    loader: CountingLoader, independent: bool
) -> None:
    edited: list[MediaItem] = []
    applied: list[MediaItem] = []
    starred: list[bool] = []
    built: list[str] = []

    def menu(_item: MediaItem) -> Gio.MenuModel:
        built.append("context")
        return Gio.Menu()

    def targets(_item: MediaItem) -> Gio.MenuModel:
        built.append("targets")
        return Gio.Menu()

    grid = WallpaperGrid(
        loader,
        edited.append,
        lambda _item, active: starred.append(active),
        menu,
        applied.append,
        targets,
    )
    grid.set_apply_targeting(independent)
    media = item("a-long-wallpaper-name-that-must-not-hide-actions")
    grid.populate((media,))
    tile = grid._tiles[media.path]
    window = Gtk.Window()
    window.set_default_size(360, 400)
    window.set_child(grid)
    window.present()
    _settle_widgets()
    try:
        apply = tile._apply_targets if independent else tile._apply
        assert apply.get_label() == ("Apply to…" if independent else "Apply")
        assert tile._edit.get_label() == "Edit"
        assert apply.get_visible() and tile._edit.get_visible()
        assert not apply.has_css_class("wio-tile-action")
        assert not tile._edit.has_css_class("wio-tile-action")
        assert tile.get_width() <= window.get_width()
        assert apply.get_width() > 0 and tile._edit.get_width() > 0
        assert tile._edit.grab_focus()
        assert tile._edit.activate()
        _settle_widgets()
        assert edited == [media]
        edited.clear()

        assert apply.grab_focus()
        if independent:
            tile._apply_targets.popup()
            assert built == ["targets"]
            tile._apply_targets.popdown()
        else:
            assert apply.activate()
            _settle_widgets()
            assert applied == [media]
            applied.clear()
        assert edited == []
        assert applied == []

        tile._star.activate()
        _settle_widgets()
        assert starred == [True]
        tile._menu.popup()
        assert built[-1] == "context"
        tile._menu.popdown()
        assert edited == [] and applied == []

        # Exercise the registered secondary-press and keyboard-menu handlers;
        # both open the same context menu, never the Apply path.
        tile._secondary_click.emit("pressed", 1, 20.0, 20.0)
        assert tile._menu.get_active()
        tile._menu.popdown()
        assert tile._menu_keys.emit("key-pressed", Gdk.KEY_F10, 0, Gdk.ModifierType.SHIFT_MASK)
        assert tile._menu.get_active()
        tile._menu.popdown()
        assert tile._menu_keys.emit("key-pressed", Gdk.KEY_Menu, 0, Gdk.ModifierType(0))
        tile._menu.popdown()
        assert not tile._menu_keys.emit("key-pressed", Gdk.KEY_F10, 0, Gdk.ModifierType(0))
        assert edited == [] and applied == []

        child = tile.get_parent()
        assert isinstance(child, Gtk.FlowBoxChild)
        grid._flow.emit("child-activated", child)
        assert edited == [media]
    finally:
        window.destroy()


def test_context_shortcuts_cover_default_tile_and_nested_button_focus_once(
    loader: CountingLoader,
) -> None:
    built: list[MediaItem] = []
    edited: list[MediaItem] = []

    def menu(media: MediaItem) -> Gio.MenuModel:
        built.append(media)
        return Gio.Menu()

    grid = WallpaperGrid(loader, edited.append, menu_for=menu)
    media = item("keyboard-context")
    grid.populate((media,))
    tile = grid._tiles[media.path]
    child = tile.get_parent()
    assert isinstance(child, Gtk.FlowBoxChild)
    window = Gtk.Window()
    window.set_child(grid)
    window.present()
    _settle_widgets()
    try:
        # A controller on tile cannot see events targeting its FlowBoxChild
        # ancestor. Pin the real event-routing owner, not only the handler.
        assert tile._menu_keys.get_widget() is child
        assert not any(
            isinstance(controller, Gtk.EventControllerKey)
            for controller in tile.observe_controllers()
        )
        for focused in (child, tile._edit):
            assert focused.grab_focus()
            assert window.get_focus() is focused
            for keyval, modifiers in (
                (Gdk.KEY_Menu, Gdk.ModifierType(0)),
                (Gdk.KEY_F10, Gdk.ModifierType.SHIFT_MASK),
            ):
                before = len(built)
                assert tile._menu_keys.emit("key-pressed", keyval, 0, modifiers)
                assert tile._menu.get_active()
                assert len(built) == before + 1
                tile._menu.popdown()
        assert edited == []
    finally:
        window.destroy()


def test_blocked_tile_disables_apply_but_keeps_edit_and_recovers_in_place(
    loader: CountingLoader,
) -> None:
    edited: list[MediaItem] = []
    applied: list[MediaItem] = []
    grid = WallpaperGrid(loader, edited.append, on_apply=applied.append)
    media = item("blocked")
    grid.populate((media,))
    tile = grid._tiles[media.path]
    grid.set_borked({media.path: "renderer crashed"})
    assert not tile._apply.get_sensitive()
    assert not tile._apply_targets.get_sensitive()
    assert "renderer crashed" in (tile._apply.get_tooltip_text() or "")
    tile._apply.emit("clicked")
    tile._edit.emit("clicked")
    assert applied == [] and edited == [media]
    grid.set_borked({})
    assert grid._tiles[media.path] is tile
    assert tile._apply.get_sensitive()
    tile._apply.emit("clicked")
    assert applied == [media]


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


def test_every_current_display_wallpaper_is_highlighted(grid: WallpaperGrid) -> None:
    items = (item("a"), item("b"), item("c"))
    grid.populate(items)

    grid.set_current_many((Path("/w/a.png"), Path("/w/c.png")))

    assert grid._tiles[Path("/w/a.png")].has_css_class("wio-tile-current")
    assert not grid._tiles[Path("/w/b.png")].has_css_class("wio-tile-current")
    assert grid._tiles[Path("/w/c.png")].has_css_class("wio-tile-current")


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


# -- bounded materialisation ----------------------------------------------


@pytest.mark.parametrize("count", (600, 4096))
def test_large_library_initial_work_is_bounded_and_fast(loader: CountingLoader, count: int) -> None:
    """Library size affects pure matching, never GTK-card construction.

    The time ceiling is deliberately generous for emulated and debug GTK
    builds.  The card/request counts are the hard invariant; the ceiling also
    catches an accidental expensive full-inventory pass beyond the intended
    string matching and sort.
    """
    grid = WallpaperGrid(loader, lambda _item: None)
    items = tuple(item(f"media-{index:04d}") for index in range(count))

    started = time.perf_counter()
    grid.populate(items)
    elapsed = time.perf_counter() - started

    assert grid.visible_count == count
    assert len(grid._tiles) == MEDIA_PAGE_SIZE
    assert len(tiles_in(grid)) == MEDIA_PAGE_SIZE
    assert len(loader.requested) == MEDIA_PAGE_SIZE
    assert grid._more.get_visible()
    assert grid._more.get_label() == (
        f"Load {MEDIA_PAGE_SIZE} more · {MEDIA_PAGE_SIZE} of {count} shown"
    )
    assert elapsed < 1.0, f"bounded initial grid took {elapsed:.3f}s for {count} items"


def test_load_more_adds_one_page_without_replacing_existing_tiles(
    grid: WallpaperGrid, loader: CountingLoader
) -> None:
    items = tuple(item(f"media-{index:04d}") for index in range(200))
    grid.populate(items)
    first = grid._tiles[items[0].path]

    grid._show_more(grid._more)

    assert len(grid._tiles) == MEDIA_PAGE_SIZE * 2
    assert len(loader.requested) == MEDIA_PAGE_SIZE * 2
    assert grid._tiles[items[0].path] is first
    assert grid._more.get_label() == f"Load 56 more · {MEDIA_PAGE_SIZE * 2} of 200 shown"


def test_search_matches_the_complete_inventory_without_loading_preceding_tiles(
    grid: WallpaperGrid, loader: CountingLoader
) -> None:
    items = tuple(item(f"media-{index:04d}") for index in range(4096))
    wanted = items[-1]
    grid.populate(items)
    loader.requested.clear()

    grid.set_query(Query(text=wanted.name))

    assert grid.visible_count == 1
    assert tuple(grid._tiles) == (wanted.path,)
    assert loader.requested == [wanted.path]
    assert not grid._more.get_visible()


def test_sort_uses_the_complete_inventory_before_materialising_a_page(
    grid: WallpaperGrid,
) -> None:
    items = tuple(
        item(f"media-{index:04d}", size=index + 1) for index in range(MEDIA_PAGE_SIZE * 3)
    )
    grid.populate(items)

    grid.set_query(Query(sort=Sort.LARGEST))

    expected = tuple(each.path for each in reversed(items[-MEDIA_PAGE_SIZE:]))
    assert tuple(grid._tiles) == expected
    assert grid.visible_count == len(items)


def test_off_page_current_displays_are_materialised_and_keep_borked_badges(
    grid: WallpaperGrid,
) -> None:
    items = tuple(item(f"media-{index:04d}") for index in range(600))
    first_current, second_current = items[-2:]
    grid.populate(items)
    grid.set_borked({first_current.path: "renderer crashed"})

    grid.set_current_many((first_current.path, second_current.path))

    assert len(grid._tiles) == MEDIA_PAGE_SIZE + 2
    for current in (first_current, second_current):
        assert grid._tiles[current.path].has_css_class("wio-tile-current")
    borked = grid._tiles[first_current.path]
    assert borked.has_css_class("wio-tile-borked")
    assert borked._health_badge.get_visible()
    assert borked._health_badge.get_tooltip_text() == "renderer crashed"
    assert grid._more.get_label() == (
        f"Load {MEDIA_PAGE_SIZE} more · {MEDIA_PAGE_SIZE + 2} of 600 shown"
    )

    # MainWindow performs a model rescan and then refreshes runtime truth.
    # The ordinary populate must not tear down off-page current pins in that
    # interval or make their highlight visibly blink.
    first_tile = grid._tiles[first_current.path]
    second_tile = grid._tiles[second_current.path]
    grid.populate(items)
    assert grid._tiles[first_current.path] is first_tile
    assert grid._tiles[second_current.path] is second_tile
    assert first_tile.has_css_class("wio-tile-current")
    assert second_tile.has_css_class("wio-tile-current")


def test_rescan_preserves_loaded_pages_scroll_query_focus_and_tile_identity(
    grid: WallpaperGrid,
) -> None:
    items = tuple(item(f"media-{index:04d}") for index in range(200))
    grid.populate(items)
    grid._show_more(grid._more)
    grid.set_query(Query(text="media"))
    focused_item = items[MEDIA_PAGE_SIZE - 1]
    focused_tile = grid._tiles[focused_item.path]
    root = Gtk.Window()
    root.set_child(grid)
    focus_taken = focused_tile._star.grab_focus()
    focused = root.get_focus()
    adjustment = grid.get_vadjustment()
    adjustment.configure(17, 0, 1000, 1, 10, 10)

    try:
        # This new first result pushes the focused item just beyond the page.
        # It remains as one bounded focus pin rather than being destroyed.
        replacement = (item("media-!first"), *items)
        grid.populate(replacement)

        assert grid._query == Query(text="media")
        assert grid.visible_count == len(items) + 1
        assert grid._tiles[focused_item.path] is focused_tile
        assert grid.get_vadjustment() is adjustment
        assert adjustment.get_value() == 17
        if focus_taken:
            assert root.get_focus() is focused
    finally:
        root.set_child(None)
        root.destroy()


def test_late_thumbnail_from_replaced_item_cannot_overwrite_the_new_tile(
    grid: WallpaperGrid,
) -> None:
    old = item("changing", mtime=1)
    new = item("changing", mtime=2)
    grid.populate((old,))
    grid.populate((new,))
    tile = grid._tiles[new.path]

    grid._on_thumbnail(old, None)
    assert tile._spinner.get_visible()

    grid._on_thumbnail(new, None)
    assert not tile._spinner.get_visible()


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


@pytest.mark.parametrize(
    "document",
    (
        "roots = [\n",
        '# written by a future release\nroots = []\nfuture_renderer = "shiny"\n',
    ),
    ids=("malformed", "future-key"),
)
def test_closing_without_an_edit_preserves_settings_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from wall_in_one import paths
    from wall_in_one.ui.app import Application

    target = paths.settings_path()
    target.parent.mkdir(parents=True)
    target.write_text(document, encoding="utf-8")
    application = Application()
    window = object()
    application._window = window  # type: ignore[assignment]

    try:
        assert application._on_close_request(window) is False  # type: ignore[arg-type]
        assert target.read_text(encoding="utf-8") == document
    finally:
        application.session.shutdown()


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


def test_one_thumbnail_request_delivers_to_every_visible_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pairing shown in both playlist panes shares work, not its callback."""
    from threading import Event

    from wall_in_one.ui.thumbnails import ThumbnailLoader as Loader

    started = Event()
    release = Event()
    texture = object()

    def load(_item: MediaItem, _processes: object | None = None) -> object:
        started.set()
        assert release.wait(2)
        return texture

    monkeypatch.setattr(Loader, "_texture_for", staticmethod(load))
    monkeypatch.setattr("wall_in_one.ui.thumbnails.GLib.idle_add", lambda callback: callback())
    loader = Loader(max_workers=1)
    wallpaper = item("shared")
    delivered: list[object | None] = []
    complete = Event()

    def receive(_item: MediaItem, result: object | None) -> None:
        delivered.append(result)
        if len(delivered) == 2:
            complete.set()

    loader.request(wallpaper, receive)
    assert started.wait(2)
    loader.request(wallpaper, receive)
    release.set()
    assert complete.wait(2)
    loader.shutdown()
    assert delivered == [texture, texture]


def test_thumbnail_shutdown_terminates_its_active_child_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wall_in_one import thumbnails as cache
    from wall_in_one import worker_processes
    from wall_in_one.ui.thumbnails import ThumbnailLoader as Loader

    monkeypatch.setattr("wall_in_one.ui.thumbnails.thumbnails.prune", lambda: 0)
    monkeypatch.setattr("wall_in_one.ui.thumbnails.thumbnails.lookup", lambda _item: None)

    def block(
        _item: MediaItem,
        *,
        processes: worker_processes.Cancellation | None = None,
    ) -> Path:
        assert processes is not None
        try:
            processes.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=60.0,
            )
        except worker_processes.ProcessCancelledError as error:
            raise cache.ThumbnailError("cancelled") from error
        raise AssertionError("blocking thumbnail process returned normally")

    monkeypatch.setattr("wall_in_one.ui.thumbnails.thumbnails.generate", block)
    loader = Loader(max_workers=1)
    loader.request(item("slow"), lambda _item, _texture: None)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with loader._processes._lock:
            if loader._processes._active:
                break
        time.sleep(0.01)
    else:
        pytest.fail("thumbnail child did not start")

    started = time.monotonic()
    loader.shutdown()
    loader._pool.shutdown(wait=True, cancel_futures=True)

    assert time.monotonic() - started < 1.5


# -- the window and the store ---------------------------------------------


def test_changing_roots_requests_a_scan_and_redraw_instead_of_only_a_highlight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Graphical root changes queue a scan whose completed result is redrawn."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    from wall_in_one import config
    from wall_in_one.ui.app import Application

    class StubSession:
        def __init__(self) -> None:
            self.settings = config.Settings()
            self.synced = 0

        def update_settings(
            self, settings: config.Settings, *, rescan_library: bool = True
        ) -> None:
            del rescan_library
            self.settings = settings

        def sync_with_noctalia(self) -> None:
            self.synced += 1

    class StubWindow:
        def __init__(self) -> None:
            self.libraries = 0
            self.highlights = 0
            self.settings: config.Settings | None = None

        def apply_settings(self, settings: config.Settings) -> None:
            self.settings = settings

        def show_library(self, _session: object) -> None:
            self.libraries += 1

        def show_current(self, _session: object) -> None:
            self.highlights += 1

    application = Application()
    application._session.shutdown()
    session = StubSession()
    window = StubWindow()
    application._session = session  # type: ignore[assignment]
    application._window = window  # type: ignore[assignment]
    monkeypatch.setattr(config, "save", lambda _settings: None)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    monkeypatch.setattr(application, "sync_cycle_timer", lambda: None)
    monkeypatch.setattr(application, "_make_missing_stills", lambda: None)

    def finish_scan() -> None:
        session.sync_with_noctalia()
        window.show_library(session)

    monkeypatch.setattr(application, "refresh_library", finish_scan)

    replacement = tmp_path / "new-library"
    application.update_settings(roots=(replacement,))

    assert session.settings.roots == (replacement,)
    assert session.synced == 1
    assert window.settings is session.settings
    assert window.libraries == 1
    assert window.highlights == 0
    application._window = None


def test_failed_settings_save_is_not_adopted_by_application_or_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one import config
    from wall_in_one.ui.app import Application

    application = Application()
    before = application.settings

    def fail(_changes: object, _path: Path | None = None) -> config.Settings:
        raise config.ConfigError("disk full")

    monkeypatch.setattr(config, "update", fail)
    try:
        with pytest.raises(config.ConfigError, match="disk full"):
            application.update_settings(opacity=0.72)

        assert application.settings is before
        assert application.session.settings is before
    finally:
        application._stills.shutdown()
        application.session.shutdown()


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


def test_failed_context_menu_store_writes_do_not_publish_or_claim_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context menu must honour the stores' persist-before-adopt contract."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    from wall_in_one import config
    from wall_in_one.library import favourites, pairings, scan
    from wall_in_one.session import Session
    from wall_in_one.ui.window import MainWindow

    root = tmp_path / "library"
    root.mkdir()
    wallpaper = root / "one.png"
    _png(wallpaper)
    indexed_still = root / "other.png"
    _png(indexed_still)
    indexed_video = root / "motion.mp4"
    indexed_video.write_bytes(b"video")
    outside = tmp_path / "outside.png"
    _png(outside)

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.TransactionalMenuTest")
            self.settings = config.Settings(roots=(root,))
            self.resolved_palette = None
            self.session = Session(self.settings, scanner=lambda _roots: scan.scan((root,)))
            self.session.refresh()
            self.pairing_updates = 0
            self.favourite_updates = 0

        def refresh_library(self) -> None: ...

        def pairing_changed(self, _item: MediaItem) -> None:
            self.pairing_updates += 1

        def favourites_changed(self) -> None:
            self.favourite_updates += 1

        def authoring_action_async(
            self,
            work: object,
            finish: object,
            *,
            prepare: object = None,
            failure: object = None,
            **_keywords: object,
        ) -> bool:
            try:
                worker = prepare() if callable(prepare) else work
                assert callable(worker)
                result = worker()
            except Exception as error:
                if callable(failure):
                    failure(str(error))
                return False
            assert callable(finish)
            finish(result)
            return True

        def current_item_for_authoring(self, media: MediaItem) -> MediaItem:
            current = self.session.library.find(media.path)
            assert current is not None
            return current

        def prepare_pairing_mutation(self, media: MediaItem, mutation: object) -> object:
            current = self.current_item_for_authoring(media)
            assert callable(mutation)
            return lambda: mutation(self.session.pairings, current)

        def prepare_still_pairing_mutation(
            self,
            media: MediaItem,
            still: Path | None,
            mutation: object,
        ) -> object:
            current = self.current_item_for_authoring(media)
            assert callable(mutation)

            def run() -> SimpleNamespace:
                effective = still
                if effective is None:
                    effective = pairings.synthesize(current, self.session.library.roots).still
                record = mutation(self.session.pairings, current, still)
                return SimpleNamespace(
                    item=current,
                    record=record,
                    effective_still=effective,
                )

            return run

        def prepare_pairing_reset(self, media: MediaItem) -> object:
            current = self.current_item_for_authoring(media)

            def run() -> SimpleNamespace:
                effective = pairings.synthesize(current, self.session.library.roots).still
                changed = self.session.pairings.reset(current)
                return SimpleNamespace(
                    item=current,
                    changed=changed,
                    effective_still=effective,
                )

            return run

        def adopt_pairing_still(
            self,
            media: MediaItem,
            effective: Path | None,
        ) -> MediaItem:
            current = self.current_item_for_authoring(media)
            adopted = current.with_still(effective) if current.is_moving else current
            library = self.session.library
            self.session.adopt_library(
                type(library)(
                    roots=library.roots,
                    items=tuple(
                        adopted if candidate.path == current.path else candidate
                        for candidate in library.items
                    ),
                    skipped=library.skipped,
                    still_inventory=library.still_inventory,
                ),
                reconcile_workshop=False,
            )
            return adopted

    application = FakeApp()
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    item = application.session.library.find(wallpaper)
    assert item is not None
    reports: list[str] = []
    monkeypatch.setattr(window, "report", reports.append)

    window._store_still(item, indexed_still)
    saved = application.session.pairings.get(pairings.Identity.of(item))
    assert saved is not None and saved.still == indexed_still
    assert application.pairing_updates == 1

    window._store_still(item, outside)
    window._store_still(item, indexed_video)
    assert application.pairing_updates == 1
    assert "not an indexed library item" in reports[0]
    assert "add its folder in Settings" in reports[0]
    assert "indexed as video, not as a still image" in reports[1]
    reports.clear()

    def favourite_failure(_path: Path) -> None:
        raise favourites.FavouritesError("local-io", "disk full")

    monkeypatch.setattr(application.session.favourites, "add", favourite_failure)
    window._on_favourite(item, True)
    assert application.favourite_updates == 0
    assert item.path not in application.session.favourites.paths

    def pairing_failure(*_args: object) -> None:
        raise pairings.PairingError("local-io", "disk full")

    failure = pairing_failure
    monkeypatch.setattr(application.session.pairings, "choose_still", failure)
    window._store_still(item, indexed_still)
    monkeypatch.setattr(application.session.pairings, "reset", failure)
    window._on_reset_pairing(None, GLib.Variant.new_string(str(item.path)))  # type: ignore[arg-type]
    monkeypatch.setattr(application.session.pairings, "choose_palette", failure)
    window._on_palette_path(
        None,  # type: ignore[arg-type]
        GLib.Variant("(ss)", (str(item.path), pairings.PalettePolicy().encode())),
    )

    assert application.pairing_updates == 1
    assert len(reports) == 4
    assert all("nothing changed" in message for message in reports)
    window.destroy()
    application.session.shutdown()


@pytest.fixture
def apply_window(tmp_path: Path) -> Iterator[tuple[MainWindow, list[Path], list[tuple[Path, str]]]]:
    """Real window and stores; applying records dispatches without a runtime."""
    from wall_in_one import config
    from wall_in_one.library import scan
    from wall_in_one.session import Session

    root = tmp_path / "library"
    root.mkdir()
    _png(root / "one.png")
    played: list[Path] = []
    targeted: list[tuple[Path, str]] = []

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.TileApplyTargetsTest")
            self.settings = config.Settings(
                roots=(root,), display_mode=config.DISPLAY_MODE_INDEPENDENT
            )
            self.resolved_palette = None
            self.session = Session(self.settings, scanner=lambda _roots: scan.scan((root,)))
            self.session.refresh()

        def refresh_library(self) -> None: ...

        def play_item_async(self, chosen: MediaItem) -> bool:
            played.append(chosen.path)
            return True

        def play_item_on_async(self, chosen: MediaItem, connector: str) -> bool:
            targeted.append((chosen.path, connector))
            return True

    application = FakeApp()
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    window.show_library(application.session)
    window._runtime_media_status = {
        "status_version": 2,
        "displays": [
            {"connector": "DP-1", "connected": True},
            {"connector": "DP-2", "connected": True},
            {"connector": "DP-offline", "connected": False},
        ],
    }
    try:
        yield window, played, targeted
    finally:
        window.destroy()
        application.session.shutdown()


def _menu_labels(menu: Gio.MenuModel) -> list[str]:
    return [
        value.get_string()
        for index in range(menu.get_n_items())
        if (value := menu.get_item_attribute_value(index, Gio.MENU_ATTRIBUTE_LABEL, None))
        is not None
    ]


def test_independent_apply_requires_explicit_target_and_dispatches_each_action_once(
    apply_window: tuple[MainWindow, list[Path], list[tuple[Path, str]]],
) -> None:
    window, played, targeted = apply_window
    media = window._app.session.library.items[0]
    tile = window._grid._tiles[media.path]
    assert tile._apply_targets.get_visible()
    assert not tile._apply.get_visible()
    tile._apply.emit("clicked")
    assert played == [] and targeted == []
    menu = window._apply_menu_for(media)
    assert _menu_labels(menu) == [
        "Apply to DP-1",
        "Apply to DP-2",
        "Apply to all displays (Quick choice)",
    ]
    assert played == [] and targeted == []
    for index in (0, 2):
        action_name = menu.get_item_attribute_value(index, Gio.MENU_ATTRIBUTE_ACTION, None)
        assert action_name is not None
        action = window.lookup_action(action_name.get_string().removeprefix("win."))
        assert action is not None
        action.activate(menu.get_item_attribute_value(index, Gio.MENU_ATTRIBUTE_TARGET, None))
    assert targeted == [(media.path, "DP-1")]
    assert played == [media.path]
    assert window._content_stack.get_visible_child_name() == "primary"


def test_open_target_menu_refreshes_connector_snapshot_without_applying(
    apply_window: tuple[MainWindow, list[Path], list[tuple[Path, str]]],
) -> None:
    window, played, targeted = apply_window
    tile = next(iter(window._grid._tiles.values()))
    window.present()
    _settle_widgets()
    tile._apply_targets.popup()
    menu = tile._apply_targets.get_menu_model()
    assert menu is not None and "Apply to DP-1" in _menu_labels(menu)
    tile._apply_targets.popdown()
    window._runtime_media_status = {
        "status_version": 2,
        "displays": [{"connector": "HDMI-A-3", "connected": True}],
    }
    tile._apply_targets.popup()
    menu = tile._apply_targets.get_menu_model()
    assert menu is not None
    assert _menu_labels(menu) == ["Apply to HDMI-A-3", "Apply to all displays (Quick choice)"]
    tile._apply_targets.popdown()
    assert played == [] and targeted == []


def test_unknown_or_disconnected_target_does_not_fall_back_to_all_displays(
    apply_window: tuple[MainWindow, list[Path], list[tuple[Path, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, played, targeted = apply_window
    media = window._app.session.library.items[0]
    window._runtime_media_status = None
    menu = window._apply_menu_for(media)
    assert _menu_labels(menu) == [
        "No connected displays reported",
        "Apply to all displays (Quick choice)",
    ]
    reports: list[str] = []
    monkeypatch.setattr(window, "report", reports.append)
    action = window.lookup_action("apply-wallpaper-on")
    assert action is not None
    action.activate(GLib.Variant("(ss)", (str(media.path), "DP-1")))
    assert played == [] and targeted == []
    assert reports and "no longer available" in reports[-1]


def test_stale_target_action_cannot_apply_a_newly_blocked_item(
    apply_window: tuple[MainWindow, list[Path], list[tuple[Path, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, played, targeted = apply_window
    media = window._app.session.library.items[0]
    window._app.session.pairings.mark_borked(media, "renderer crashed", "automatic-apply")
    reports: list[str] = []
    monkeypatch.setattr(window, "report", reports.append)
    for name, target in (
        ("apply-wallpaper", GLib.Variant.new_string(str(media.path))),
        ("apply-wallpaper-on", GLib.Variant("(ss)", (str(media.path), "DP-1"))),
    ):
        action = window.lookup_action(name)
        assert action is not None
        action.activate(target)
    assert played == [] and targeted == []
    assert len(reports) == 2 and all("Playback unavailable" in report for report in reports)
    assert _menu_labels(window._apply_menu_for(media)) == ["Playback unavailable"]


def test_apply_control_tracks_display_mode_without_rebuilding_the_tile(
    apply_window: tuple[MainWindow, list[Path], list[tuple[Path, str]]],
) -> None:
    from dataclasses import replace

    from wall_in_one import config

    window, played, targeted = apply_window
    media = window._app.session.library.items[0]
    tile = window._grid._tiles[media.path]
    window.apply_settings(replace(window.settings, display_mode=config.DISPLAY_MODE_MIRRORED))
    assert window._grid._tiles[media.path] is tile
    assert tile._apply.get_visible() and not tile._apply_targets.get_visible()
    tile._apply.emit("clicked")
    assert played == [media.path] and targeted == []


def test_quick_choice_menu_action_really_plays_instead_of_opening_the_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the registered Gio action, not a matching string in source."""
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
            super().__init__(application_id="dev.goober.QuickChoiceActionTest")
            self.settings = config.Settings(roots=(root,))
            self.resolved_palette = None
            self.session = Session(self.settings, scanner=lambda _roots: scan.scan((root,)))
            self.session.refresh()
            self.played: list[Path] = []

        def refresh_library(self) -> None: ...

        def play_item_async(self, chosen: MediaItem) -> bool:
            self.played.append(chosen.path)
            return True

    application = FakeApp()
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    window.show_library(application.session)
    action = window.lookup_action("apply-wallpaper")
    assert action is not None

    action.activate(GLib.Variant.new_string(str(wallpaper)))

    assert application.played == [wallpaper]
    assert window._content_stack.get_visible_child_name() == "primary"
    window.destroy()
    application.session.shutdown()


def test_borked_media_has_a_warning_and_no_global_or_targeted_play_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            super().__init__(application_id="dev.goober.BorkedMediaActionsTest")
            self.settings = config.Settings(
                roots=(root,), display_mode=config.DISPLAY_MODE_INDEPENDENT
            )
            self.resolved_palette = None
            self.session = Session(self.settings, scanner=lambda _roots: scan.scan((root,)))
            self.session.refresh()
            self.played: list[Path] = []

        def refresh_library(self) -> None: ...

        def play_item_async(self, chosen: MediaItem) -> bool:
            self.played.append(chosen.path)
            return True

    application = FakeApp()
    item = application.session.library.items[0]
    application.session.pairings.mark_borked(item, "renderer crashed", "automatic-apply")
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    reports: list[str] = []
    monkeypatch.setattr(window, "report", reports.append)
    window.show_library(application.session)
    window._runtime_media_status = {
        "status_version": 2,
        "displays": [
            {"connector": "DP-1", "connected": True},
            {"connector": "DP-2", "connected": True},
        ],
    }

    tile = window._grid._tiles[wallpaper]
    assert tile.has_css_class("wio-tile-borked")
    assert "Playback disabled" in (tile._frame.get_tooltip_text() or "")
    menu = window._menu_for(item)
    labels = [
        value.get_string()
        for index in range(menu.get_n_items())
        if (
            value := menu.get_item_attribute_value(
                index, Gio.MENU_ATTRIBUTE_LABEL, GLib.VariantType.new("s")
            )
        )
        is not None
    ]
    assert "Playback unavailable" in labels
    assert not any(label == "Play" or label.startswith("Play ") for label in labels)
    warning_index = labels.index("Playback unavailable")
    assert menu.get_item_attribute_value(warning_index, Gio.MENU_ATTRIBUTE_ACTION, None) is None

    window._quick_apply(item)

    assert application.played == []
    assert reports and "Playback unavailable" in reports[-1]
    window.destroy()
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
    assert isinstance(window._library_controls, Adw.WrapBox)

    for page in ("browse", "media", "playlists", "schedules", "settings"):
        assert window._stack.get_child_by_name(page) is not None
        window.show_page(page)
        assert window._toast.get_child() is window._content_stack
        assert (
            window.get_title()
            == {
                "browse": "Wall-in-One - Store",
                "media": "Wall-in-One - Library",
                "playlists": "Wall-in-One - Playlists",
                "schedules": "Wall-in-One - Schedules",
                "settings": "Wall-in-One - Settings",
            }[page]
        )
    assert window._stack.get_child_by_name("pairings") is None
    assert window._content_stack.get_child_by_name("pairing-editor") is window._pairings_page
    window._content_stack.set_visible_child_name("pairing-editor")
    assert window._toast.get_child() is window._content_stack

    window.destroy()
    application.session.shutdown()


def test_runtime_popover_drives_live_state_instead_of_editing_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    from wall_in_one import config
    from wall_in_one.library.model import Library
    from wall_in_one.session import Session
    from wall_in_one.theme import source
    from wall_in_one.ui.window import MainWindow

    calls: list[tuple[str, str | None]] = []

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.RuntimeControlsTest")
            self.settings = config.Settings(cycle_enabled=False, shuffle=False)
            self.resolved_palette = source.resolve()
            self.session = Session(
                self.settings,
                scanner=lambda _roots: Library(roots=(), items=()),
            )
            self.session.refresh()

        def refresh_library(self) -> None: ...

        def runtime_action_async(self, verb: str, argument: str | None = None) -> bool:
            calls.append((verb, argument))
            return True

    application = FakeApp()
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    window.show_runtime_protocol_error("Runtime returned status in an unexpected format")
    assert "invalid status reply" in window._runtime_control_status.get_text()
    assert "runtime status invalid" in window._subtitle.get_subtitle()
    assert not window._runtime_controls.get_sensitive()
    window.show_runtime_status(
        {
            "playlist_id": "evening",
            "playlist": "Evening",
            "source": "manual",
            "entry_id": "evening-second",
            "playback_state": "playing",
            "paused": False,
            "cycle_enabled": True,
            "shuffle": False,
            "last_error": "",
        }
    )

    assert window._runtime_cycle.get_active()
    assert not window._runtime_shuffle.get_active()
    assert "invalid status reply" not in window._runtime_control_status.get_text()
    assert calls == [], "status refreshes must not echo commands back to the service"
    window.show_runtime_delayed()
    assert window._runtime_controls.get_sensitive()
    assert "status delayed" in window._runtime_control_status.get_text()
    assert "status delayed" in window._subtitle.get_subtitle()
    window.show_runtime_status(
        {
            "playlist": "Evening",
            "source": "manual",
            "playback_state": "playing",
            "paused": False,
            "cycle_enabled": True,
            "shuffle": False,
            "last_error": "",
        }
    )
    assert "status delayed" not in window._runtime_control_status.get_text()
    window.show_runtime_status(
        {
            "playlist": "Evening",
            "source": "manual",
            "playback_state": "playing",
            "cycle_enabled": True,
            "shuffle": False,
            "last_error": "",
            "output_discovery_error": "niri IPC is not ready",
        }
    )
    assert "display discovery degraded" in window._subtitle.get_subtitle()
    window._runtime_cycle.set_active(False)
    window._runtime_stop.emit("clicked")
    assert calls == [("cycle", "off"), ("stop", None)]

    window.show_runtime_status(
        {
            "playlist": "Evening",
            "source": "manual",
            "playback_state": "stopped",
            "paused": False,
            "cycle_enabled": False,
            "shuffle": False,
            "last_error": "",
        }
    )
    window._runtime_play.emit("clicked")
    assert calls[-1] == ("play", None)
    window.show_runtime_status(
        {
            "playlist": "Multiple displays",
            "source": "mixed",
            "playback_state": "mixed",
            "paused": False,
            "cycle_enabled": False,
            "cycle_source": "mixed",
            "shuffle": False,
            "shuffle_source": "mixed",
            "last_error": "",
        }
    )
    assert "pause all" in (window._runtime_play.get_tooltip_text() or "")
    assert "cycle mixed" in window._runtime_control_status.get_text()
    assert "shuffle mixed" in window._runtime_control_status.get_text()
    assert "sets Cycle on everywhere" in (window._runtime_cycle.get_tooltip_text() or "")
    assert "sets Shuffle on everywhere" in (window._runtime_shuffle.get_tooltip_text() or "")
    window._runtime_cycle.set_active(True)
    assert len(calls) == 4
    assert calls[3] == ("cycle", "on")
    window._runtime_play.emit("clicked")
    assert len(calls) == 5
    assert calls[4] == ("toggle", None)
    long_error = "renderer failed " + ("x" * 12_000) + " attributable tail"
    window.show_runtime_status(
        {
            "playlist": "Evening",
            "source": "manual",
            "playback_state": "playing",
            "paused": False,
            "cycle_enabled": True,
            "shuffle": False,
            "last_error": long_error,
        }
    )
    subtitle = window._subtitle.get_subtitle()
    assert len(subtitle) < 300
    assert "…" in subtitle
    assert subtitle.endswith("attributable tail")
    assert window._subtitle.get_tooltip_text() == long_error
    window.show_runtime_status(
        {
            "playlist": "Evening",
            "source": "manual",
            # The still remains the active entry, so Rust does not call this a
            # user-requested Stop even though its renderer has failed.
            "playback_state": "playing",
            "paused": False,
            "cycle_enabled": True,
            "shuffle": False,
            "renderer_failed": True,
            "last_error": "mpvpaper exited for evening-second",
        }
    )
    assert "Renderer stopped · Retry available" in window._runtime_control_status.get_text()
    assert "Retry motion" in (window._runtime_play.get_tooltip_text() or "")
    window._runtime_play.emit("clicked")
    assert calls[-1] == ("play", None)
    before_taboo = len(calls)
    window.show_page("settings")
    window.show_runtime_status(
        {
            "playlist_id": "evening",
            "playlist": "Evening",
            "source": "manual",
            "entry_id": "evening-second",
            "playback_state": "playing",
            "paused": False,
            "cycle_enabled": True,
            "shuffle": False,
            # This occurrence did not own the renderer which discovered the
            # equivalent media crash, and its record has fallen outside the
            # bounded diagnostic inventory. The explicit safety bit alone
            # must still remove Play from the global control.
            "renderer_failed": False,
            "entry_taboo": True,
            "last_error": "mpvpaper repeatedly failed for evening-second",
            "taboo_entries": [],
            "taboo_entries_omitted": 1,
        }
    )
    assert "Playback unavailable" in window._runtime_control_status.get_text()
    assert "Library" in (window._runtime_play.get_tooltip_text() or "")
    assert window._runtime_play.get_icon_name() == "dialog-warning-symbolic"

    window._runtime_play.emit("clicked")

    assert len(calls) == before_taboo, "taboo entries cannot be retried by Play"
    assert window._stack.get_visible_child_name() == "media"
    for playback in ("playing", "paused", "stopped"):
        window.show_runtime_status(
            {
                "playlist": "Evening",
                "source": "manual",
                "playback_state": playback,
                "cycle_enabled": True,
                "shuffle": False,
                "last_error": "",
                "power_source": "battery",
                "power_available": True,
                "stop_animations_on_battery": True,
                "animations_inhibited": True,
                "animation_inhibition_reason": "battery",
            }
        )
        assert window._playback_state == playback
        assert "Animations stopped on battery" in window._runtime_control_status.get_text()
        assert "Animations stopped on battery" in window._subtitle.get_subtitle()
        assert "stopped on battery" in (window._runtime_play.get_tooltip_text() or "")
        assert window._runtime_controls.get_sensitive()
        assert len(calls) == before_taboo, "power status must not send transport commands"
    window.show_runtime_unavailable()
    assert not window._runtime_controls.get_sensitive()

    window.destroy()
    application.session.shutdown()


def test_media_highlights_and_playing_label_follow_atomic_runtime_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one import config
    from wall_in_one.library import playlists
    from wall_in_one.library.model import Library
    from wall_in_one.session import Session
    from wall_in_one.theme import source
    from wall_in_one.ui.window import MainWindow

    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    _png(first_path)
    _png(second_path)
    first = MediaItem(first_path, Kind.STILL, size=1, mtime=1)
    second = MediaItem(second_path, Kind.STILL, size=1, mtime=1)
    authored = playlists.Store(
        {
            "day": playlists.Playlist(
                id="day",
                name="Day",
                entries=(playlists.Entry("day-first", str(first_path)),),
            ),
            "evening": playlists.Playlist(
                id="evening",
                name="Evening",
                entries=(playlists.Entry("evening-second", str(second_path)),),
            ),
        },
        tmp_path / "playlists.json",
    )

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.RuntimeMediaTruthTest")
            self.settings = config.Settings(roots=(tmp_path,))
            self.resolved_palette = source.resolve()
            self.session = Session(
                self.settings,
                scanner=lambda _roots: Library((tmp_path,), (first, second)),
                playlist_store=authored,
            )
            self.session.refresh()

        def refresh_library(self) -> None: ...

        def runtime_action_async(self, _verb: str, _argument: str | None = None) -> bool:
            return True

    application = FakeApp()
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    window.show_library(application.session)
    local_cursor = application.session.cursor
    assert local_cursor is not None and local_cursor.path == first_path

    window.show_runtime_status(
        {
            "playlist_id": "evening",
            "playlist": "Evening",
            "source": "manual",
            "entry_id": "evening-second",
            "still": str(second_path),
            "playback_state": "playing",
            "cycle_enabled": True,
            "shuffle": False,
            "last_error": "",
            "displays": [
                {
                    "connector": "DP-1",
                    "playlist_id": "evening",
                    "entry_id": "evening-second",
                    "still": str(second_path),
                }
            ],
        }
    )

    assert not window._grid._tiles[first_path].has_css_class("wio-tile-current")
    assert window._grid._tiles[second_path].has_css_class("wio-tile-current")
    assert "playing Evening" in window._subtitle.get_subtitle()
    assert application.session.cursor is local_cursor

    window.show_runtime_status(
        {
            "playlist_id": "",
            "playlist": "Multiple displays",
            "source": "schedule",
            "playback_state": "playing",
            "cycle_enabled": True,
            "shuffle": False,
            "last_error": "",
            "playlists": [
                {"id": "day", "active": True},
                {"id": "evening", "active": True},
            ],
            "displays": [
                {
                    "connector": "DP-1",
                    "playlist_id": "day",
                    "entry_id": "day-first",
                    "still": str(first_path),
                },
                {
                    "connector": "DP-2",
                    "playlist_id": "evening",
                    "entry_id": "evening-second",
                    "still": str(second_path),
                },
            ],
        }
    )

    assert window._grid._tiles[first_path].has_css_class("wio-tile-current")
    assert window._grid._tiles[second_path].has_css_class("wio-tile-current")
    assert "playing Multiple displays" in window._subtitle.get_subtitle()
    assert application.session.cursor is local_cursor

    window.show_runtime_status(
        {
            "playlist_id": "",
            "playlist": "Multiple displays",
            "source": "schedule",
            "playback_state": "playing",
            "cycle_enabled": True,
            "shuffle": False,
            "last_error": "",
            "playlists": [
                {"id": "day", "active": True},
                {"id": "evening", "active": True},
            ],
            "displays": [
                {
                    "connector": "DP-1",
                    "connected": True,
                    "playlist_id": "day",
                    "playlist": "Day",
                    "entry_id": "day-first",
                    "still": str(first_path),
                },
                {
                    "connector": "DP-2",
                    "connected": False,
                    "playlist_id": "evening",
                    "playlist": "Evening",
                    "entry_id": "evening-second",
                    "still": str(second_path),
                },
            ],
        }
    )

    assert window._grid._tiles[first_path].has_css_class("wio-tile-current")
    assert not window._grid._tiles[second_path].has_css_class("wio-tile-current")
    assert "playing Day" in window._subtitle.get_subtitle()
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
