"""Full-page editor for named, ordered wallpaper rotations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gtk, Pango

from wall_in_one.library import playlists
from wall_in_one.library.model import MediaItem
from wall_in_one.ui.thumbnails import ThumbnailLoader

if TYPE_CHECKING:
    from wall_in_one.session import Session
    from wall_in_one.ui.app import Application


SOURCE_PREFIX = "media:"
ENTRY_PREFIX = "entry:"


class _MediaCard(Gtk.Box):
    """A pairing thumbnail that can be dragged, clicked, or removed."""

    def __init__(
        self,
        item: MediaItem,
        payload: str,
        on_activate: Any,
        *,
        on_drag_started: Any | None = None,
        on_drag_finished: Any | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.item = item
        self.add_css_class("card")
        self.set_size_request(180, -1)

        self.picture = Gtk.Picture(width_request=180, height_request=102)
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.append(self.picture)
        title = Gtk.Label(label=item.name, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("caption")
        self.append(title)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        actions.set_halign(Gtk.Align.CENTER)
        add = Gtk.Button(
            icon_name="list-add-symbolic",
            tooltip_text="Add to playlist",
        )
        add.add_css_class("flat")
        add.connect("clicked", on_activate)
        actions.append(add)
        self.append(actions)

        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag.connect(
            "prepare",
            lambda *_arguments: Gdk.ContentProvider.new_for_value(payload),
        )
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-cancel", self._drag_cancel)
        drag.connect("drag-end", self._drag_end)
        self._on_drag_started = on_drag_started
        self._on_drag_finished = on_drag_finished
        self.add_controller(drag)

    def _drag_begin(self, source: Gtk.DragSource, _drag: Gdk.Drag) -> None:
        """Keep the gesture visually tied to the image the person grabbed."""
        if self._on_drag_started is not None:
            self._on_drag_started()
        paintable = self.picture.get_paintable()
        if paintable is None:
            paintable = Gtk.WidgetPaintable.new(self)
        source.set_icon(paintable, 90, 51)

    def _drag_cancel(
        self, _source: Gtk.DragSource, _drag: Gdk.Drag, _reason: Gdk.DragCancelReason
    ) -> bool:
        self._finish_drag()
        return False

    def _drag_end(self, _source: Gtk.DragSource, _drag: Gdk.Drag, _delete_data: bool) -> None:
        self._finish_drag()

    def _finish_drag(self) -> None:
        if self._on_drag_finished is not None:
            self._on_drag_finished()

    def show_thumbnail(self, _item: MediaItem, texture: Gdk.Texture | None) -> None:
        self.picture.set_paintable(texture)


class _PlaylistEntryRow(Gtk.ListBoxRow):
    """A stable sortable row whose handle is its only drag gesture."""

    def __init__(
        self,
        identifier: str,
        item: MediaItem | None,
        source: Path,
        on_remove: Any,
        on_key: Any,
        on_drag_started: Any,
        on_drag_finished: Any,
    ) -> None:
        super().__init__(selectable=False, activatable=False, focusable=True)
        self.identifier = identifier
        self.item = item

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # The landing space is never inserted into GTK's child sequence. The
        # revealer lives for exactly as long as its row, so drag motion only
        # changes a property even while its closing animation is running.
        self.spacer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=160,
        )
        self.spacer.set_child(Gtk.Box(height_request=24))
        body.append(self.spacer)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(8)
        content.set_margin_end(8)

        self.handle = Gtk.Button(
            icon_name="list-drag-handle-symbolic",
            tooltip_text="Drag to reorder",
        )
        self.handle.add_css_class("flat")
        content.append(self.handle)

        self.picture = Gtk.Picture(width_request=96, height_request=54)
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        content.append(self.picture)

        name = item.name if item is not None else f"Missing · {source.name}"
        title = Gtk.Label(label=name, xalign=0.0, hexpand=True)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.add_css_class("heading")
        content.append(title)

        remove = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove from playlist")
        remove.add_css_class("flat")
        remove.connect("clicked", on_remove)
        content.append(remove)
        body.append(content)
        self.set_child(body)

        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect(
            "prepare",
            lambda *_arguments: Gdk.ContentProvider.new_for_value(f"{ENTRY_PREFIX}{identifier}"),
        )
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-cancel", self._drag_cancel)
        drag.connect("drag-end", self._drag_end)
        self._on_drag_started = on_drag_started
        self._on_drag_finished = on_drag_finished
        self.handle.add_controller(drag)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", on_key)
        self.add_controller(keys)

    def _drag_begin(self, source: Gtk.DragSource, _drag: Gdk.Drag) -> None:
        self._on_drag_started()
        paintable = self.picture.get_paintable()
        if paintable is None:
            paintable = Gtk.WidgetPaintable.new(self)
        source.set_icon(paintable, 48, 27)

    def _drag_cancel(
        self, _source: Gtk.DragSource, _drag: Gdk.Drag, _reason: Gdk.DragCancelReason
    ) -> bool:
        self._on_drag_finished()
        return False

    def _drag_end(self, _source: Gtk.DragSource, _drag: Gdk.Drag, _delete_data: bool) -> None:
        self._on_drag_finished()

    def show_thumbnail(self, _item: MediaItem, texture: Gdk.Texture | None) -> None:
        self.picture.set_paintable(texture)


class PlaylistsPage(Gtk.Box):
    """Create playlists and arrange their stable entries without a terminal."""

    def __init__(self, application: Application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._app = application
        self._session: Session | None = None
        self._selected = ""
        self._playlist_rows: dict[Gtk.ListBoxRow, str] = {}
        self._playlist_rows_by_id: dict[str, tuple[Gtk.ListBoxRow, Gtk.Label, Gtk.Label]] = {}
        self._playlist_positions: dict[str, int] = {}
        self._loader = ThumbnailLoader()
        self._source_cards: dict[_MediaCard, MediaItem] = {}
        self._source_cards_by_path: dict[Path, _MediaCard] = {}
        self._source_positions: dict[Path, int] = {}
        self._entry_rows: dict[str, _PlaylistEntryRow] = {}
        # Kept as an alias because permanence is about stable identity, not the
        # name callers used when the editor happened to be a card grid.
        self._entry_cards = self._entry_rows
        self._entry_items: dict[str, MediaItem | None] = {}
        self._entry_positions: dict[str, int] = {}
        self._revealed_slot = -1
        self._dragging = False
        self._playlist_change_after_drag = False
        self._editor_id = ""

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_position(300)
        split.set_shrink_start_child(False)
        split.set_shrink_end_child(False)
        split.set_start_child(self._sidebar())

        self._editor_scroll = Gtk.ScrolledWindow(vexpand=True)
        self._editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self._editor.set_margin_top(18)
        self._editor.set_margin_bottom(24)
        self._editor.set_margin_start(24)
        self._editor.set_margin_end(24)
        self._editor_scroll.set_child(self._editor)
        split.set_end_child(self._editor_scroll)
        self.append(split)
        self._show_empty()

    def shutdown(self) -> None:
        self._loader.shutdown()

    def _sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.set_margin_top(12)
        sidebar.set_margin_bottom(12)
        sidebar.set_margin_start(12)
        sidebar.set_margin_end(6)
        title = Gtk.Label(label="Playlists", xalign=0.0)
        title.add_css_class("title-2")
        sidebar.append(title)
        note = Gtk.Label(
            label="A one-item playlist is a fixed choice. Longer lists rotate in this order.",
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        sidebar.append(note)

        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._new_name = Gtk.Entry(hexpand=True, placeholder_text="New playlist")
        self._new_name.connect("activate", self._create)
        add_box.append(self._new_name)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Create playlist")
        add.connect("clicked", self._create)
        add_box.append(add)
        sidebar.append(add_box)

        self._sidebar_scroll = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._list.add_css_class("boxed-list")
        self._list.set_sort_func(self._compare_playlist_rows)
        self._list.connect("row-selected", self._on_selected)
        self._sidebar_scroll.set_child(self._list)
        sidebar.append(self._sidebar_scroll)
        return sidebar

    def refresh(self, session: Session) -> None:
        """Diff authoring state without replacing the editor under the user.

        Playlist edits are frequent, small changes. Rebuilding this page used
        to destroy the search entry and all three scroll adjustments after
        every add, remove, rename, or drag. Rows and cards are keyed by their
        stable model identities instead, exactly like the main media grid.
        """
        self._session = session
        available = session.playlists.all()
        incoming = {playlist.id: playlist for playlist in available}
        for identifier, (row, _name, _detail) in list(self._playlist_rows_by_id.items()):
            if identifier not in incoming:
                self._list.remove(row)
                self._playlist_rows.pop(row, None)
                del self._playlist_rows_by_id[identifier]

        self._playlist_positions = {playlist.id: index for index, playlist in enumerate(available)}
        for playlist in available:
            existing = self._playlist_rows_by_id.get(playlist.id)
            if existing is None:
                row, name, detail = self._new_playlist_row()
                self._playlist_rows[row] = playlist.id
                self._playlist_rows_by_id[playlist.id] = (row, name, detail)
                self._list.append(row)
            else:
                row, name, detail = existing
            name.set_label(playlist.name)
            detail.set_label(self._playlist_detail(session, playlist))
        self._list.invalidate_sort()

        wanted = self._selected if self._selected in incoming else ""
        if not wanted and available:
            wanted = available[0].id
        selected_record = self._playlist_rows_by_id.get(wanted)
        selected_row = selected_record[0] if selected_record is not None else None
        self._list.select_row(selected_row)
        if wanted != self._selected:
            self._selected = wanted
            self._show_editor()
        elif wanted:
            self._sync_editor()
        else:
            self._show_empty()

    @staticmethod
    def _new_playlist_row() -> tuple[Gtk.ListBoxRow, Gtk.Label, Gtk.Label]:
        row = Gtk.ListBoxRow()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(10)
        content.set_margin_end(10)
        name = Gtk.Label(xalign=0.0)
        name.add_css_class("heading")
        detail = Gtk.Label(xalign=0.0)
        detail.add_css_class("caption")
        detail.add_css_class("dim-label")
        content.append(name)
        content.append(detail)
        row.set_child(content)
        return row, name, detail

    @staticmethod
    def _playlist_detail(session: Session, playlist: playlists.Playlist) -> str:
        return (
            f"{len(playlist)} item{'s' if len(playlist) != 1 else ''}"
            + (" · playing" if playlist.id == session.active_playlist() else "")
            + (" · default" if playlist.id == session.settings.active_playlist else "")
        )

    def _compare_playlist_rows(self, first: Gtk.ListBoxRow, second: Gtk.ListBoxRow) -> int:
        first_id = self._playlist_rows.get(first, "")
        second_id = self._playlist_rows.get(second, "")
        end = len(self._playlist_positions)
        return self._playlist_positions.get(first_id, end) - self._playlist_positions.get(
            second_id, end
        )

    def _on_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        selected = self._playlist_rows.get(row, "") if row is not None else ""
        if selected == self._selected and self._editor_id == selected:
            return
        self._selected = selected
        self._show_editor()

    def _clear_editor(self) -> None:
        self._assert_not_dragging()
        self._clear_drop_slot()
        while (child := self._editor.get_first_child()) is not None:
            self._editor.remove(child)
        self._source_cards.clear()
        self._source_cards_by_path.clear()
        self._source_positions.clear()
        self._entry_rows.clear()
        self._entry_items.clear()
        self._entry_positions.clear()
        self._editor_id = ""

    def _show_empty(self) -> None:
        self._clear_editor()
        page = Adw.StatusPage(
            title="Make a playlist",
            description="Create a playlist on the left, then add media from your library.",
            icon_name="view-list-symbolic",
        )
        page.set_vexpand(True)
        self._editor.append(page)

    def _show_editor(self) -> None:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if session is None or playlist is None:
            self._show_empty()
            return
        self._clear_editor()
        self._editor_id = playlist.id

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._name_entry = Gtk.Entry(text=playlist.name, hexpand=True)
        self._name_entry.add_css_class("title-2")
        title_row.append(self._name_entry)
        rename = Gtk.Button(label="Rename")
        rename.connect("clicked", lambda _button: self._rename(self._name_entry.get_text()))
        title_row.append(rename)
        self._editor.append(title_row)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._play_button = Gtk.Button()
        self._play_button.add_css_class("suggested-action")
        self._play_button.connect("clicked", lambda _button: self._play_now())
        actions.append(self._play_button)
        self._default_button = Gtk.Button()
        self._default_button.connect("clicked", lambda _button: self._set_default())
        actions.append(self._default_button)
        delete = Gtk.Button(label="Delete playlist")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda _button: self._delete())
        actions.append(delete)
        self._editor.append(actions)

        arranger = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        arranger.set_position(390)
        arranger.set_shrink_start_child(False)
        arranger.set_shrink_end_child(False)
        arranger.set_start_child(self._build_source_pane())
        arranger.set_end_child(self._build_playlist_pane())
        arranger.set_vexpand(True)
        self._editor.append(arranger)
        self._sync_editor()

    def _build_source_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pane.set_margin_end(8)
        heading = Gtk.Label(label="Media/Pairings", xalign=0.0)
        heading.add_css_class("title-3")
        pane.append(heading)
        self._source_search = Gtk.SearchEntry(placeholder_text="Search pairings")
        pane.append(self._source_search)
        self._source_flow = self._new_flow()
        self._source_flow.set_filter_func(
            lambda child: self._source_visible(child, self._source_search.get_text())
        )
        self._source_flow.set_sort_func(self._compare_source_cards)
        self._source_search.connect(
            "search-changed", lambda _entry: self._source_flow.invalidate_filter()
        )
        self._source_scroll = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        self._source_scroll.set_child(self._source_flow)
        pane.append(self._source_scroll)
        return pane

    def _build_playlist_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pane.set_margin_start(8)
        heading = Gtk.Label(label="Playlist order", xalign=0.0)
        heading.add_css_class("title-3")
        pane.append(heading)
        note = Gtk.Label(
            label=(
                "Drag pairings here. Use a row's handle or Ctrl+Up/Ctrl+Down to reorder; "
                "duplicates are allowed."
            ),
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        pane.append(note)

        self._order_drop_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self._order_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            valign=Gtk.Align.START,
        )
        self._order_list.add_css_class("boxed-list")
        self._order_list.set_sort_func(self._compare_entry_rows)
        self._order_drop_area.append(self._order_list)
        # Source drags need one more slot than there are rows. This permanent
        # sibling represents "after the last row" without making a fake list
        # row or changing list membership during motion.
        self._order_end_spacer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=160,
        )
        self._order_end_spacer.set_child(Gtk.Box(height_request=24))
        self._order_drop_area.append(self._order_end_spacer)

        target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        # Motion decides both acceptance and the open slot from the payload.
        # Reading it only at drop time creates a circular refusal: GTK will not
        # deliver a drop after motion answered that it accepts no action.
        target.set_preload(True)
        target.connect("drop", self._drop_on_order_list)
        target.connect("motion", self._motion_on_order_list)
        target.connect("leave", self._leave_drop_target)
        self._order_drop_area.add_controller(target)
        self._order_scroll = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        self._order_scroll.set_child(self._order_drop_area)
        pane.append(self._order_scroll)
        return pane

    def _sync_editor(self) -> None:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if session is None or playlist is None:
            self._show_empty()
            return
        if self._editor_id != playlist.id:
            self._show_editor()
            return
        if not self._name_entry.has_focus() and self._name_entry.get_text() != playlist.name:
            self._name_entry.set_text(playlist.name)
        self._play_button.set_label(
            "Playing now" if playlist.id == session.manual_playlist else "Play this playlist now"
        )
        self._play_button.set_sensitive(
            bool(playlist.entries) and playlist.id != session.manual_playlist
        )
        self._default_button.set_label(
            "Schedule default"
            if playlist.id == session.settings.active_playlist
            else "Use as schedule default"
        )
        self._default_button.set_sensitive(playlist.id != session.settings.active_playlist)
        self._sync_source_cards(session)
        self._sync_entry_cards(session, playlist)

    def _sync_source_cards(self, session: Session) -> None:
        incoming = {item.path: item for item in session.library.items}
        for path, card in list(self._source_cards_by_path.items()):
            replacement = incoming.get(path)
            if replacement is None or replacement != card.item:
                self._source_flow.remove(card)
                self._source_cards.pop(card, None)
                del self._source_cards_by_path[path]
        self._source_positions = {
            item.path: index for index, item in enumerate(session.library.items)
        }
        for item in session.library.items:
            if item.path in self._source_cards_by_path:
                continue
            card = _MediaCard(
                item,
                f"{SOURCE_PREFIX}{item.path}",
                self._make_add(item),
                on_drag_started=self._begin_drag,
                on_drag_finished=self._finish_drag,
            )
            self._source_cards[card] = item
            self._source_cards_by_path[item.path] = card
            self._source_flow.append(card)
            self._loader.request(item, card.show_thumbnail)
        self._source_flow.invalidate_sort()
        self._source_flow.invalidate_filter()

    def _sync_entry_cards(self, session: Session, playlist: playlists.Playlist) -> None:
        wanted = {entry.id: session.library.find(Path(entry.source)) for entry in playlist.entries}
        membership_changes = set(wanted) != set(self._entry_rows) or any(
            self._entry_items.get(identifier) != item for identifier, item in wanted.items()
        )
        if membership_changes:
            self._assert_not_dragging()
        for identifier, existing_row in list(self._entry_rows.items()):
            if identifier not in wanted or self._entry_items.get(identifier) != wanted[identifier]:
                self._remove_order_row(existing_row)
                self._entry_items.pop(identifier, None)
                del self._entry_rows[identifier]
        self._entry_positions = {entry.id: index for index, entry in enumerate(playlist.entries)}
        for entry in playlist.entries:
            if entry.id in self._entry_rows:
                continue
            item = wanted[entry.id]
            row = _PlaylistEntryRow(
                entry.id,
                item,
                Path(entry.source),
                self._make_remove(entry.id),
                self._make_reorder_key(entry.id),
                self._begin_drag,
                self._finish_drag,
            )
            if item is not None:
                self._loader.request(item, row.show_thumbnail)
            self._entry_rows[entry.id] = row
            self._entry_items[entry.id] = item
            self._append_order_row(row)
        if not self._dragging:
            self._order_list.invalidate_sort()

    def _compare_source_cards(self, first: Gtk.FlowBoxChild, second: Gtk.FlowBoxChild) -> int:
        first_card = first.get_child()
        second_card = second.get_child()
        first_item = (
            self._source_cards.get(first_card) if isinstance(first_card, _MediaCard) else None
        )
        second_item = (
            self._source_cards.get(second_card) if isinstance(second_card, _MediaCard) else None
        )
        end = len(self._source_positions)
        first_rank = self._source_positions.get(first_item.path, end) if first_item else end
        second_rank = self._source_positions.get(second_item.path, end) if second_item else end
        return first_rank - second_rank

    def _compare_entry_rows(self, first: Gtk.ListBoxRow, second: Gtk.ListBoxRow) -> int:
        end = len(self._entry_positions)
        first_id = first.identifier if isinstance(first, _PlaylistEntryRow) else ""
        second_id = second.identifier if isinstance(second, _PlaylistEntryRow) else ""
        return self._entry_positions.get(first_id, end) - self._entry_positions.get(second_id, end)

    @staticmethod
    def _new_flow() -> Gtk.FlowBox:
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            column_spacing=10,
            row_spacing=10,
            min_children_per_line=1,
            max_children_per_line=3,
            valign=Gtk.Align.START,
        )
        flow.set_margin_top(6)
        flow.set_margin_bottom(6)
        return flow

    def _source_visible(self, child: Gtk.FlowBoxChild, raw: str) -> bool:
        card = child.get_child()
        item = self._source_cards.get(card) if isinstance(card, _MediaCard) else None
        query = raw.strip().casefold()
        return item is None or not query or query in item.name.casefold()

    def _append_order_row(self, row: _PlaylistEntryRow) -> None:
        self._assert_not_dragging()
        self._order_list.append(row)

    def _remove_order_row(self, row: _PlaylistEntryRow) -> None:
        self._assert_not_dragging()
        self._order_list.remove(row)

    def _assert_not_dragging(self) -> None:
        assert not self._dragging, "playlist order list membership changed during a drag"

    def _row_bounds(self) -> tuple[tuple[float, float], ...]:
        list_top = float(self._order_list.get_allocation().y)
        bounds: list[tuple[float, float]] = []
        position = 0
        while (row := self._order_list.get_row_at_index(position)) is not None:
            allocation = row.get_allocation()
            top = list_top + float(allocation.y)
            bounds.append((top, top + float(allocation.height)))
            position += 1
        return tuple(bounds)

    def _slot_anchor(self, slot: int) -> str | None:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if playlist is None or slot >= len(playlist.entries):
            return None
        return playlist.entries[max(slot, 0)].id

    def _drop_on_order_list(
        self, _target: Gtk.DropTarget, value: object, _x: float, y: float
    ) -> bool:
        slot = playlists.drop_slot(self._row_bounds(), y)
        return self._accept_drop(value, self._slot_anchor(slot))

    def _motion_on_order_list(self, target: Gtk.DropTarget, _x: float, y: float) -> Gdk.DragAction:
        slot = playlists.drop_slot(self._row_bounds(), y)
        return self._preview_drop(target.get_value(), self._slot_anchor(slot), slot=slot)

    def _preview_drop(
        self,
        value: object,
        anchor: str | None,
        *,
        after: bool = False,
        slot: int | None = None,
    ) -> Gdk.DragAction:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if playlist is None:
            self._clear_drop_slot()
            return Gdk.DragAction(0)
        if value is None:
            # The payload has not arrived yet. Refusing here would be refusing
            # the whole drag -- GTK never delivers a drop to a target whose
            # motion handler answered zero -- so say yes and let the next motion
            # event, or the drop itself, decide on the real value. This is only
            # reachable if preloading fails; it is a fail-open, not a fallback
            # anybody should rely on.
            return Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        if not isinstance(value, str):
            self._clear_drop_slot()
            return Gdk.DragAction(0)
        if value.startswith(ENTRY_PREFIX):
            action = Gdk.DragAction.MOVE
        elif value.startswith(SOURCE_PREFIX):
            action = Gdk.DragAction.COPY
        else:
            self._clear_drop_slot()
            return Gdk.DragAction(0)
        entry_ids = tuple(entry.id for entry in playlist.entries)
        if slot is None:
            slot = entry_ids.index(anchor) + int(after) if anchor in entry_ids else len(entry_ids)
        self._show_drop_slot(slot)
        return action

    def _show_drop_slot(self, slot: int) -> None:
        slot = min(max(slot, 0), len(self._entry_rows))
        if slot == self._revealed_slot:
            return
        self._clear_drop_slot()
        row = self._order_list.get_row_at_index(slot)
        spacer = row.spacer if isinstance(row, _PlaylistEntryRow) else self._order_end_spacer
        spacer.set_reveal_child(True)
        self._revealed_slot = slot

    def _clear_drop_slot(self) -> None:
        if self._revealed_slot < 0:
            return
        row = self._order_list.get_row_at_index(self._revealed_slot)
        spacer = row.spacer if isinstance(row, _PlaylistEntryRow) else self._order_end_spacer
        spacer.set_reveal_child(False)
        self._revealed_slot = -1

    def _leave_drop_target(self, _target: Gtk.DropTarget) -> None:
        self._clear_drop_slot()

    def _begin_drag(self) -> None:
        self._assert_not_dragging()
        self._dragging = True
        self._playlist_change_after_drag = False

    def _finish_drag(self) -> None:
        if not self._dragging:
            return
        self._clear_drop_slot()
        publish = self._playlist_change_after_drag
        self._playlist_change_after_drag = False
        self._dragging = False
        if publish:
            self._app.playlists_changed()

    def _accept_drop(
        self,
        value: object,
        anchor: str | None,
        *,
        after: bool = False,
    ) -> bool:
        """Apply one row drop, preserving entry ids and allowing duplicates."""
        self._clear_drop_slot()
        if not isinstance(value, str) or not self._selected:
            return False
        session = self._app.session
        playlist = session.playlists.get(self._selected)
        if playlist is None:
            return False
        try:
            if value.startswith(SOURCE_PREFIX):
                source = Path(value.removeprefix(SOURCE_PREFIX))
                if session.library.find(source) is None:
                    return False
                updated = session.playlists.add(self._selected, source)
                moving = updated.entries[-1].id
            elif value.startswith(ENTRY_PREFIX):
                moving = value.removeprefix(ENTRY_PREFIX)
            else:
                return False
            current = session.playlists.find(self._selected)
            position = playlists.drop_position(
                tuple(entry.id for entry in current.entries), moving, anchor, after=after
            )
            session.playlists.move_entry(self._selected, moving, position)
        except playlists.PlaylistError as error:
            self._app.window_report(str(error))
            return False
        if self._dragging:
            # The store write is safe, but its normal synchronous refresh would
            # sort, append, or remove list rows before GTK has ended the drag.
            # Publish only after drag-end has lowered the membership guard.
            self._playlist_change_after_drag = True
        else:
            self._app.playlists_changed()
        return True

    def _create(self, *_arguments: object) -> None:
        session = self._session
        if session is None:
            return
        try:
            made = session.playlists.create(self._new_name.get_text())
        except playlists.PlaylistError as error:
            self._app.window_report(str(error))
            return
        self._selected = made.id
        self._new_name.set_text("")
        self._app.playlists_changed()

    def _rename(self, name: str) -> None:
        session = self._session
        if session is None:
            return
        try:
            session.playlists.rename(self._selected, name)
        except playlists.PlaylistError as error:
            self._app.window_report(str(error))
            return
        self._app.playlists_changed()

    def _delete(self) -> None:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if session is None or playlist is None:
            return
        session.playlists.delete(playlist.id)
        session.schedules.forget_playlist(playlist.id)
        session.displays.forget_playlist(playlist.id)
        if session.settings.active_playlist == playlist.id:
            self._app.update_settings(active_playlist="")
        if session.manual_playlist == playlist.id:
            self._app.resume_schedule()
        self._selected = ""
        self._app.playlists_changed()

    def _set_default(self) -> None:
        if self._selected:
            self._app.update_settings(active_playlist=self._selected)
            self._app.playlists_changed()

    def _play_now(self) -> None:
        if not self._selected:
            return
        response = self._app.activate_playlist(self._selected)
        if not response.ok:
            self._app.window_report(response.message)

    def _make_add(self, item: MediaItem) -> Any:
        def add(_button: Gtk.Button) -> None:
            try:
                self._app.session.playlists.add(self._selected, item.path)
            except playlists.PlaylistError as error:
                self._app.window_report(str(error))
                return
            self._app.playlists_changed()

        return add

    def _make_remove(self, entry_id: str) -> Any:
        def remove(_button: Gtk.Button) -> None:
            try:
                self._app.session.playlists.remove_entry(self._selected, entry_id)
            except playlists.PlaylistError as error:
                self._app.window_report(str(error))
                return
            self._app.playlists_changed()

        return remove

    def _make_reorder_key(self, entry_id: str) -> Any:
        def reorder(
            _controller: Gtk.EventControllerKey,
            keyval: int,
            _code: int,
            state: Gdk.ModifierType,
        ) -> bool:
            if not state & Gdk.ModifierType.CONTROL_MASK:
                return False
            if keyval == Gdk.KEY_Up:
                step = -1
            elif keyval == Gdk.KEY_Down:
                step = 1
            else:
                return False

            playlist = self._app.session.playlists.get(self._selected)
            if playlist is None:
                return True
            entry_ids = tuple(entry.id for entry in playlist.entries)
            if entry_id not in entry_ids:
                return True
            current = entry_ids.index(entry_id)
            position = min(max(current + step, 0), len(entry_ids) - 1)
            if position != current:
                try:
                    self._app.session.playlists.move_entry(self._selected, entry_id, position)
                except playlists.PlaylistError as error:
                    self._app.window_report(str(error))
                    return True
                self._app.playlists_changed()

            # The keyed diff keeps this exact row alive. Reclaiming focus after
            # the synchronous sort makes repeated Ctrl+Arrow presses reliable.
            row = self._entry_rows.get(entry_id)
            if row is not None:
                row.grab_focus()
            return True

        return reorder

    def _make_move(self, entry_id: str, position: int) -> Any:
        def move(_button: Gtk.Button) -> None:
            try:
                self._app.session.playlists.move_entry(self._selected, entry_id, position)
            except playlists.PlaylistError as error:
                self._app.window_report(str(error))
                return
            self._app.playlists_changed()

        return move
