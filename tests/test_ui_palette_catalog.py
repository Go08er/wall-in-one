"""Display-backed palette catalogue responsiveness and bounded widgets."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

import wall_in_one.ui.pairings_page as pairings_page  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.library import pairings  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.theme import noctalia, palettes  # noqa: E402
from wall_in_one.theme.palette import Palette, PalettePair  # noqa: E402
from wall_in_one.ui.palette_browser import (  # noqa: E402
    BROWSE_PAGE_SIZE,
    STRIP_TOKENS,
    PaletteBrowserDialog,
)
from wall_in_one.ui.palette_catalog import CatalogState, PaletteCatalog  # noqa: E402
from wall_in_one.ui.window import MAX_PALETTES_PER_ORIGIN, MainWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class QuietLoader:
    def __init__(self, **_arguments: object) -> None: ...

    def request(self, *_arguments: object) -> None: ...

    def shutdown(self) -> None: ...


def _catalogue(count: int = 512) -> palettes.Discovery:
    dark = {token: f"#{index + 1:02x}2233" for index, (token, _label) in enumerate(STRIP_TOKENS)}
    light = {token: f"#{index + 1:02x}ddee" for index, (token, _label) in enumerate(STRIP_TOKENS)}
    pair = PalettePair(
        dark=Palette.from_mapping("dark", dark),
        light=Palette.from_mapping("light", light),
    )
    return palettes.Discovery(
        tuple(
            palettes.PaletteEntry(
                name=f"Palette {index:04d}",
                origin=palettes.Origin.COMMUNITY,
                path=Path(f"/palettes/Palette-{index:04d}.json"),
                colours=pair,
            )
            for index in range(count)
        )
    )


def _drain() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def _first_scroller(widget: Gtk.Widget) -> Gtk.ScrolledWindow | None:
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.ScrolledWindow):
            return child
        nested = _first_scroller(child)
        if nested is not None:
            return nested
        child = child.get_next_sibling()
    return None


def test_blocked_discovery_does_not_block_a_gtk_heartbeat() -> None:
    started = threading.Event()
    release = threading.Event()

    def discover(_cancelled: palettes.CancelCheck) -> palettes.Discovery:
        started.set()
        assert release.wait(2)
        return _catalogue()

    catalog = PaletteCatalog(discover=discover)
    heartbeat = threading.Event()
    catalog.refresh()
    assert started.wait(2)

    def beat() -> bool:
        heartbeat.set()
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(10, beat)

    deadline = time.monotonic() + 1
    context = GLib.MainContext.default()
    while not heartbeat.is_set() and time.monotonic() < deadline:
        context.iteration(True)

    assert heartbeat.is_set()
    assert catalog.state.phase == "loading"
    release.set()
    deadline = time.monotonic() + 2
    while not catalog.state.discovery.entries and time.monotonic() < deadline:
        context.iteration(True)
    current = catalog.state
    assert current.phase == "ready"
    catalog.shutdown()


def test_browser_materialises_one_page_and_preserves_search_focus_scroll_and_rows() -> None:
    discovery = _catalogue()
    catalog = PaletteCatalog(initial=discovery)
    application = SimpleNamespace(
        session=SimpleNamespace(library=SimpleNamespace(stills=()), cursor=None)
    )
    started = time.perf_counter()
    dialog = PaletteBrowserDialog(cast(Any, application), palette_catalog=catalog)
    elapsed = time.perf_counter() - started

    assert len(dialog._entry_rows) == BROWSE_PAGE_SIZE
    assert elapsed < 1.0, f"bounded palette page took {elapsed:.3f}s"
    first_key = next(iter(dialog._entry_rows))
    first_row = dialog._entry_rows[first_key]
    search = dialog._palette_search
    browse = dialog._browse

    root = Gtk.Window()
    root.present()
    dialog.present(root)
    _drain()
    focused = search.grab_focus()
    focus = root.get_focus()
    scroller = _first_scroller(browse)
    adjustment = scroller.get_vadjustment() if scroller is not None else None
    if adjustment is not None:
        adjustment.configure(13, 0, 1000, 1, 10, 10)

    # A same-content newer generation must update status without tearing down
    # an unchanged row tree or the user's interaction widgets.
    dialog._catalog_changed(CatalogState(8, "ready", discovery))

    assert dialog._browse is browse
    assert dialog._palette_search is search
    assert dialog._entry_rows[first_key] is first_row
    if focused:
        assert root.get_focus() is focus
    if adjustment is not None:
        assert scroller is not None and scroller.get_vadjustment() is adjustment
        assert adjustment.get_value() == 13

    dialog._show_more_palettes(dialog._more_button)
    assert len(dialog._entry_rows) == BROWSE_PAGE_SIZE * 2
    assert dialog._entry_rows[first_key] is first_row

    search.set_text("Palette 0511")
    search.emit("search-changed")
    assert len(dialog._entry_rows) == 1
    assert next(iter(dialog._entry_rows))[1] == "Palette 0511"

    dialog.close()
    _drain()
    root.destroy()
    catalog.shutdown()


def test_pairing_policy_searches_all_512_but_builds_only_one_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietLoader)
    wallpaper = tmp_path / "wall.png"
    wallpaper.write_bytes(b"image")
    item = MediaItem(path=wallpaper, kind=Kind.STILL, size=1, mtime=1)
    library = Library(roots=(tmp_path,), items=(item,), still_inventory=(item,))
    session = Session(
        config.Settings(roots=(tmp_path,)),
        scanner=lambda _roots: library,
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()

    class App:
        settings = session.settings
        resolved_palette = None

        def __init__(self) -> None:
            self.session = session

        def pairing_changed(self, _item: MediaItem) -> None: ...

        def window_report(self, _message: str) -> None: ...

    discovery = _catalogue()
    catalog = PaletteCatalog(initial=discovery)
    page = pairings_page.PairingsPage(cast(Any, App()), lambda: None, palette_catalog=catalog)
    page.edit(session, item)

    base = len(noctalia.ALL_SCHEMES) + 1
    assert len(page._palette_buttons) == base + pairings_page.PALETTE_PAGE_SIZE
    assert len(page._palette_buttons) < len(discovery.entries)
    search = page._palette_search
    first_button = page._palette_buttons["community:Palette 0000"]
    scroller = page._editor_scroll
    adjustment = scroller.get_vadjustment()
    adjustment.configure(17, 0, 1000, 1, 10, 10)

    page._catalog_changed(CatalogState(9, "ready", discovery))
    assert page._palette_buttons["community:Palette 0000"] is first_button

    page._show_more_palettes(page._palette_more)
    assert len(page._palette_buttons) == base + pairings_page.PALETTE_PAGE_SIZE * 2
    assert page._palette_buttons["community:Palette 0000"] is first_button

    search.set_text("Palette 0511")
    search.emit("search-changed")

    assert page._palette_search is search
    assert "community:Palette 0511" in page._palette_buttons
    assert len(page._palette_buttons) == base + 1
    assert scroller.get_vadjustment() is adjustment
    assert adjustment.get_value() == 17

    page.shutdown()
    catalog.shutdown()
    session.shutdown()


def test_context_menu_uses_the_snapshot_and_keeps_each_origin_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _catalogue()
    catalog = PaletteCatalog(initial=discovery)
    raw_window = SimpleNamespace(_palette_catalog=catalog)
    item = MediaItem(Path("/library/wall.png"), Kind.STILL, 1, 1)
    monkeypatch.setattr(
        palettes,
        "discover",
        lambda **_arguments: pytest.fail("a context menu must not touch the filesystem"),
    )

    menu = MainWindow._palette_menu(cast(MainWindow, raw_window), item)
    community: Gio.MenuModel | None = None
    for index in range(menu.get_n_items()):
        label = menu.get_item_attribute_value(
            index,
            Gio.MENU_ATTRIBUTE_LABEL,
            GLib.VariantType.new("s"),
        )
        if label is not None and label.get_string() == palettes.Origin.COMMUNITY.label:
            community = menu.get_item_link(index, Gio.MENU_LINK_SUBMENU)
            break

    assert community is not None
    assert community.get_n_items() == MAX_PALETTES_PER_ORIGIN
    catalog.shutdown()
