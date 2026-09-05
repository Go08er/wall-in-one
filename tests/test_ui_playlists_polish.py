"""Compact playlist authoring stays clear, keyboard-friendly, and durable."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from wall_in_one import config  # noqa: E402
from wall_in_one.library import displays, playlists, schedules  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui import playlists_page  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    Gtk.init()
    Adw.init()


class QuietLoader:
    def request(self, _item: MediaItem, _callback: Any) -> None: ...

    def shutdown(self) -> None: ...


class PlaylistApp:
    """Actor-shaped fake: save first, then notify the same permanent page."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.page: playlists_page.PlaylistsPage | None = None
        self.published = 0
        self.reports: list[str] = []
        self.defer = False
        self.pending: list[Any] = []

    def playlists_changed(self) -> None:
        self.published += 1
        self.session.playlists_changed()
        assert self.page is not None
        self.page.refresh(self.session)

    def window_report(self, message: str) -> None:
        self.reports.append(message)

    def current_item_for_authoring(self, item: MediaItem) -> MediaItem:
        current = self.session.library.find(item.path)
        if current is None:
            raise ValueError("pairing no longer exists")
        return current

    def authoring_action_async(
        self, work: Any, finish: Any, *, prepare: Any = None, failure: Any = None
    ) -> bool:
        def complete() -> bool:
            try:
                selected = prepare() if prepare is not None else work
                finish(selected())
            except Exception as error:
                if failure is not None:
                    failure(str(error))
                return False
            return True

        if self.defer:
            self.pending.append(complete)
            return True
        return complete()


@pytest.fixture
def make_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
    editors: list[tuple[PlaylistApp, playlists_page.PlaylistsPage]] = []

    def make(
        count: int = 3, entries: int = 3, *, items: tuple[MediaItem, ...] | None = None
    ) -> tuple[PlaylistApp, playlists_page.PlaylistsPage]:
        if items is None:
            items = tuple(
                MediaItem(
                    path=Path("/test-media") / f"pairing-{index:03d}.png",
                    kind=Kind.STILL,
                    size=1,
                    mtime=1,
                )
                for index in range(count)
            )
        store = playlists.Store(path=tmp_path / "playlists.json")
        chosen = store.create("Evening", entry_id="evening")
        for index, item in enumerate(items[:entries]):
            store.add(chosen.id, item.path, entry_id=f"entry-{index}")
        session = Session(
            config.Settings(active_playlist=chosen.id),
            scanner=lambda _roots: Library(roots=(Path("/test-media"),), items=items),
            playlist_store=store,
            schedule_store=schedules.Store(path=tmp_path / "schedules.json"),
            display_store=displays.Store(path=tmp_path / "displays.json"),
        )
        session.refresh()
        app = PlaylistApp(session)
        page = playlists_page.PlaylistsPage(app)  # type: ignore[arg-type]
        app.page = page
        page.refresh(session)
        editors.append((app, page))
        return app, page

    yield make
    for app, page in editors:
        root = page.get_root()
        if isinstance(root, Gtk.Window):
            root.set_child(None)
            root.destroy()
        page.shutdown()
        app.session.shutdown()


def _settle(predicate: Any) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return bool(predicate())


def _present(page: playlists_page.PlaylistsPage, width: int = 800) -> Gtk.Window:
    window = Gtk.Window(default_width=width, default_height=600)
    window.set_child(page)
    window.present()
    assert _settle(
        lambda: (
            page._arranger.get_width() > 0
            and (not page._navigation.get_collapsed() or page._navigation.get_show_content())
        )
    )
    return window


def _has_focus_within(widget: Gtk.Widget) -> bool:
    root = widget.get_root()
    focused = root.get_focus() if isinstance(root, Gtk.Window) else None
    while focused is not None:
        if focused is widget:
            return True
        focused = focused.get_parent()
    return False


def test_library_pairings_are_compact_rows_with_reachable_add_controls(make_editor: Any) -> None:
    _app, page = make_editor()
    _present(page)
    cards = tuple(page._source_cards)
    assert _settle(lambda: all(card.get_height() > 0 for card in cards))

    assert page._source_flow.get_max_children_per_line() == 1
    for card in cards:
        assert card.get_orientation() == Gtk.Orientation.HORIZONTAL
        assert card.picture.get_size_request() == (64, 44)
        assert card.get_height() <= 72, "source rows must not regrow into oversized tiles"
        assert card._add.get_width() >= 24
        assert card._add.get_allocation().x + card._add.get_width() <= card.get_width()
        assert str(card.item.path) in (card.get_tooltip_text() or "")
    assert page._order_scroll.get_width() > page._source_scroll.get_width()
    assert page._source_count.get_label() == "3 library pairings"
    assert page._entry_count.get_label() == "3 pairings · plays in this order"


@pytest.mark.parametrize("window_width", [800, 1280])
@pytest.mark.parametrize(("texture_width", "texture_height"), [(1920, 1080), (800, 1200)])
def test_loaded_large_thumbnails_keep_compact_geometry_and_reachable_controls(
    make_editor: Any, window_width: int, texture_width: int, texture_height: int
) -> None:
    _app, page = make_editor(count=6, entries=6)
    window = _present(page, window_width)
    cards = tuple(page._source_cards)
    rows = tuple(page._entry_rows.values())
    assert _settle(lambda: all(row.get_height() > 0 for row in rows))
    clock = window.get_frame_clock()
    assert clock is not None
    frame = clock.get_frame_counter()
    texture = Gdk.MemoryTexture.new(
        texture_width,
        texture_height,
        Gdk.MemoryFormat.R8G8B8A8,
        GLib.Bytes.new(bytes((64, 120, 180, 255)) * texture_width * texture_height),
        texture_width * 4,
    )
    for card in cards:
        card.show_thumbnail(card.item, texture)
    for row in rows:
        assert row.item is not None
        row.show_thumbnail(row.item, texture)
    # Paintables change natural requests asynchronously. Check allocations
    # after GTK has processed the frame, not the empty-picture layout above.
    assert _settle(lambda: clock.get_frame_counter() > frame)

    for card in cards:
        assert (card.picture.get_width(), card.picture.get_height()) == (64, 44)
        assert card.get_height() <= 72
        assert card._add.get_width() >= 24
        assert card._add.get_allocation().x + card._add.get_width() <= card.get_width()
    for row in rows:
        assert (row.picture.get_width(), row.picture.get_height()) == (96, 54)
        assert row.get_height() <= 96, "loaded previews must not expand ordered pairing rows"
        assert row.handle.get_size_request() == (46, 72)
        remove = row.surface.get_last_child()
        assert isinstance(remove, Gtk.Button)
        assert remove.get_width() >= 24
        assert remove.get_allocation().x + remove.get_width() <= row.surface.get_width()
    assert page._order_scroll.get_width() > page._source_scroll.get_width()
    icons: list[tuple[Gdk.Paintable, int, int]] = []
    source = SimpleNamespace(set_icon=lambda icon, x, y: icons.append((icon, x, y)))
    cards[0]._drag_begin(source, None)
    try:
        icon, hotspot_x, hotspot_y = icons[0]
        assert isinstance(icon, Gtk.WidgetPaintable)
        assert (icon.get_intrinsic_width(), icon.get_intrinsic_height()) == (64, 44)
        assert (hotspot_x, hotspot_y) == (32, 22)
    finally:
        cards[0]._finish_drag()


@pytest.mark.parametrize("thumbnail_state", ["loading", "absent", "failed"])
def test_unavailable_thumbnail_drag_has_a_visible_bounded_type_placeholder(
    make_editor: Any, thumbnail_state: str
) -> None:
    _app, page = make_editor()
    window = _present(page)
    card = next(iter(page._source_cards))
    clock = window.get_frame_clock()
    assert clock is not None
    frame = clock.get_frame_counter()
    if thumbnail_state == "failed":
        texture = Gdk.MemoryTexture.new(
            1, 1, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(bytes((64, 120, 180, 255))), 4
        )
        card.show_thumbnail(card.item, texture)
        assert not card._preview_placeholder.get_visible()
    if thumbnail_state != "loading":
        card.show_thumbnail(card.item, None)
    assert _settle(lambda: clock.get_frame_counter() > frame)
    assert card._preview_placeholder.get_visible()
    assert card._preview_placeholder.get_label() == "Still"

    icons: list[tuple[Gdk.Paintable, int, int]] = []
    source = SimpleNamespace(set_icon=lambda icon, x, y: icons.append((icon, x, y)))
    payload = card._drag.emit("prepare", 1.0, 1.0)
    assert isinstance(payload, Gdk.ContentProvider)
    assert payload.ref_formats().contain_gtype(str)
    card._drag_begin(source, None)
    assert page._dragging
    try:
        icon, hotspot_x, hotspot_y = icons[0]
        assert isinstance(icon, Gtk.WidgetPaintable)
        assert (icon.get_intrinsic_width(), icon.get_intrinsic_height()) == (64, 44)
        assert (hotspot_x, hotspot_y) == (32, 22)
        # A realized empty picture yields GdkEmptyPaintable. The type label's
        # rendered text must instead provide an actual visible render node.
        assert type(icon.get_current_image()).__name__ != "GdkEmptyPaintable"
    finally:
        card._drag_cancel(card._drag, None, Gdk.DragCancelReason.NO_TARGET)
        assert not page._dragging
        card._drag_end(card._drag, None, False)
        assert not page._dragging


def test_source_type_and_collision_detail_preserve_distinct_pairings_and_identity(
    make_editor: Any,
) -> None:
    items = (
        MediaItem(Path("/test-media/moving-grid.png"), Kind.STILL, 1, 1),
        MediaItem(Path("/test-media/moving-grid.mp4"), Kind.VIDEO, 1, 1),
        MediaItem(Path("/test-media/workshop/123"), Kind.SCENE, 1, 1, title="moving-grid"),
        MediaItem(Path("/test-media/first/common/clouds.png"), Kind.STILL, 1, 1),
        MediaItem(Path("/test-media/second/common/clouds.png"), Kind.STILL, 1, 1),
    )
    app, page = make_editor(items=items, entries=0)
    _present(page)
    cards = dict(page._source_cards_by_path)
    assert [cards[item.path]._description.get_label() for item in items] == [
        "Still",
        "Video",
        "Scene",
        "Still · first/common/clouds.png",
        "Still · second/common/clouds.png",
    ]
    for item in items:
        card = cards[item.path]
        assert card._description.get_visible()
        assert card._description.get_ellipsize() == Pango.EllipsizeMode.MIDDLE
        card._add.emit("clicked")
    assert [entry.source for entry in app.session.playlists.find("evening").entries] == [
        str(item.path) for item in items
    ]
    page._source_search.set_text("first/common")
    assert page._source_cards_by_path == cards
    assert cards[items[3].path]._description.get_label() == "Still · first/common/clouds.png"


def test_source_collision_detail_considers_pairings_beyond_the_current_page(
    make_editor: Any,
) -> None:
    items = tuple(
        MediaItem(Path(f"/test-media/folder-{index:03d}/clouds.png"), Kind.STILL, 1, 1)
        for index in range(playlists_page.SOURCE_PAGE_SIZE + 1)
    )
    _app, page = make_editor(items=items, entries=0)
    assert len(page._source_cards) == playlists_page.SOURCE_PAGE_SIZE
    first = page._source_cards_by_path[items[0].path]
    assert first._description.get_label() == "Still · folder-000/clouds.png"
    page._source_search.set_text(f"folder-{len(items) - 1:03d}")
    last = page._source_cards_by_path[items[-1].path]
    assert last._description.get_label() == "Still · folder-048/clouds.png"
    assert len(page._source_cards) == playlists_page.SOURCE_PAGE_SIZE + 1


def test_type_labels_keep_the_fixed_thumbnail_and_reachable_add_button_at_large_text(
    make_editor: Any,
) -> None:
    settings = Gtk.Settings.get_default()
    assert settings is not None
    previous_font = settings.get_property("gtk-font-name")
    settings.set_property("gtk-font-name", "Sans 24")
    try:
        items = (
            MediaItem(Path("/test-media/workshop/123"), Kind.SCENE, 1, 1, title="Evening clouds"),
        )
        _app, page = make_editor(items=items, entries=1)
        _present(page)
        card = next(iter(page._source_cards))
        assert _settle(lambda: card.get_width() > 0)
        assert (card._preview.get_width(), card._preview.get_height()) == (64, 44)
        assert (card.picture.get_width(), card.picture.get_height()) == (64, 44)
        assert card._description.get_label() == "Scene"
        assert card._description.get_width() > 0
        assert card._add.get_width() >= 24
        assert card._add.get_allocation().x + card._add.get_width() <= card.get_width()
        assert next(iter(page._entry_rows.values())).handle.get_size_request() == (46, 72)
    finally:
        settings.set_property("gtk-font-name", previous_font)


def test_search_and_empty_states_explain_pairings_without_rebuilding_controls(
    make_editor: Any,
) -> None:
    app, page = make_editor(entries=0)
    search = page._source_search
    cards = dict(page._source_cards_by_path)
    assert page._order_empty.get_visible()
    assert page._entry_count.get_label() == "0 pairings · empty playlist"

    search.set_text("not-a-match")
    assert page._source_empty.get_visible()
    assert "No matching pairings" in page._source_empty.get_label()
    assert page._source_count.get_label() == "0 of 0 matching pairings shown"
    search.set_text("pairing-001")
    assert not page._source_empty.get_visible()
    assert page._source_count.get_label() == "1 of 1 matching pairings shown"

    card = page._source_cards_by_path[app.session.library.items[1].path]
    card._add.emit("clicked")
    card._add.emit("clicked")
    assert page._source_search is search
    assert search.get_text() == "pairing-001"
    assert page._source_cards_by_path == cards
    assert page._entry_count.get_label() == "2 pairings · plays in this order"
    assert not page._order_empty.get_visible()
    saved = app.session.playlists.find("evening")
    assert [entry.source for entry in saved.entries] == [str(card.item.path)] * 2
    assert len({entry.id for entry in saved.entries}) == 2


def test_empty_library_gives_an_actionable_hint(make_editor: Any) -> None:
    _app, page = make_editor(count=0, entries=0)
    assert page._source_empty.get_visible()
    assert "Add a folder in Settings or get wallpapers from Store" in page._source_empty.get_label()
    assert page._source_count.get_label() == "0 library pairings"
    assert not page._source_more.get_visible()


def test_source_counts_follow_bounded_pages_and_full_inventory_search(make_editor: Any) -> None:
    count = playlists_page.SOURCE_PAGE_SIZE + 3
    _app, page = make_editor(count=count, entries=0)
    first = next(iter(page._source_cards))
    assert page._source_count.get_label() == f"48 of {count} library pairings shown"
    page._source_more.emit("clicked")
    assert page._source_count.get_label() == f"{count} library pairings"
    assert first in page._source_cards
    assert not page._source_more.get_visible()

    page._source_search.set_text(f"pairing-{count - 1:03d}")
    assert page._source_count.get_label() == "1 of 1 matching pairings shown"
    assert not page._source_empty.get_visible()


def test_source_row_activation_adds_once_and_respects_unavailable_pairings(
    make_editor: Any,
) -> None:
    app, page = make_editor(entries=0)
    card = next(iter(page._source_cards))
    child = card.get_parent()
    assert isinstance(child, Gtk.FlowBoxChild)
    assert not page._source_flow.get_activate_on_single_click()
    page._source_flow.emit("child-activated", child)
    assert len(app.session.playlists.find("evening")) == 1
    assert page._entry_count.get_label() == "1 pairing · fixed choice"

    card.set_borked("test playback failure")
    page._source_flow.emit("child-activated", child)
    assert len(app.session.playlists.find("evening")) == 1


def test_enter_renames_through_authoring_without_replacing_the_editor(make_editor: Any) -> None:
    app, page = make_editor()
    entry = page._name_entry
    search = page._source_search
    entry.set_text("Late evening")
    entry.emit("activate")
    assert app.session.playlists.find("evening").name == "Late evening"
    assert page._name_entry is entry
    assert page._source_search is search
    assert app.published == 1


@pytest.mark.parametrize(("removed", "neighbor"), [("entry-0", "entry-1"), ("entry-2", "entry-1")])
def test_removal_keeps_keyboard_focus_on_a_neighbor_after_saving(
    make_editor: Any, removed: str, neighbor: str
) -> None:
    app, page = make_editor()
    _present(page)
    row = page._entry_rows[removed]
    row.grab_focus()
    assert _settle(lambda: page._focused_entry_id() == removed)
    app.defer = True
    page._make_remove(removed)(Gtk.Button())
    assert page._entry_rows[removed] is row
    assert page._focused_entry_id() == removed

    app.pending.pop()()
    assert removed not in page._entry_rows
    assert _settle(lambda: page._focused_entry_id() == neighbor)


def test_delayed_removal_does_not_steal_focus_from_search(make_editor: Any) -> None:
    app, page = make_editor()
    _present(page)
    page._entry_rows["entry-0"].grab_focus()
    app.defer = True
    page._make_remove("entry-0")(Gtk.Button())
    page._source_search.grab_focus()
    assert _settle(lambda: _has_focus_within(page._source_search))
    app.pending.pop()()
    assert _has_focus_within(page._source_search)


def test_failed_removal_keeps_the_saved_row_and_keyboard_focus(
    make_editor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, page = make_editor()
    _present(page)
    row = page._entry_rows["entry-0"]
    row.grab_focus()

    def fail(_playlist_id: str, _entry_id: str) -> playlists.Playlist:
        raise playlists.PlaylistError("local-io", "test disk is full")

    monkeypatch.setattr(app.session.playlists, "remove_entry", fail)
    page._make_remove("entry-0")(Gtk.Button())
    assert page._entry_rows["entry-0"] is row
    assert page._focused_entry_id() == "entry-0"
    assert len(app.session.playlists.find("evening")) == 3
    assert app.published == 0
    assert len(app.reports) == 1


def test_ctrl_arrows_keep_real_keyboard_focus_on_the_same_pairing_row(make_editor: Any) -> None:
    app, page = make_editor()
    window = _present(page)
    row: playlists_page._PlaylistEntryRow = page._entry_rows["entry-0"]
    page._source_search.grab_focus()
    assert page._order_list.child_focus(Gtk.DirectionType.TAB_FORWARD)
    assert page._focused_entry_id(), "Tab navigation must reach the custom list's pairing rows"
    assert row.grab_focus(), "custom-list rows must not require a Gtk.ListBox parent to focus"
    assert window.get_focus() is row
    keys = next(
        controller
        for controller in row.observe_controllers()
        if isinstance(controller, Gtk.EventControllerKey)
    )
    assert keys.emit("key-pressed", Gdk.KEY_Down, 0, Gdk.ModifierType.CONTROL_MASK)
    assert [entry.id for entry in app.session.playlists.find("evening").entries] == [
        "entry-1",
        "entry-0",
        "entry-2",
    ]
    assert window.get_focus() is row
    assert keys.emit("key-pressed", Gdk.KEY_Up, 0, Gdk.ModifierType.CONTROL_MASK)
    assert [entry.id for entry in app.session.playlists.find("evening").entries] == [
        "entry-0",
        "entry-1",
        "entry-2",
    ]
    assert window.get_focus() is row


@pytest.mark.parametrize("move_to_search", [False, True])
def test_delayed_reorder_retains_only_the_focus_still_owned_by_its_row(
    make_editor: Any, move_to_search: bool
) -> None:
    app, page = make_editor()
    window = _present(page)
    row = page._entry_rows["entry-0"]
    assert row.grab_focus()
    app.defer = True
    assert page._make_reorder_key("entry-0")(
        Gtk.EventControllerKey(), Gdk.KEY_Down, 0, Gdk.ModifierType.CONTROL_MASK
    )
    assert app.session.playlists.find("evening").entries[0].id == "entry-0"
    if move_to_search:
        page._source_search.grab_focus()
        assert _has_focus_within(page._source_search)

    app.pending.pop()()

    assert [entry.id for entry in app.session.playlists.find("evening").entries] == [
        "entry-1",
        "entry-0",
        "entry-2",
    ]
    assert page._entry_rows["entry-0"] is row
    if move_to_search:
        assert _has_focus_within(page._source_search)
    else:
        assert window.get_focus() is row


def test_delayed_reorder_does_not_focus_or_animate_another_playlist(
    make_editor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, page = make_editor()
    _present(page)
    second = app.session.playlists.create("Morning", entry_id="morning")
    # Entry identifiers only need to be unique within one playlist. Make the
    # other editor contain the same id so an id-only focus guard is insufficient.
    app.session.playlists.add(second.id, app.session.library.items[0].path, entry_id="entry-0")
    page.refresh(app.session)
    page._entry_rows["entry-0"].grab_focus()
    app.defer = True
    assert page._make_reorder_key("entry-0")(
        Gtk.EventControllerKey(), Gdk.KEY_Down, 0, Gdk.ModifierType.CONTROL_MASK
    )
    page._list.select_row(page._playlist_rows_by_id[second.id][0])
    page._source_search.grab_focus()
    assert _has_focus_within(page._source_search)
    animations: list[Any] = []
    monkeypatch.setattr(page._order_list, "animate_from", animations.append)

    app.pending.pop()()

    assert app.session.playlists.find("evening").entries[0].id == "entry-1"
    assert app.session.playlists.find(second.id).entries[0].id == "entry-0"
    assert page._selected == second.id
    assert _has_focus_within(page._source_search)
    assert not animations


@pytest.mark.parametrize("move_to_search", [False, True])
def test_failed_delayed_reorder_preserves_saved_order_and_current_focus(
    make_editor: Any, monkeypatch: pytest.MonkeyPatch, move_to_search: bool
) -> None:
    app, page = make_editor()
    window = _present(page)
    row = page._entry_rows["entry-0"]
    assert row.grab_focus()

    def fail(_playlist_id: str, _entry_id: str, _step: int) -> playlists.Playlist:
        raise playlists.PlaylistError("local-io", "test disk is full")

    monkeypatch.setattr(app.session.playlists, "move_entry_relative", fail)
    app.defer = True
    assert page._make_reorder_key("entry-0")(
        Gtk.EventControllerKey(), Gdk.KEY_Down, 0, Gdk.ModifierType.CONTROL_MASK
    )
    if move_to_search:
        page._source_search.grab_focus()

    app.pending.pop()()

    assert [entry.id for entry in app.session.playlists.find("evening").entries] == [
        "entry-0",
        "entry-1",
        "entry-2",
    ]
    assert app.published == 0
    assert len(app.reports) == 1
    if move_to_search:
        assert _has_focus_within(page._source_search)
    else:
        assert window.get_focus() is row


@pytest.mark.parametrize(
    ("entry_id", "keys"),
    [("entry-0", (Gdk.KEY_Down, Gdk.KEY_Up)), ("entry-4", (Gdk.KEY_Up, Gdk.KEY_Down))],
)
def test_rapid_boundary_reversal_queues_both_gestures_in_actor_order(
    make_editor: Any, entry_id: str, keys: tuple[int, int]
) -> None:
    app, page = make_editor(count=5, entries=5)
    window = _present(page)
    row = page._entry_rows[entry_id]
    row.grab_focus()
    before = app.session.playlists.find("evening")
    app.defer = True
    reorder = page._make_reorder_key(entry_id)
    for key in keys:
        assert reorder(Gtk.EventControllerKey(), key, 0, Gdk.ModifierType.CONTROL_MASK)
    assert len(app.pending) == 2
    assert app.session.playlists.find("evening") == before
    assert app.pending.pop(0)()
    assert app.session.playlists.find("evening") != before
    assert app.pending.pop(0)()
    assert app.session.playlists.find("evening") == before
    assert app.published == 2
    assert window.get_focus() is row


@pytest.mark.parametrize(("entry_id", "key"), [("entry-0", Gdk.KEY_Down), ("entry-4", Gdk.KEY_Up)])
def test_repeated_queued_arrows_clamp_only_after_prior_gestures_save(
    make_editor: Any, entry_id: str, key: int
) -> None:
    app, page = make_editor(count=5, entries=5)
    window = _present(page)
    row = page._entry_rows[entry_id]
    row.grab_focus()
    app.defer = True
    reorder = page._make_reorder_key(entry_id)
    for _ in range(7):
        assert reorder(Gtk.EventControllerKey(), key, 0, Gdk.ModifierType.CONTROL_MASK)
    assert len(app.pending) == 7
    while app.pending:
        assert app.pending.pop(0)()
    entries = app.session.playlists.find("evening").entries
    assert entries[4 if key == Gdk.KEY_Down else 0].id == entry_id
    assert app.published == 4, "clamped gestures must not republish a no-op"
    assert window.get_focus() is row


@pytest.mark.parametrize(("entry_id", "key"), [("entry-0", Gdk.KEY_Up), ("entry-2", Gdk.KEY_Down)])
def test_boundary_noop_does_not_write_publish_or_animate(
    make_editor: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry_id: str, key: int
) -> None:
    app, page = make_editor()
    window = _present(page)
    row = page._entry_rows[entry_id]
    row.grab_focus()
    target = tmp_path / "playlists.json"
    original = (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino)
    animations: list[Any] = []
    monkeypatch.setattr(page._order_list, "animate_from", animations.append)
    assert page._make_reorder_key(entry_id)(
        Gtk.EventControllerKey(), key, 0, Gdk.ModifierType.CONTROL_MASK
    )
    assert (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino) == original
    assert app.published == 0
    assert not animations
    assert window.get_focus() is row


@pytest.mark.parametrize("failed_gesture", [0, 1])
def test_failed_queued_gesture_does_not_discard_or_invent_later_relative_moves(
    make_editor: Any, monkeypatch: pytest.MonkeyPatch, failed_gesture: int
) -> None:
    app, page = make_editor(count=5, entries=5)
    window = _present(page)
    row = page._entry_rows["entry-4"]
    row.grab_focus()
    store: playlists.Store = app.session.playlists
    move = store.move_entry_relative
    called = 0

    def sometimes_fail(playlist_id: str, entry_id: str, step: int) -> playlists.Playlist:
        nonlocal called
        gesture = called
        called += 1
        if gesture == failed_gesture:
            raise playlists.PlaylistError("local-io", "test disk is full")
        return move(playlist_id, entry_id, step)

    monkeypatch.setattr(app.session.playlists, "move_entry_relative", sometimes_fail)
    app.defer = True
    reorder = page._make_reorder_key("entry-4")
    for key in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Up):
        assert reorder(Gtk.EventControllerKey(), key, 0, Gdk.ModifierType.CONTROL_MASK)
    results = []
    while app.pending:
        results.append(app.pending.pop(0)())
    assert results.count(False) == 1
    assert len(app.reports) == 1
    entries = app.session.playlists.find("evening").entries
    assert entries[3 if failed_gesture == 0 else 2].id == "entry-4"
    assert app.published == (1 if failed_gesture == 0 else 2)
    assert window.get_focus() is row


def test_queued_reversal_does_not_focus_or_animate_the_newly_selected_playlist(
    make_editor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, page = make_editor()
    _present(page)
    second = app.session.playlists.create("Morning", entry_id="morning")
    app.session.playlists.add(second.id, app.session.library.items[0].path, entry_id="entry-0")
    page.refresh(app.session)
    page._entry_rows["entry-0"].grab_focus()
    before = app.session.playlists.find("evening")
    app.defer = True
    reorder = page._make_reorder_key("entry-0")
    for key in (Gdk.KEY_Down, Gdk.KEY_Up):
        assert reorder(Gtk.EventControllerKey(), key, 0, Gdk.ModifierType.CONTROL_MASK)
    page._list.select_row(page._playlist_rows_by_id[second.id][0])
    page._source_search.grab_focus()
    animations: list[Any] = []
    monkeypatch.setattr(page._order_list, "animate_from", animations.append)
    assert len(app.pending) == 2
    while app.pending:
        assert app.pending.pop(0)()
    assert app.session.playlists.find("evening") == before
    assert page._selected == second.id
    assert _has_focus_within(page._source_search)
    assert not animations
