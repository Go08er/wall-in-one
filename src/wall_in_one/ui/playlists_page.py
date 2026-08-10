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
        on_drop: Any | None = None,
        on_remove: Any | None = None,
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
        self.add_controller(drag)

        if on_drop is not None:
            target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
            target.connect("drop", on_drop)
            self.add_controller(target)

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
        self._loader = ThumbnailLoader()
        self._source_cards: dict[_MediaCard, MediaItem] = {}

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

        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._list.add_css_class("boxed-list")
        self._list.connect("row-selected", self._on_selected)
        scroll.set_child(self._list)
        sidebar.append(scroll)
        return sidebar

    def refresh(self, session: Session) -> None:
        self._session = session
        while (row := self._list.get_first_child()) is not None:
            self._list.remove(row)
        self._playlist_rows.clear()
        selected_row: Gtk.ListBoxRow | None = None
        for playlist in session.playlists.all():
            row = Gtk.ListBoxRow()
            self._playlist_rows[row] = playlist.id
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            content.set_margin_top(8)
            content.set_margin_bottom(8)
            content.set_margin_start(10)
            content.set_margin_end(10)
            name = Gtk.Label(label=playlist.name, xalign=0.0)
            name.add_css_class("heading")
            detail = Gtk.Label(
                label=(
                    f"{len(playlist)} item{'s' if len(playlist) != 1 else ''}"
                    + (" · playing" if playlist.id == session.active_playlist() else "")
                    + (" · default" if playlist.id == session.settings.active_playlist else "")
                ),
                xalign=0.0,
            )
            detail.add_css_class("caption")
            detail.add_css_class("dim-label")
            content.append(name)
            content.append(detail)
            row.set_child(content)
            self._list.append(row)
            if playlist.id == self._selected:
                selected_row = row
        if selected_row is None and self._playlist_rows:
            selected_row = next(iter(self._playlist_rows))
        self._list.select_row(selected_row)

    def _on_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        self._selected = self._playlist_rows.get(row, "") if row is not None else ""
        self._show_editor()

    def _clear_editor(self) -> None:
        while (child := self._editor.get_first_child()) is not None:
            self._editor.remove(child)
        self._source_cards.clear()

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

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Entry(text=playlist.name, hexpand=True)
        name.add_css_class("title-2")
        title_row.append(name)
        rename = Gtk.Button(label="Rename")
        rename.connect("clicked", lambda _button: self._rename(name.get_text()))
        title_row.append(rename)
        self._editor.append(title_row)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        play = Gtk.Button(
            label=(
                "Playing now"
                if playlist.id == session.manual_playlist
                else "Play this playlist now"
            )
        )
        play.add_css_class("suggested-action")
        play.set_sensitive(bool(playlist.entries) and playlist.id != session.manual_playlist)
        play.connect("clicked", lambda _button: self._play_now())
        actions.append(play)
        default = Gtk.Button(
            label=(
                "Schedule default"
                if playlist.id == session.settings.active_playlist
                else "Use as schedule default"
            )
        )
        default.set_sensitive(playlist.id != session.settings.active_playlist)
        default.connect("clicked", lambda _button: self._set_default())
        actions.append(default)
        delete = Gtk.Button(label="Delete playlist")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda _button: self._delete())
        actions.append(delete)
        self._editor.append(actions)

        arranger = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        arranger.set_position(390)
        arranger.set_shrink_start_child(False)
        arranger.set_shrink_end_child(False)
        arranger.set_start_child(self._build_source_pane(session))
        arranger.set_end_child(self._build_playlist_pane(session, playlist))
        arranger.set_vexpand(True)
        self._editor.append(arranger)

    def _build_source_pane(self, session: Session) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pane.set_margin_end(8)
        heading = Gtk.Label(label="Media/Pairings", xalign=0.0)
        heading.add_css_class("title-3")
        pane.append(heading)
        search = Gtk.SearchEntry(placeholder_text="Search pairings")
        pane.append(search)
        flow = self._new_flow()
        flow.set_filter_func(lambda child: self._source_visible(child, search.get_text()))
        search.connect("search-changed", lambda _entry: flow.invalidate_filter())
        for item in session.library.items:
            card = _MediaCard(
                item,
                f"{SOURCE_PREFIX}{item.path}",
                self._make_add(item),
            )
            self._source_cards[card] = item
            flow.append(card)
            self._loader.request(item, card.show_thumbnail)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(flow)
        pane.append(scroll)
        return pane

    def _build_playlist_pane(self, session: Session, playlist: playlists.Playlist) -> Gtk.Widget:
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
        flow = self._new_flow()
        for entry in playlist.entries:
            item = session.library.find(Path(entry.source))
            if item is None:
                missing = Gtk.Label(label=f"Missing · {Path(entry.source).name}")
                missing.add_css_class("card")
                flow.append(missing)
                continue
            card = _MediaCard(
                item,
                f"{ENTRY_PREFIX}{entry.id}",
                self._make_remove(entry.id),
                self._make_drop(entry.id),
                self._make_remove(entry.id),
            )
            flow.append(card)
            self._loader.request(item, card.show_thumbnail)
        target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.connect("drop", self._drop_at_end)
        flow.add_controller(target)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(flow)
        pane.append(scroll)
        return pane

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

    def _make_drop(self, before: str) -> Any:
        def drop(_target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
            return self._accept_drop(value, before)

        return drop

    def _drop_at_end(self, _target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        return self._accept_drop(value, None)

    def _accept_drop(self, value: object, before: str | None) -> bool:
        """Apply one card drop, preserving entry ids and allowing duplicates."""
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
                tuple(entry.id for entry in current.entries), moving, before
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
