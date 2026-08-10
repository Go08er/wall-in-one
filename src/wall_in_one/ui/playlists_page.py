"""Full-page editor for named, ordered wallpaper rotations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

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
        on_drop: Any | None = None,
        on_remove: Any | None = None,
        *,
        on_motion: Any | None = None,
        on_leave: Any | None = None,
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
            icon_name="list-add-symbolic" if on_remove is None else "list-remove-symbolic",
            tooltip_text="Add to playlist" if on_remove is None else "Remove from playlist",
        )
        add.add_css_class("flat")
        add.connect("clicked", on_activate if on_remove is None else on_remove)
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
        self._on_drag_finished = on_drag_finished
        self.add_controller(drag)

        if on_drop is not None:
            target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
            # Without this the payload is only read once a drop has been
            # accepted, so `get_value` is None for every motion event -- and a
            # motion handler that cannot see the payload refuses the drag, which
            # stops the drop from ever being delivered. The payload here is a
            # short string, so reading it early costs nothing.
            target.set_preload(True)
            target.connect("drop", on_drop)
            if on_motion is not None:
                target.connect("motion", on_motion)
            if on_leave is not None:
                target.connect("leave", on_leave)
            self.add_controller(target)

    def _drag_begin(self, source: Gtk.DragSource, _drag: Gdk.Drag) -> None:
        """Keep the gesture visually tied to the image the person grabbed."""
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
        self._entry_cards: dict[str, Gtk.Widget] = {}
        self._entry_items: dict[str, MediaItem | None] = {}
        self._entry_ids: dict[Gtk.Widget, str] = {}
        self._entry_positions: dict[str, int] = {}
        self._drop_gap: Gtk.Revealer | None = None
        self._gap_position = -1
        self._gap_display_position = -1
        self._gap_moving = ""
        self._gap_display_positions: dict[Gtk.Revealer, int] = {}
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
        self._clear_drop_gap(animate=False)
        while (child := self._editor.get_first_child()) is not None:
            self._editor.remove(child)
        self._source_cards.clear()
        self._source_cards_by_path.clear()
        self._source_positions.clear()
        self._entry_cards.clear()
        self._entry_items.clear()
        self._entry_ids.clear()
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
            label="Drag pairings here. Drag cards to reorder; duplicates are allowed.",
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        pane.append(note)
        # A transient narrow placeholder needs to keep its own width; making
        # every child homogeneous would turn it into a full card-sized hole.
        self._order_flow = self._new_flow(homogeneous=False)
        self._order_flow.set_sort_func(self._compare_entry_cards)
        target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        # See `_MediaCard`: motion cannot read the payload without this.
        target.set_preload(True)
        target.connect("drop", self._drop_at_end)
        target.connect("motion", self._motion_at_end)
        target.connect("leave", self._leave_drop_target)
        self._order_flow.add_controller(target)
        self._order_scroll = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        self._order_scroll.set_child(self._order_flow)
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
                on_drag_finished=self._finish_card_drag,
            )
            self._source_cards[card] = item
            self._source_cards_by_path[item.path] = card
            self._source_flow.append(card)
            self._loader.request(item, card.show_thumbnail)
        self._source_flow.invalidate_sort()
        self._source_flow.invalidate_filter()

    def _sync_entry_cards(self, session: Session, playlist: playlists.Playlist) -> None:
        wanted = {entry.id: session.library.find(Path(entry.source)) for entry in playlist.entries}
        for identifier, existing_card in list(self._entry_cards.items()):
            if identifier not in wanted or self._entry_items.get(identifier) != wanted[identifier]:
                self._order_flow.remove(existing_card)
                self._entry_ids.pop(existing_card, None)
                self._entry_items.pop(identifier, None)
                del self._entry_cards[identifier]
        self._entry_positions = {entry.id: index for index, entry in enumerate(playlist.entries)}
        for entry in playlist.entries:
            if entry.id in self._entry_cards:
                continue
            item = wanted[entry.id]
            if item is None:
                card: Gtk.Widget = Gtk.Label(label=f"Missing · {Path(entry.source).name}")
                card.add_css_class("card")
            else:
                card = _MediaCard(
                    item,
                    f"{ENTRY_PREFIX}{entry.id}",
                    self._make_remove(entry.id),
                    self._make_drop(entry.id),
                    self._make_remove(entry.id),
                    on_motion=self._make_motion(entry.id),
                    on_leave=self._leave_drop_target,
                    on_drag_finished=self._finish_card_drag,
                )
                self._loader.request(item, card.show_thumbnail)
            self._entry_cards[entry.id] = card
            self._entry_items[entry.id] = item
            self._entry_ids[card] = entry.id
            self._order_flow.append(card)
        self._order_flow.invalidate_sort()

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

    def _compare_entry_cards(self, first: Gtk.FlowBoxChild, second: Gtk.FlowBoxChild) -> int:
        first_card = first.get_child()
        second_card = second.get_child()
        if isinstance(first_card, Gtk.Revealer) and first_card in self._gap_display_positions:
            first_rank = self._gap_display_positions[first_card] * 2 - 1
        else:
            first_id = self._entry_ids.get(first_card, "") if first_card is not None else ""
            first_rank = self._entry_positions.get(first_id, len(self._entry_positions)) * 2
        if isinstance(second_card, Gtk.Revealer) and second_card in self._gap_display_positions:
            second_rank = self._gap_display_positions[second_card] * 2 - 1
        else:
            second_id = self._entry_ids.get(second_card, "") if second_card is not None else ""
            second_rank = self._entry_positions.get(second_id, len(self._entry_positions)) * 2
        return first_rank - second_rank

    @staticmethod
    def _new_flow(*, homogeneous: bool = True) -> Gtk.FlowBox:
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=homogeneous,
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

    def _make_drop(self, anchor: str) -> Any:
        def drop(target: Gtk.DropTarget, value: object, x: float, _y: float) -> bool:
            widget = target.get_widget()
            after = widget is not None and x >= widget.get_width() / 2
            return self._accept_drop(value, anchor, after=after)

        return drop

    def _drop_at_end(self, _target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        return self._accept_drop(value, None)

    def _make_motion(self, anchor: str) -> Any:
        def motion(target: Gtk.DropTarget, x: float, _y: float) -> Gdk.DragAction:
            widget = target.get_widget()
            after = widget is not None and x >= widget.get_width() / 2
            return self._preview_drop(target.get_value(), anchor, after=after)

        return motion

    def _motion_at_end(self, target: Gtk.DropTarget, _x: float, _y: float) -> Gdk.DragAction:
        return self._preview_drop(target.get_value(), None)

    def _preview_drop(
        self, value: object, anchor: str | None, *, after: bool = False
    ) -> Gdk.DragAction:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if playlist is None:
            self._clear_drop_gap()
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
            self._clear_drop_gap()
            return Gdk.DragAction(0)
        if value.startswith(ENTRY_PREFIX):
            moving = value.removeprefix(ENTRY_PREFIX)
            action = Gdk.DragAction.MOVE
        elif value.startswith(SOURCE_PREFIX):
            moving = ""
            action = Gdk.DragAction.COPY
        else:
            self._clear_drop_gap()
            return Gdk.DragAction(0)
        if anchor == moving:
            self._clear_drop_gap()
            return action
        entry_ids = tuple(entry.id for entry in playlist.entries)
        position = playlists.drop_position(entry_ids, moving, anchor, after=after)
        self._show_drop_gap(position, moving)
        return action

    def _show_drop_gap(self, position: int, moving: str) -> None:
        if (
            self._drop_gap is not None
            and position == self._gap_position
            and moving == self._gap_moving
        ):
            return
        self._clear_drop_gap()
        original = self._entry_positions.get(moving)
        display_position = position + int(original is not None and original < position)
        gap = Gtk.Revealer()
        gap.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        gap.set_transition_duration(160)
        spacer = Gtk.Box(width_request=32, height_request=138)
        gap.set_child(spacer)
        gap.connect("notify::child-revealed", self._gap_reveal_changed)
        target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.connect("drop", self._make_gap_drop(position))
        target.connect("motion", self._motion_over_gap)
        target.connect("leave", self._leave_drop_target)
        gap.add_controller(target)
        self._drop_gap = gap
        self._gap_position = position
        self._gap_display_position = display_position
        self._gap_moving = moving
        self._gap_display_positions[gap] = display_position
        self._order_flow.append(gap)
        self._order_flow.invalidate_sort()
        gap.set_reveal_child(True)

    def _clear_drop_gap(self, *_arguments: object, animate: bool = True) -> None:
        gap = self._drop_gap
        if gap is None:
            return
        self._drop_gap = None
        self._gap_position = -1
        self._gap_display_position = -1
        self._gap_moving = ""
        gap.set_reveal_child(False)
        if not animate or not gap.get_child_revealed():
            self._remove_gap(gap)
            return
        # The signal normally removes it after the slide closes. The timeout
        # is a belt-and-braces cleanup for an interrupted or unmapped widget.
        GLib.timeout_add(gap.get_transition_duration() + 50, self._remove_gap, gap)

    def _gap_reveal_changed(self, gap: Gtk.Revealer, _parameter: object) -> None:
        if not gap.get_reveal_child() and not gap.get_child_revealed():
            self._remove_gap(gap)

    def _remove_gap(self, gap: Gtk.Revealer) -> bool:
        wrapper = gap.get_parent()
        flow = wrapper.get_parent() if isinstance(wrapper, Gtk.FlowBoxChild) else None
        if isinstance(flow, Gtk.FlowBox):
            flow.remove(gap)
        self._gap_display_positions.pop(gap, None)
        return False

    def _leave_drop_target(self, _target: Gtk.DropTarget) -> None:
        self._clear_drop_gap()

    def _finish_card_drag(self) -> None:
        self._clear_drop_gap(animate=False)

    def _make_gap_drop(self, position: int) -> Any:
        def drop(_target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
            return self._accept_drop(value, None, position=position)

        return drop

    def _motion_over_gap(self, target: Gtk.DropTarget, _x: float, _y: float) -> Gdk.DragAction:
        value = target.get_value()
        if isinstance(value, str) and value.startswith(ENTRY_PREFIX):
            return Gdk.DragAction.MOVE
        if isinstance(value, str) and value.startswith(SOURCE_PREFIX):
            return Gdk.DragAction.COPY
        return Gdk.DragAction(0)

    def _accept_drop(
        self,
        value: object,
        anchor: str | None,
        *,
        after: bool = False,
        position: int | None = None,
    ) -> bool:
        """Apply one card drop, preserving entry ids and allowing duplicates."""
        self._clear_drop_gap(animate=False)
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
            if position is None:
                current = session.playlists.find(self._selected)
                position = playlists.drop_position(
                    tuple(entry.id for entry in current.entries), moving, anchor, after=after
                )
            session.playlists.move_entry(self._selected, moving, position)
        except playlists.PlaylistError as error:
            self._app.window_report(str(error))
            return False
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

    def _make_move(self, entry_id: str, position: int) -> Any:
        def move(_button: Gtk.Button) -> None:
            try:
                self._app.session.playlists.move_entry(self._selected, entry_id, position)
            except playlists.PlaylistError as error:
                self._app.window_report(str(error))
                return
            self._app.playlists_changed()

        return move
