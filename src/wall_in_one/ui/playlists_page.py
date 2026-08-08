"""Full-page editor for named, ordered wallpaper rotations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gtk, Pango

from wall_in_one.library import playlists
from wall_in_one.library.model import MediaItem

if TYPE_CHECKING:
    from wall_in_one.session import Session
    from wall_in_one.ui.app import Application


class PlaylistsPage(Gtk.Box):
    """Create playlists and arrange their stable entries without a terminal."""

    def __init__(self, application: Application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._app = application
        self._session: Session | None = None
        self._selected = ""
        self._playlist_rows: dict[Gtk.ListBoxRow, str] = {}
        self._media_rows: dict[Gtk.ListBoxRow, MediaItem] = {}

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
        self._media_rows.clear()

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
        default = Gtk.Button(
            label=(
                "Default rotation"
                if playlist.id == session.settings.active_playlist
                else "Use as default rotation"
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

        entries = Adw.PreferencesGroup(
            title="Rotation order",
            description=(
                "Entry IDs stay stable when you reorder them. Missing files remain listed so "
                "removable drives can come back."
            ),
        )
        if not playlist.entries:
            entries.add(Adw.ActionRow(title="This playlist is empty"))
        for index, entry in enumerate(playlist.entries):
            item = session.library.find(Path(entry.source))
            row = Adw.ActionRow(
                title=item.name if item is not None else Path(entry.source).name,
                subtitle=(
                    f"{index + 1} · {item.kind.value if item is not None else 'missing'} · "
                    f"entry {entry.id}"
                ),
            )
            row.add_prefix(
                Gtk.Image(
                    icon_name=(
                        "video-x-generic-symbolic"
                        if item is not None and item.kind.moves
                        else "image-x-generic-symbolic"
                    )
                )
            )
            up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Move earlier")
            up.set_sensitive(index > 0)
            up.add_css_class("flat")
            up.connect("clicked", self._make_move(entry.id, index - 1))
            row.add_suffix(up)
            down = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Move later")
            down.set_sensitive(index + 1 < len(playlist.entries))
            down.add_css_class("flat")
            down.connect("clicked", self._make_move(entry.id, index + 1))
            row.add_suffix(down)
            remove = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove entry")
            remove.add_css_class("flat")
            remove.connect("clicked", self._make_remove(entry.id))
            row.add_suffix(remove)
            entries.add(row)
        self._editor.append(entries)

        picker = Adw.PreferencesGroup(
            title="Add from Media",
            description="Search the same library shown on the Media page, then add one item.",
        )
        search = Gtk.SearchEntry(placeholder_text="Find media")
        picker.add(search)
        scroller = Gtk.ScrolledWindow(
            min_content_height=220,
            max_content_height=380,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listing.add_css_class("boxed-list")
        listing.set_filter_func(lambda row: self._filter_media(row, search.get_text()))
        search.connect("search-changed", lambda _entry: listing.invalidate_filter())
        for item in session.library.items:
            media_row = Gtk.ListBoxRow(activatable=False)
            self._media_rows[media_row] = item
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            content.set_margin_top(7)
            content.set_margin_bottom(7)
            content.set_margin_start(10)
            content.set_margin_end(10)
            icon = Gtk.Image(
                icon_name=(
                    "video-x-generic-symbolic" if item.kind.moves else "image-x-generic-symbolic"
                )
            )
            content.append(icon)
            label = Gtk.Label(
                label=item.name,
                xalign=0.0,
                hexpand=True,
                ellipsize=Pango.EllipsizeMode.END,
            )
            label.set_tooltip_text(str(item.path))
            content.append(label)
            add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add to playlist")
            add.connect("clicked", self._make_add(item))
            content.append(add)
            media_row.set_child(content)
            listing.append(media_row)
        scroller.set_child(listing)
        picker.add(scroller)
        self._editor.append(picker)

    def _filter_media(self, row: Gtk.ListBoxRow, raw: str) -> bool:
        item = self._media_rows.get(row)
        query = raw.strip().casefold()
        return item is None or not query or query in item.name.casefold()

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
        self._selected = ""
        self._app.playlists_changed()

    def _set_default(self) -> None:
        if self._selected:
            self._app.update_settings(active_playlist=self._selected)
            self._app.playlists_changed()

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
