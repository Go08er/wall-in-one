"""Authoring refreshes keep the controls a person is actively using.

The playlist editor once rebuilt its complete widget tree after every store
write. That made a single Add click erase a search, reset three scrollbars,
and move keyboard focus. These tests pin identity, not merely copied values:
restore-after-rebuild still flickers and still interrupts an edit in progress.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from wall_in_one import config  # noqa: E402
from wall_in_one.library import playlists, schedules  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui import playlists_page  # noqa: E402
from wall_in_one.ui.pairings_page import PairingsPage  # noqa: E402
from wall_in_one.ui.schedules_page import SchedulesPage  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class QuietLoader:
    """Do not start ffmpeg just to test widget reconciliation."""

    def request(self, _item: MediaItem, _callback: Any) -> None: ...

    def shutdown(self) -> None: ...


def _item(name: str) -> MediaItem:
    return MediaItem(
        path=Path("/test-media") / f"{name}.png",
        kind=Kind.STILL,
        size=1,
        mtime=1,
    )


def _session(tmp_path: Path) -> tuple[Session, playlists.Playlist, tuple[MediaItem, ...]]:
    items = (_item("alpine"), _item("forest"), _item("ocean"))
    store = playlists.Store(path=tmp_path / "playlists.json")
    chosen = store.create("Evening", entry_id="evening")
    store.add(chosen.id, items[0].path, entry_id="first")
    session = Session(
        config.Settings(active_playlist=chosen.id),
        scanner=lambda _roots: Library(roots=(Path("/test-media"),), items=items),
        playlist_store=store,
    )
    session.refresh()
    return session, store.find(chosen.id), items


class ScheduleApp:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.published = 0

    def schedule_edited(self) -> None:
        self.published += 1

    def window_report(self, _message: str) -> None: ...


def _put_scroll_at(scroller: Gtk.ScrolledWindow, value: float) -> None:
    scroller.get_vadjustment().configure(value, 0.0, 200.0, 1.0, 10.0, 20.0)


def _child_count(widget: Gtk.Widget) -> int:
    count = 0
    child = widget.get_first_child()
    while child is not None:
        count += 1
        child = child.get_next_sibling()
    return count


def test_playlist_entry_edit_diffs_widgets_and_keeps_interaction_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
    session, playlist, items = _session(tmp_path)
    page = playlists_page.PlaylistsPage(SimpleNamespace(session=session))  # type: ignore[arg-type]
    page.refresh(session)

    row = page._playlist_rows_by_id[playlist.id][0]
    editor = page._editor.get_first_child()
    search = page._source_search
    source_card = page._source_cards_by_path[items[0].path]
    entry_card = page._entry_cards["first"]
    search.set_text("forest")
    search.set_position(3)
    _put_scroll_at(page._sidebar_scroll, 17.0)
    _put_scroll_at(page._source_scroll, 31.0)
    _put_scroll_at(page._order_scroll, 47.0)

    # This is the model mutation an Add click makes before the app asks the
    # visible page to reconcile itself.
    session.playlists.add(playlist.id, items[1].path, entry_id="second")
    session.playlists_changed()
    page.refresh(session)

    assert page._playlist_rows_by_id[playlist.id][0] is row
    assert page._editor.get_first_child() is editor
    assert page._source_search is search
    assert page._source_cards_by_path[items[0].path] is source_card
    assert page._entry_cards["first"] is entry_card
    assert page._entry_cards["second"] is not entry_card
    assert search.get_text() == "forest"
    assert search.get_position() == 3
    assert page._sidebar_scroll.get_vadjustment().get_value() == 17.0
    assert page._source_scroll.get_vadjustment().get_value() == 31.0
    assert page._order_scroll.get_vadjustment().get_value() == 47.0
    session.shutdown()


def test_playlist_refresh_does_not_replace_focused_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
    session, playlist, items = _session(tmp_path)
    page = playlists_page.PlaylistsPage(SimpleNamespace(session=session))  # type: ignore[arg-type]
    page.refresh(session)
    root = Gtk.Window()
    root.set_child(page)
    search = page._source_search
    focused = search.grab_focus()
    focused_widget = root.get_focus()

    session.playlists.add(playlist.id, items[2].path, entry_id="third")
    session.playlists_changed()
    page.refresh(session)

    assert page._source_search is search
    if focused:
        assert root.get_focus() is focused_widget
    root.set_child(None)
    root.destroy()
    session.shutdown()


def test_cancelled_playlist_drag_removes_the_animated_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
    session, _playlist, _items = _session(tmp_path)
    page = playlists_page.PlaylistsPage(SimpleNamespace(session=session))  # type: ignore[arg-type]
    page.refresh(session)
    original_count = _child_count(page._order_flow)

    page._show_drop_gap(1, "")
    gap = page._drop_gap
    assert gap is not None
    assert _child_count(page._order_flow) == original_count + 1

    card = page._entry_cards["first"]
    assert isinstance(card, playlists_page._MediaCard)
    card._drag_cancel(None, None, None)  # type: ignore[arg-type]

    assert page._drop_gap is None
    assert _child_count(page._order_flow) == original_count
    session.shutdown()


def _drop_targets(widget: Gtk.Widget) -> list[Gtk.DropTarget]:
    found = [
        controller
        for controller in widget.observe_controllers()
        if isinstance(controller, Gtk.DropTarget)
    ]
    child = widget.get_first_child()
    while child is not None:
        found.extend(_drop_targets(child))
        child = child.get_next_sibling()
    return found


def test_reorder_drop_targets_preload_their_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without preloading, the payload is unreadable until a drop is accepted.

    Which is fatal rather than cosmetic: the motion handler decides whether the
    target accepts the drag, it decides that by looking at the payload, and a
    motion handler that answers zero means GTK never delivers a drop at all. The
    whole gesture dies silently, and every direct-call test still passes -- which
    is exactly how it shipped once.
    """
    monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
    session, _playlist, _items = _session(tmp_path)
    page = playlists_page.PlaylistsPage(SimpleNamespace(session=session))  # type: ignore[arg-type]
    page.refresh(session)

    targets = _drop_targets(page._order_flow)
    assert targets, "the playlist order flow has no drop targets at all"
    assert all(target.get_preload() for target in targets)
    session.shutdown()


def test_motion_accepts_a_payload_that_has_not_arrived_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail open. A refusal here cannot be recovered from later in the drag."""
    monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
    session, _playlist, _items = _session(tmp_path)
    page = playlists_page.PlaylistsPage(SimpleNamespace(session=session))  # type: ignore[arg-type]
    page.refresh(session)

    assert page._preview_drop(None, "first") != Gdk.DragAction(0)
    session.shutdown()


def test_schedule_rule_reorder_keeps_calendar_editor_and_scroll(tmp_path: Path) -> None:
    initial, playlist, _items = _session(tmp_path)
    rules = schedules.Store(path=tmp_path / "schedules.json")
    first = rules.add(playlist.id, months=(1,), weekdays=("mon",), rule_id="first")
    rules.add(playlist.id, months=(2,), weekdays=("tue",), rule_id="second")
    session = Session(
        config.Settings(active_playlist=playlist.id),
        scanner=lambda _roots: initial.library,
        playlist_store=initial.playlists,
        schedule_store=rules,
    )
    initial.shutdown()
    session.refresh()
    application = ScheduleApp(session)
    page = SchedulesPage(application)  # type: ignore[arg-type]
    page.refresh(session)
    page._edit_rule(first)
    playlist_picker = page._rule_playlist
    month_buttons = tuple(page._months)
    content = page._content.get_first_child()
    _put_scroll_at(page, 53.0)

    page._make_move("first", 1)(Gtk.Button())
    page.refresh(session)

    assert application.published == 1
    assert page._content.get_first_child() is content
    assert page._rule_playlist is playlist_picker
    assert tuple(page._months) == month_buttons
    assert page._months[0].get_active()
    assert page._editing_rule == "first"
    assert page.get_vadjustment().get_value() == 53.0
    session.shutdown()


def test_unchanged_pairing_refresh_keeps_editor_widgets(tmp_path: Path) -> None:
    session, _playlist, items = _session(tmp_path)
    application = SimpleNamespace(
        session=session,
        settings=session.settings,
        resolved_palette=None,
    )
    page = PairingsPage(application, lambda: None)  # type: ignore[arg-type]
    page.edit(session, items[0])
    editor = page._editor.get_first_child()
    still_picker = page._still_row
    _put_scroll_at(page._editor_scroll, 29.0)

    page.refresh(session)

    assert page._editor.get_first_child() is editor
    assert page._still_row is still_picker
    assert page._editor_scroll.get_vadjustment().get_value() == 29.0
    page.shutdown()
    session.shutdown()
