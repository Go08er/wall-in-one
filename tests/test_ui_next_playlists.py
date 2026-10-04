"""The new interface's Playlists page over the real adapter and the real playlist store.

The page (`wall_in_one.ui.next.playlists`) runs over `RealAppState` and its
`RealPlaylists`, a real `Session` with stores in the test's own XDG dirs, and
the queue-holding `FakeApplication` of ``test_next_state``: nothing the page
shows changes until a test runs the authoring queue, which is how each test
watches the page show what is saved, what is asked for meanwhile, and what is
saved again after a failure.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, Graphene, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_next_state import FakeApplication, _adapter, _library, _session  # noqa: E402
from wall_in_one import paths  # noqa: E402
from wall_in_one.library import playlists as store_module  # noqa: E402
from wall_in_one.library.model import MediaItem  # noqa: E402
from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME  # noqa: E402
from wall_in_one.ui.next import playlists  # noqa: E402
from wall_in_one.ui.next.catalog import CLASSIC_HINT  # noqa: E402
from wall_in_one.ui.next.page import Placeholder  # noqa: E402
from wall_in_one.ui.next.playlists import ENTRY_PAGE_SIZE, EntryRow, PlaylistPage  # noqa: E402
from wall_in_one.ui.next.real_state import RealAppState  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> Iterator[None]:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()
    settings = Gtk.Settings.get_default()
    assert settings is not None
    animated = settings.props.gtk_enable_animations
    # Rows settle at once: what is shown is decided, not how it glides there.
    settings.props.gtk_enable_animations = False
    yield
    settings.props.gtk_enable_animations = animated


class Shown:
    """One page over one adapter, in a window with its header, as the shell holds it."""

    def __init__(self, backend: FakeApplication, pid: str | None = None) -> None:
        self.backend = backend
        self.state, self.keeper = _adapter(backend)
        page = playlists.create(self.state)
        assert isinstance(page, PlaylistPage)
        self.page = page
        self.toasts: list[tuple[str, Any]] = []
        self.state.connect("toast", lambda _s, text, button: self.toasts.append((text, button)))
        self.state.connect("navigate", lambda _s, key: self._navigate(key))
        header = Adw.HeaderBar()
        for widget in page.header_start():
            header.pack_start(widget)
        for widget in page.header_end():
            header.pack_end(widget)
        header.set_title_widget(page.title_widget())
        view = Adw.ToolbarView(content=page.widget)
        view.add_top_bar(header)
        self.window = Adw.Window(default_width=1100, default_height=900, content=view)
        self.window.present()
        page.activate(pid)
        spin_until(lambda: page.widget.get_mapped(), what="the page on screen")

    def _navigate(self, key: str) -> None:
        name, _, argument = key.partition(":")
        if name == "playlist":
            self.page.activate(argument or None)

    def settle(self) -> None:
        spin_until(
            lambda: all(row.get_height() > 0 for row in self.page.rows), what="rows laid out"
        )

    def run(self) -> None:
        self.backend.run_authoring()
        self.settle()

    def order(self) -> list[str]:
        return [row.entry_id for row in self.page.rows]

    def saved(self, pid: str = "evening") -> list[str]:
        return [entry.id for entry in self.backend.session.playlists.find(pid).entries]

    def close(self) -> None:
        self.window.destroy()
        self.keeper.close(wait=True)


@pytest.fixture
def items(tmp_path: Path) -> tuple[MediaItem, ...]:
    return _library(tmp_path / "home" / "Pictures" / "Wallpapers")


@pytest.fixture
def backend(items: tuple[MediaItem, ...]) -> Iterator[FakeApplication]:
    session = _session(items)
    try:
        yield FakeApplication(session)
    finally:
        session.shutdown()


@pytest.fixture
def make(backend: FakeApplication) -> Iterator[Any]:
    shown: list[Shown] = []

    def build(pid: str | None = "evening") -> Shown:
        one = Shown(backend, pid)
        shown.append(one)
        return one

    yield build
    for one in shown:
        one.close()


def _evening(backend: FakeApplication, count: int = 3) -> None:
    store = backend.session.playlists
    store.create("Evening", entry_id="evening")
    for index, item in enumerate(backend.session.library.items[:count]):
        store.add("evening", item.path, entry_id=f"e{index}")


def _keys(row: EntryRow) -> Gtk.EventControllerKey:
    return next(
        controller
        for controller in row.observe_controllers()
        if isinstance(controller, Gtk.EventControllerKey)
    )


# -- what the page shows -----------------------------------------------------------------------


def test_rows_are_the_saved_entries_with_missing_and_skipped_ones_named(
    backend: FakeApplication, make: Any
) -> None:
    _evening(backend, count=2)
    items = backend.session.library.items
    backend.session.playlists.add("evening", Path("/not/mounted/dune.png"), entry_id="gone")
    backend.session.playlists.add("evening", items[3].path, entry_id="skip")
    backend.session.pairings.mark_borked(items[3], "codec missing", "runtime")
    shown = make()
    shown.state.reload()
    shown.settle()
    page = shown.page
    assert shown.order() == ["e0", "e1", "gone", "skip"]
    titles = [row.get_title() for row in page.rows]
    assert titles == [
        "alpine",
        "city-dusk",
        "Missing · dune.png",
        "Playback unavailable · rain",
    ]
    assert page.rows[3].get_tooltip_text() == "codec missing"
    assert page._summary.get_label() == "4 wallpapers · 2 unavailable"
    assert page._play.get_tooltip_text() == "2 unavailable entries are skipped"
    assert [row.number.get_label() for row in page.rows] == ["1", "2", "3", "4"]


def test_your_pick_never_shows(backend: FakeApplication, make: Any) -> None:
    _evening(backend)
    store = backend.session.playlists
    store.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, backend.session.library.items[0].path)
    shown = make(QUICK_CHOICE_ID)
    assert [playlist.id for playlist in shown.state.playlists] == ["evening", "all-media"]
    assert shown.page.pid == "evening", "an id the page can't show opens a playlist it can"
    assert store.get(QUICK_CHOICE_ID) is not None


def test_long_playlists_build_one_page_of_rows_at_a_time(
    backend: FakeApplication, make: Any
) -> None:
    store = backend.session.playlists
    store.create("Evening", entry_id="evening")
    sources = [item.path for item in backend.session.library.items]
    placed = [
        (index, store_module.Entry(f"e{index}", str(sources[index % len(sources)])))
        for index in range(ENTRY_PAGE_SIZE + 28)
    ]
    store.insert_entries("evening", placed)
    shown = make()
    page = shown.page
    assert len(page.rows) == ENTRY_PAGE_SIZE
    assert page.more.get_visible()
    assert page.more.get_label() == f"Show 28 more · {ENTRY_PAGE_SIZE} of 100 shown"
    first = page.rows[0]
    page.show_more()
    assert len(page.rows) == 100 and not page.more.get_visible()
    assert page.rows[0] is first, "rows already shown are kept"


# -- create, rename, delete ---------------------------------------------------------------------


def test_a_new_playlist_opens_once_it_is_saved(backend: FakeApplication, make: Any) -> None:
    _evening(backend)
    shown = make()
    page = shown.page
    pid = page.create("Morning")
    assert page.pid == pid and page._outer.get_visible_child_name() == "waiting"
    shown.run()
    spin_until(lambda: page._outer.get_visible_child_name() == "playlist")
    detail = page.detail()
    assert detail is not None and detail.name == "Morning"
    assert page._area.get_visible_child_name() == "empty"
    assert not page._default.get_sensitive(), "nothing to play: it can't be the default"
    assert page._default.get_tooltip_text() == "Add wallpapers to it first"


def test_renaming_in_place_shows_the_name_asked_for_then_what_is_saved(
    backend: FakeApplication, make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _evening(backend)
    shown = make()
    page = shown.page
    page.start_rename()
    page._name.set_text("Late evening")
    page._name.stop_editing(True)
    assert page._name.get_text() == "Late evening"
    assert backend.session.playlists.find("evening").name == "Evening"
    shown.run()
    assert backend.session.playlists.find("evening").name == "Late evening"
    assert page._name.get_text() == "Late evening"

    def refuse(_identifier: str, _name: str) -> store_module.Playlist:
        raise store_module.PlaylistError("local-io", "test disk is full")

    monkeypatch.setattr(backend.session.playlists, "rename", refuse)
    page.start_rename()
    page._name.set_text("Night")
    page._name.stop_editing(True)
    assert page._name.get_text() == "Night"
    shown.run()
    assert page._name.get_text() == "Late evening", "back on the saved name"
    assert backend.reports[-1].endswith("nothing changed: local-io: test disk is full")


def test_delete_says_what_goes_with_it_and_then_shows_a_neighbour(
    backend: FakeApplication, make: Any
) -> None:
    _evening(backend)
    store = backend.session.playlists
    store.create("Morning", entry_id="morning")
    store.add("morning", backend.session.library.items[0].path)
    backend.session.schedules.add("evening", weekdays=("sat",))
    shown = make()
    page = shown.page
    page.confirm_delete()
    dialog = page._dialog
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_heading() == "Delete \u201cEvening\u201d?"
    assert dialog.get_body() == (
        "This removes 3 playlist entries and 1 schedule rule. "
        "Wallpaper files stay in the library. This cannot be undone."
    )
    dialog.emit("response", "delete")
    assert store.get("evening") is not None, "deleted once saved"
    shown.run()
    assert store.get("evening") is None and not backend.session.schedules.rules
    spin_until(lambda: page.pid == "morning", what="the neighbour shown")
    assert shown.toasts[-1][0] == "Deleted \u201cEvening\u201d"


# -- adding --------------------------------------------------------------------------------------


def test_the_picker_adds_several_in_one_task_in_pick_order(
    backend: FakeApplication, make: Any
) -> None:
    _evening(backend, count=1)
    _give_the_video_a_still(backend)
    shown = make()
    page = shown.page
    picker = page.open_picker()
    assert picker is not None
    ids = picker.card_ids
    assert len(ids) == 4 and not picker.more.get_visible()
    picker.toggle(ids[3])
    picker.toggle(ids[1])
    picker.toggle(ids[2])
    picker.toggle(ids[2])
    picker.toggle(ids[2])
    assert picker.add_button.get_label() == "Add 3"
    picker.commit()
    assert len(backend.authoring) == 1, "one task for all of them"
    shown.run()
    sources = [entry.source for entry in backend.session.playlists.find("evening").entries]
    assert sources[1:] == [ids[3], ids[1], ids[2]]
    assert len(page.rows) == 4
    assert shown.toasts[-1][0] == "Added 3 wallpapers to \u201cEvening\u201d"


def test_the_picker_never_offers_a_wallpaper_playback_skips(
    backend: FakeApplication, make: Any
) -> None:
    _evening(backend, count=1)
    items = backend.session.library.items
    backend.session.pairings.mark_borked(items[3], "codec missing", "runtime")
    shown = make()
    shown.state.reload()
    picker = shown.page.open_picker()
    assert picker is not None
    picker.toggle(str(items[3].path))
    assert picker.picked == []
    assert "can\u2019t be added: codec missing" in (
        picker._cards[str(items[3].path)].get_tooltip_text() or ""
    )


def _give_the_video_a_still(backend: FakeApplication) -> None:
    """The fixture's video has no still of its own, so playback could not show it."""
    items = backend.session.library.items
    backend.session.pairings.choose_still(items[3], items[0].path)


def test_a_library_drop_lands_where_it_is_dropped(backend: FakeApplication, make: Any) -> None:
    _evening(backend, count=2)
    _give_the_video_a_still(backend)
    shown = make()
    shown.settle()
    page = shown.page
    target = next(
        controller
        for controller in page._list_wrap.observe_controllers()
        if isinstance(controller, Gtk.DropTarget)
    )
    assert target.get_preload(), "motion reads the payload, so it must be preloaded"
    # Motion fails open: GTK would never deliver the drop after a refusal.
    assert target.emit("motion", 10.0, 1.0) == Gdk.DragAction.COPY
    wid = str(backend.session.library.items[3].path)
    assert target.emit("drop", wid, 10.0, 1.0)
    shown.run()
    assert backend.session.playlists.find("evening").entries[0].source == wid


def test_a_drop_below_the_shown_rows_of_a_long_playlist_lands_at_its_end(
    backend: FakeApplication, make: Any
) -> None:
    """Only the first page of rows is built; below the last one is the true end."""
    store = backend.session.playlists
    store.create("Evening", entry_id="evening")
    sources = [str(item.path) for item in backend.session.library.items[:3]]
    placed = [
        (index, store_module.Entry(f"e{index}", sources[index % len(sources)]))
        for index in range(ENTRY_PAGE_SIZE + 28)
    ]
    store.insert_entries("evening", placed)
    _give_the_video_a_still(backend)
    shown = make()
    shown.settle()
    page = shown.page
    assert len(page.rows) == ENTRY_PAGE_SIZE and page.more.get_visible()
    target = next(
        controller
        for controller in page._list_wrap.observe_controllers()
        if isinstance(controller, Gtk.DropTarget)
    )
    last = page.rows[-1]
    ok, bounds = last.compute_bounds(page.list)
    below = (bounds.get_y() + bounds.get_height() + 4.0) if ok else 1e6
    assert page.drop_position(below) == ENTRY_PAGE_SIZE, "the line shows under the last row"
    ok, point = page.list.compute_point(page._list_wrap, Graphene.Point().init(10.0, below))
    wid = str(backend.session.library.items[3].path)
    assert target.emit("drop", wid, 10.0, point.y if ok else below)
    shown.run()
    entries = backend.session.playlists.find("evening").entries
    assert len(entries) == ENTRY_PAGE_SIZE + 29
    assert entries[-1].source == wid, "at the end, never after the last row shown"
    assert all(entry.source != wid for entry in entries[:-1])


# -- reordering ------------------------------------------------------------------------------------


def test_the_reorder_gesture_is_measured_on_the_scrolled_window(
    backend: FakeApplication, make: Any
) -> None:
    """DECISIONS.md: measure a gesture where nothing it causes can move."""
    _evening(backend)
    shown = make()
    page = shown.page
    gesture = page.list.gesture
    assert gesture is not None and gesture.get_widget() is page.scroller
    for row in page.rows:
        assert row.handle is not None and not isinstance(row.handle, Gtk.Button)
        widget: Gtk.Widget | None = gesture.get_widget()
        while widget is not None:
            assert widget is not row, "the gesture must not be inside a row"
            widget = widget.get_parent()


def test_ctrl_arrows_queue_relative_moves_and_keep_focus_on_the_row(
    backend: FakeApplication, make: Any
) -> None:
    _evening(backend, count=4)
    shown = make()
    shown.settle()
    page = shown.page
    row = page.row("e0")
    assert row is not None and row.grab_focus()
    keys = _keys(row)
    for _press in range(2):
        assert keys.emit("key-pressed", Gdk.KEY_Down, 0, Gdk.ModifierType.CONTROL_MASK)
    assert len(shown.backend.authoring) == 2
    assert shown.order() == ["e0", "e1", "e2", "e3"], "a key move shows when it is saved"
    assert page._saving.get_visible()
    shown.run()
    assert shown.saved() == shown.order() == ["e1", "e2", "e0", "e3"]
    assert page.row("e0") is row and shown.window.get_focus() is row
    assert row.number.get_label() == "3"
    assert not page._saving.get_visible()
    # Past the end changes nothing and publishes nothing.
    changes = backend.playlist_changes
    assert keys.emit("key-pressed", Gdk.KEY_End, 0, Gdk.ModifierType.CONTROL_MASK)
    assert keys.emit("key-pressed", Gdk.KEY_Down, 0, Gdk.ModifierType.CONTROL_MASK)
    shown.run()
    assert shown.saved() == ["e1", "e2", "e3", "e0"]
    assert backend.playlist_changes == changes + 1


def test_a_dropped_row_stays_put_until_saved_and_goes_back_if_the_save_fails(
    backend: FakeApplication, make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _evening(backend, count=4)
    shown = make()
    shown.settle()
    page = shown.page
    rows = page.rows
    heights = page.list.row_heights()

    def drag(source: int, target: int) -> None:
        """Carry row ``source`` just past row ``target``'s middle, and drop it."""
        page.list.begin_drag(page.rows[source])
        tops = [sum(heights[:index]) for index in range(len(heights))]
        lean = 0.6 if target > source else 0.4
        middle = tops[target] + heights[target] * lean
        page.list.update_drag(middle - (tops[source] + heights[source] / 2))
        page.list.end_drag()

    drag(0, 2)
    spin_until(lambda: not page.list.busy, what="the dropped row to settle")
    assert len(backend.authoring) == 1, "one relative move for the drop"
    assert shown.order() == ["e1", "e2", "e0", "e3"], "shown where it was dropped"
    shown.run()
    assert shown.saved() == shown.order() == ["e1", "e2", "e0", "e3"]
    assert [page.row(entry) for entry in ("e0", "e1", "e2", "e3")] == [
        rows[0],
        rows[1],
        rows[2],
        rows[3],
    ], "the same rows, moved"

    def refuse(_identifier: str, _entry: str, _step: int) -> store_module.Playlist:
        raise store_module.PlaylistError("local-io", "test disk is full")

    monkeypatch.setattr(backend.session.playlists, "move_entry_relative", refuse)
    drag(3, 0)
    spin_until(lambda: not page.list.busy, what="the dropped row to settle")
    assert shown.order() == ["e3", "e1", "e2", "e0"]
    shown.run()
    assert shown.order() == shown.saved() == ["e1", "e2", "e0", "e3"], "back on what is saved"
    assert "nothing changed: local-io: test disk is full" in backend.reports[-1]


# -- removing --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("removed", "neighbour"), [("e0", "e1"), ("e2", "e1")])
def test_removal_keeps_the_keyboard_on_a_neighbour_and_offers_undo(
    backend: FakeApplication, make: Any, removed: str, neighbour: str
) -> None:
    _evening(backend)
    shown = make()
    shown.settle()
    page = shown.page
    row = page.row(removed)
    assert row is not None and row.grab_focus()
    assert _keys(row).emit("key-pressed", Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
    assert page.row(removed) is row and row.has_css_class("pl-removing"), "until it is saved"
    assert shown.window.get_focus() is row
    shown.run()
    assert page.row(removed) is None
    spin_until(lambda: shown.window.get_focus() is page.row(neighbour), what="focus on a neighbour")
    text, (label, undo) = shown.toasts[-1]
    assert label == "Undo" and text.startswith("Removed \u201c")
    undo()
    shown.run()
    assert shown.order() == ["e0", "e1", "e2"] == shown.saved()


def test_a_failed_removal_keeps_the_row(
    backend: FakeApplication, make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _evening(backend)
    shown = make()
    shown.settle()
    page = shown.page

    def refuse(_identifier: str, _entries: object) -> store_module.Playlist:
        raise store_module.PlaylistError("local-io", "test disk is full")

    monkeypatch.setattr(backend.session.playlists, "remove_entries", refuse)
    row = page.row("e1")
    page.remove_entries(["e1"])
    shown.run()
    assert page.row("e1") is row and row is not None and not row.has_css_class("pl-removing")
    assert shown.order() == ["e0", "e1", "e2"]
    assert not shown.toasts, "no Undo for a removal that didn't happen"


def test_selection_mode_removes_several_in_one_save(backend: FakeApplication, make: Any) -> None:
    _evening(backend, count=4)
    shown = make()
    shown.settle()
    page = shown.page
    page._select.set_active(True)
    for entry in ("e1", "e3"):
        row = page.row(entry)
        assert row is not None and row.check is not None
        row.check.set_active(True)
    assert page._selection_label.get_label() == "2 selected"
    page._remove_selected.emit("clicked")
    assert len(backend.authoring) == 1
    shown.run()
    assert shown.saved() == ["e0", "e2"] == shown.order()


# -- how it plays, and the guards ------------------------------------------------------------------


def test_interval_and_shuffle_rows_follow_what_is_saved(
    backend: FakeApplication, make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _evening(backend)
    shown = make()
    page = shown.page
    interval, shuffle = page.interval_row, page.shuffle_row
    assert interval.get_selected() == 0 and shuffle.get_selected() == 0
    words = interval.get_model()
    assert isinstance(words, Gtk.StringList)
    assert words.get_string(0) == "Default (every 5 minutes)"
    interval.set_selected(2)  # Default, 5 min, 15 min
    shown.run()
    assert backend.session.playlists.find("evening").cycle_interval == 900

    def refuse(*_args: object, **_kwargs: object) -> store_module.Playlist:
        raise store_module.PlaylistError("no-backup", "could not keep a copy")

    monkeypatch.setattr(backend.session.playlists, "set_rotation", refuse)
    shuffle.set_selected(1)
    assert shuffle.get_selected() == 1
    shown.run()
    assert shuffle.get_selected() == 0, "back on the saved choice"
    assert interval.get_selected() == 2


def test_a_used_playlist_keeps_its_last_playable_wallpaper(
    backend: FakeApplication, make: Any
) -> None:
    _evening(backend, count=1)
    backend.session.displays.assign("DP-1", "evening")
    shown = make()
    shown.settle()
    page = shown.page
    labels = [child for child in _children(page._used) if isinstance(child, Gtk.Button)]
    assert len(labels) == 1 and labels[0].get_tooltip_text() is not None
    page.remove_entries(["e0"])
    shown.run()
    assert shown.saved() == ["e0"] == shown.order()
    assert "is used by DP-1 and would have nothing left to play" in backend.reports[-1]


def _children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    found = []
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        child = child.get_next_sibling()
    return found


def test_files_from_a_newer_version_make_the_page_read_only(
    items: tuple[MediaItem, ...],
) -> None:
    target = paths.app_state_dir() / store_module.STATE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 99,
        "playlists": [
            {
                "id": "evening",
                "name": "Evening",
                "entries": [{"id": "e0", "source": str(items[0].path)}],
            }
        ],
    }
    target.write_text(json.dumps(document))
    before = target.read_bytes()
    session = _session(items)
    backend = FakeApplication(session)
    shown = Shown(backend, "evening")
    try:
        page = shown.page
        assert page._notice.get_revealed()
        assert page._notice.get_title() == (
            "Playlist changes are off while playlists.json is from a newer version of Wall-in-One"
        )
        assert not page.list.get_sensitive()
        assert not page._add_inline.get_sensitive()
        assert not page._name.get_sensitive() and not page._rename.get_sensitive()
        assert not page.interval_row.get_sensitive() and not page.shuffle_row.get_sensitive()
        assert not page._play.get_sensitive() and not page._default.get_sensitive()
        assert page.open_picker() is None
        page.remove_entries(["e0"])
        assert not backend.authoring and target.read_bytes() == before
    finally:
        shown.close()
        session.shutdown()


def test_the_page_is_a_placeholder_without_playlist_editing(
    backend: FakeApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(RealAppState, "playlist_editing", property(lambda _self: None))
    state, keeper = _adapter(backend)
    try:
        page = playlists.create(state)
        assert isinstance(page, Placeholder)
        assert page.status.get_description() == (
            f"Playlists is not in the new interface yet. {CLASSIC_HINT}"
        )
    finally:
        state.close()
        keeper.close(wait=True)
