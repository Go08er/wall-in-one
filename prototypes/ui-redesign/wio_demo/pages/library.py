"""Library: every wallpaper on this computer, with details beside the grid."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .. import art, thumbs, ui
from ..catalog import KIND_LABEL
from ..models import Wallpaper
from . import Page
from .inspector import Inspector

SORTS = [("added", "Recently added"), ("name", "Name"), ("kind", "Type"), ("color", "Color")]
#: Cards are built a page at a time, like the real app's grid (MEDIA_PAGE_SIZE in
#: ui/grid.py), so a library of thousands builds 72 cards, not thousands.
PAGE_SIZE = 72


class LibraryPage(Page):
    name = "library"
    title = "Library"

    def __init__(self, state) -> None:
        super().__init__(state)
        self._kind = "all"
        self._favorites = False
        self._query = ""
        self._sort = "added"
        self._card_width = 208
        self._select_mode = False
        self._selected: set[str] = set()
        self._cards: dict[str, ui.WallpaperCard] = {}  # only the wallpapers with a card now
        self._inspected: str | None = None
        self._limit = PAGE_SIZE  # how many of the matching wallpapers get a card
        self._matching = 0

        # -- header ------------------------------------------------------------
        self.search = Gtk.SearchEntry(placeholder_text="Search wallpapers, tags, folders")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search)
        clamp = Adw.Clamp(maximum_size=460, child=self.search)
        self._title_box = clamp

        self._add = Gtk.Button(icon_name="list-add-symbolic")
        self._add.set_tooltip_text("Add a folder")
        self._add.update_property([Gtk.AccessibleProperty.LABEL], ["Add a folder"])
        self._add.connect("clicked", lambda *_: self._choose_folder())
        self._select = ui.select_toggle()
        self._select.connect("toggled", self._on_select_mode)

        # -- filter bar ------------------------------------------------------------
        # The right end stays free for the Select pill hanging over the grid's corner.
        filters = Gtk.Box(spacing=8, margin_start=18, margin_end=ui.CORNER_RESERVE, margin_top=10, margin_bottom=10)
        self._kinds = Adw.ToggleGroup(can_shrink=False)  # the row scrolls instead of eliding "Images"
        for key, label in (("all", "All"), ("still", "Images"), ("video", "Videos"), ("scene", "Scenes")):
            self._kinds.add(Adw.Toggle(name=key, label=label))
        self._kinds.set_active_name("all")
        self._kinds.connect("notify::active-name", self._on_kind)
        filters.append(self._kinds)
        self._fav_toggle = Gtk.ToggleButton()
        self._fav_content = Adw.ButtonContent(icon_name="starred-symbolic", label="Favorites")
        self._fav_toggle.set_child(self._fav_content)
        self._fav_toggle.set_tooltip_text("Favorites only")
        self._fav_toggle.add_css_class("chip")
        self._fav_toggle.connect("toggled", self._on_fav)
        filters.append(self._fav_toggle)
        filters.append(Gtk.Box(hexpand=True))
        self._count = Gtk.Label()
        self._count.add_css_class("dimmed")
        self._count.add_css_class("numeric")
        filters.append(self._count)
        sort_menu = Gio.Menu()
        for key, label in SORTS:
            sort_menu.append(label, f"lib.sort::{key}")
        size_section = Gio.Menu()
        size_section.append("Large thumbnails", "lib.size::large")
        size_section.append("Small thumbnails", "lib.size::small")
        sort_menu.append_section(None, size_section)
        self._sort_button = Gtk.MenuButton(menu_model=sort_menu)
        self._sort_content = Adw.ButtonContent(icon_name="view-sort-descending-symbolic", label="Recently added")
        self._sort_button.set_child(self._sort_content)
        self._narrow = False
        self._sort_button.add_css_class("flat")
        self._sort_button.set_tooltip_text("Sort and size")
        filters.append(self._sort_button)

        # -- problems notice ---------------------------------------------------------
        self._notice = Adw.Banner()
        self._notice.set_button_label("Review")
        self._notice.connect("button-clicked", self._review_problem)

        # -- grid ------------------------------------------------------------------
        # Equal columns: FlowBox hands some columns an extra pixel, which upsets
        # height-for-width cards. ui.CardGrid gives every card its measured width.
        self.flow = ui.CardGrid(
            min_width=self._card_width,
            max_columns=10,
            valign=Gtk.Align.START,
            margin_start=14,
            margin_end=14,
            margin_top=20,  # clears the lower half of the hanging Select pill
            margin_bottom=18,
        )
        # Only the first pages of the filtered, sorted library are cards; "Show
        # more" adds a page. Filtering, sorting and counting see every wallpaper.
        self._more = Gtk.Button(halign=Gtk.Align.CENTER, margin_bottom=18, visible=False)
        self._more.add_css_class("pill")
        self._more.set_tooltip_text("Show another page of wallpapers")
        self._more.connect("clicked", lambda *_: self._show_more())
        pages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.START)
        pages.append(self.flow)
        pages.append(self._more)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(pages)
        self._empty = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="No wallpapers match",
            description="Try another word, or clear the filters.",
        )
        clear = Gtk.Button(label="Clear filters")
        clear.add_css_class("pill")
        clear.connect("clicked", lambda *_: self._clear_filters())
        store = Gtk.Button(label="Browse the Store")
        store.add_css_class("pill")
        store.connect("clicked", lambda *_: self.state.navigate("store"))
        # Wraps under each other in narrow windows (the stack measures this page too).
        empty_buttons = Adw.WrapBox(
            child_spacing=8, line_spacing=8, halign=Gtk.Align.CENTER, justify=Adw.JustifyMode.NONE
        )
        empty_buttons.append(clear)
        empty_buttons.append(store)
        self._empty.set_child(empty_buttons)
        self._grid_stack = Gtk.Stack()
        self._grid_stack.add_named(scroller, "grid")
        self._grid_stack.add_named(self._empty, "empty")

        # -- selection bar -----------------------------------------------------------
        # Labeled buttons with icons; narrow windows keep just the icons (_set_narrow).
        self._action_bar = Gtk.ActionBar(revealed=False)
        self._bar_labels: list[tuple[Adw.ButtonContent, str]] = []

        def bar_button(icon: str, label: str, tooltip: str, button: Gtk.Widget | None = None) -> Gtk.Widget:
            content = Adw.ButtonContent(icon_name=icon, label=label)
            button = button or Gtk.Button()
            button.set_child(content)
            button.set_tooltip_text(tooltip)
            self._bar_labels.append((content, label))
            return button

        select_all = bar_button("edit-select-all-symbolic", "Select all", "Select every wallpaper shown")
        select_all.add_css_class("flat")
        select_all.connect("clicked", lambda *_: self.select_all())
        self._action_bar.pack_start(select_all)
        self._selection_label = Gtk.Label()
        self._action_bar.pack_start(self._selection_label)
        add_to = bar_button(
            "list-add-symbolic",
            "Add to playlist",
            "Add the selected wallpapers to a playlist",
            Gtk.MenuButton(menu_model=self._bulk_playlist_menu(), always_show_arrow=True),
        )
        add_to.add_css_class("suggested-action")
        fav_all = bar_button("starred-symbolic", "Favorite", "Add the selected wallpapers to favorites")
        fav_all.connect("clicked", lambda *_: self._bulk_favorite())
        remove_all = bar_button("user-trash-symbolic", "Remove…", "Remove the selected wallpapers from the library")
        remove_all.add_css_class("destructive-action")
        remove_all.connect("clicked", lambda *_: self._confirm_remove_selected())
        self._remove_all = remove_all
        self._action_bar.pack_end(add_to)
        self._action_bar.pack_end(fav_all)
        self._action_bar.pack_end(remove_all)

        # The chip row scrolls sideways on narrow windows instead of forcing width.
        filter_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
        )
        filter_scroller.set_child(filters)
        self._selection_label.set_ellipsize(3)

        # Banners sit right under the header, as in libadwaita; the Select pill
        # then hangs between the filter row and the grid without covering anything.
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        listing.append(filter_scroller)
        listing.append(self._grid_stack)
        hanging = Gtk.Overlay(child=listing)
        ui.hang_on_corner(hanging, self._grid_stack, self._select)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self._notice)
        content.append(hanging)
        content.append(self._action_bar)

        self.inspector = Inspector(state, self.close_inspector)
        self.split = Adw.OverlaySplitView(
            sidebar_position=Gtk.PackType.END,
            show_sidebar=False,
            min_sidebar_width=340,
            max_sidebar_width=400,
            sidebar_width_fraction=0.34,
        )
        self.split.set_content(content)
        self.split.set_sidebar(self.inspector)
        breakpoint_bin = Adw.BreakpointBin(width_request=360, height_request=300)
        breakpoint_bin.set_child(self.split)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 860sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self._count, "visible", False)
        narrow.connect("apply", lambda *_: self._set_narrow(True))
        narrow.connect("unapply", lambda *_: self._set_narrow(False))
        breakpoint_bin.add_breakpoint(narrow)
        # Collapsing hides the overlay sidebar; keep open details open (as an overlay).
        self.split.connect("notify::collapsed", self._on_collapsed)
        self.widget = breakpoint_bin
        self._install_actions()

        state.connect("changed", self._on_changed)
        self._rebuild()

    # -- Page API ------------------------------------------------------------------
    def title_widget(self) -> Gtk.Widget:
        return self._title_box

    def header_start(self) -> list[Gtk.Widget]:
        return [self._add]

    def header_end(self) -> list[Gtk.Widget]:
        return []

    def focus_search(self) -> bool:
        self.search.grab_focus()
        return True

    def activate(self, argument: str | None) -> None:
        if argument and self.state.has_wallpaper(argument):
            self.inspect(self.state.wallpaper(argument))

    def demo(self, scene: str) -> None:
        # Start every scene from a clean page.
        self._select.set_active(False)
        self.close_inspector()
        self._clear_filters()
        for card in self._cards.values():
            card.remove_css_class("force-hover")
        what, _, arg = scene.partition(":")
        if what == "inspector":
            wid, _, section = arg.partition("/")
            self.inspect(self.state.wallpaper(wid))
            if section:
                self.inspector.scroll_to(section)
        elif what == "select":
            self._select.set_active(True)
            for wid in ("lily-pond", "golden-coast", "misty-pines"):
                self._toggle_selected(wid)
        elif what == "hover":
            self._cards[arg].add_css_class("force-hover")
        elif what == "menu":
            card = self._cards[arg]
            GLib.timeout_add(300, lambda: (card._popup(None, 1, 120, 60, card._menu), False)[1])
        elif what == "filter":
            self._kinds.set_active_name(arg)
        elif what == "search":
            self.search.set_text(arg)
        elif what == "favorites":
            self._fav_toggle.set_active(True)
        elif what == "small":
            self._set_size("small")

    # -- actions -------------------------------------------------------------------
    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()

        def add(name: str, callback, parameter: str | None = "s") -> None:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(parameter) if parameter else None)
            action.connect("activate", lambda _a, value: callback(value.get_string() if value else None))
            group.add_action(action)

        def apply(value: str) -> None:
            wid, scope = value.split("|")
            self.state.apply(wid, scope)

        def add_to(value: str) -> None:
            wid, pid = value.split("|")
            self.state.add_to_playlist(pid, [wid])

        add("apply", apply)
        add("add", add_to)
        add("fav", lambda wid: self.state.toggle_favorite(wid))
        add("edit", lambda wid: self.inspect(self.state.wallpaper(wid)))
        add("files", lambda wid: self.state.toast("Would open the folder in Files"))
        add(
            "remove",
            lambda wid: (
                self.inspect(self.state.wallpaper(wid)),
                self.inspector._confirm_remove(self.state.wallpaper(wid)),
            ),
        )
        add("sort", self._set_sort)
        add("size", self._set_size)
        add("bulk-add", self._bulk_add)
        add("store", lambda _v: self.state.navigate("store"), None)
        # The inspector lives inside this page; header widgets live in the shell.
        self.widget.insert_action_group("lib", group)

    def _card_menu(self, wallpaper: Wallpaper) -> Gtk.PopoverMenu:
        menu = Gio.Menu()
        apply = Gio.Menu()
        apply.append("Apply to all displays", f"lib.apply::{wallpaper.id}|all")
        for display in self.state.displays:
            apply.append(f"Apply to {display.connector} only", f"lib.apply::{wallpaper.id}|{display.connector}")
        menu.append_section(None, apply)
        organize = Gio.Menu()
        organize.append("Edit details", f"lib.edit::{wallpaper.id}")
        playlists = Gio.Menu()
        for playlist in self.state.playlists:
            if not playlist.automatic:
                playlists.append(playlist.name, f"lib.add::{wallpaper.id}|{playlist.id}")
        organize.append_submenu("Add to playlist", playlists)
        organize.append(
            "Remove from favorites" if wallpaper.favorite else "Add to favorites", f"lib.fav::{wallpaper.id}"
        )
        menu.append_section(None, organize)
        files = Gio.Menu()
        files.append("Show in Files", f"lib.files::{wallpaper.id}")
        files.append("Remove from library…", f"lib.remove::{wallpaper.id}")
        menu.append_section(None, files)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        return popover

    def _bulk_playlist_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        for playlist in self.state.playlists:
            if not playlist.automatic:
                menu.append(playlist.name, f"lib.bulk-add::{playlist.id}")
        return menu

    # -- building ----------------------------------------------------------------------
    def _rebuild(self) -> None:
        """The state changed: drop every card and build the current pages again
        (how many pages are shown survives, as in the real app)."""
        for card in self._cards.values():
            if card._menu is not None:
                card._menu.unparent()
        self.flow.remove_all()
        self._cards = {}
        self._materialize()
        problems = [w for w in self.state.wallpapers if w.problem]
        self._notice.set_title(
            f"{len(problems)} wallpaper is being skipped after a playback problem"
            if len(problems) == 1
            else f"{len(problems)} wallpapers are being skipped after playback problems"
        )
        self._notice.set_revealed(bool(problems))

    def _matches(self) -> list[Wallpaper]:
        """Every wallpaper that passes the filters, in the chosen order."""
        order = {wallpaper.id: index for index, wallpaper in enumerate(self.state.wallpapers)}
        matching = [wallpaper for wallpaper in self.state.wallpapers if self._visible(wallpaper)]
        return sorted(matching, key=lambda wallpaper: self._sort_key(wallpaper, order))

    def _materialize(self) -> None:
        """Give cards to the first ``self._limit`` matches, plus the wallpapers on
        screen and the one whose details are open wherever they sort (the real
        app keeps current tiles past page one too). Cards that still fit are kept."""
        matches = self._matches()
        keep = set(self.state.current.values()) | {self._inspected}
        wanted = [w for index, w in enumerate(matches) if index < self._limit or w.id in keep]
        wanted_ids = {wallpaper.id for wallpaper in wanted}
        for wid in [wid for wid in self._cards if wid not in wanted_ids]:
            card = self._cards.pop(wid)
            if card._menu is not None:
                card._menu.unparent()
        self.flow.remove_all()
        for wallpaper in wanted:
            card = self._cards.get(wallpaper.id) or self._card(wallpaper)
            self._cards[wallpaper.id] = card
            self.flow.append(card)
        self._matching = len(matches)
        self._update_count()

    def _card(self, wallpaper: Wallpaper) -> ui.WallpaperCard:
        playing = [connector for connector, wid in self.state.current.items() if wid == wallpaper.id]
        card = ui.WallpaperCard(
            wallpaper,
            width=self._card_width,
            playing_on=playing or None,
            on_open=self._on_card,
            on_apply=lambda w: self.state.apply(w.id, self.state.scope),
            apply_tooltip=self._apply_target,
            on_favorite=lambda w: self.state.toggle_favorite(w.id),
            menu=self._card_menu(wallpaper),
            **ui.card_colors(self.state, wallpaper),
        )
        card.set_selected(wallpaper.id == self._inspected)
        if self._select_mode:
            card.set_selectable(True)
            card.set_checked(wallpaper.id in self._selected)
        self._attach_drag(card, wallpaper)
        return card

    def _show_more(self) -> None:
        """Add a page of cards without rebuilding the ones already shown."""
        self._limit += PAGE_SIZE
        self._materialize()

    def _refilter(self) -> None:
        """A new search, filter or order starts again from one page."""
        self._limit = PAGE_SIZE
        self._materialize()

    def _attach_drag(self, card: ui.WallpaperCard, wallpaper: Wallpaper) -> None:
        source = Gtk.DragSource(actions=Gdk.DragAction.COPY)

        def prepare(_source, _x, _y):
            value = GObject.Value(GObject.TYPE_STRING, wallpaper.id)
            return Gdk.ContentProvider.new_for_value(value)

        def begin(drag_source, _drag) -> None:
            drag_source.set_icon(thumbs.texture(wallpaper, 160, 90), 80, 45)

        source.connect("prepare", prepare)
        source.connect("drag-begin", begin)
        card.frame.add_controller(source)

    def _visible(self, wallpaper: Wallpaper) -> bool:
        if self._kind != "all" and wallpaper.kind != self._kind:
            return False
        if self._favorites and not wallpaper.favorite:
            return False
        if self._query:
            haystack = " ".join(
                (
                    wallpaper.name,
                    wallpaper.folder,
                    wallpaper.source,
                    " ".join(wallpaper.tags),
                    wallpaper.style,
                    KIND_LABEL[wallpaper.kind],
                )
            )
            return all(word in haystack.lower() for word in self._query.lower().split())
        return True

    def _sort_key(self, wallpaper: Wallpaper, order: dict[str, int]):
        if self._sort == "name":
            return wallpaper.name.lower()
        if self._sort == "kind":
            return (wallpaper.kind, wallpaper.name.lower())
        if self._sort == "color":
            return art.look_for(*wallpaper.key).hue
        return order[wallpaper.id]

    def _update_count(self) -> None:
        shown = self._matching
        total = len(self.state.wallpapers)
        self._count.set_label(f"{shown} of {total}" if shown != total else f"{total} wallpapers")
        self._grid_stack.set_visible_child_name("grid" if shown else "empty")
        remaining = shown - len(self._cards)
        self._more.set_visible(remaining > 0)
        if remaining > 0:
            self._more.set_label(f"Show {min(PAGE_SIZE, remaining)} more · {len(self._cards)} of {shown} shown")

    # -- callbacks ------------------------------------------------------------------------
    def _on_changed(self, _state, topic: str) -> None:
        if topic in ("library", "now", "playlists"):
            self._rebuild()

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._query = entry.get_text().strip()
        self._refilter()

    def _on_kind(self, group: Adw.ToggleGroup, _param) -> None:
        self._kind = group.get_active_name()
        self._refilter()

    def _on_fav(self, button: Gtk.ToggleButton) -> None:
        self._favorites = button.get_active()
        self._refilter()

    def _clear_filters(self) -> None:
        self.search.set_text("")
        self._kinds.set_active_name("all")
        self._fav_toggle.set_active(False)

    def _set_sort(self, key: str) -> None:
        self._sort = key
        self._sort_content.set_label("" if self._narrow else dict(SORTS)[key])
        self._sort_button.set_tooltip_text(f"Sorted by {dict(SORTS)[key].lower()} · sort and size")
        self._refilter()

    def _set_narrow(self, narrow: bool) -> None:
        """Narrow windows: Favorites and Sort keep their icons, lose their words."""
        self._narrow = narrow
        self._fav_content.set_label("" if narrow else "Favorites")
        self._sort_content.set_label("" if narrow else dict(SORTS)[self._sort])
        for content, label in self._bar_labels:
            content.set_label("" if narrow else label)

    def _on_collapsed(self, split: Adw.OverlaySplitView, _param) -> None:
        if split.get_collapsed() and self._inspected is not None:
            GLib.idle_add(lambda: (self._inspected is not None and split.set_show_sidebar(True), False)[1])

    def _apply_target(self) -> str:
        label = self.state.scope_label()
        return "Apply to all displays" if label == "All displays" else f"Apply to {label}"

    def _choose_folder(self) -> None:
        dialog = Gtk.FileDialog(title="Add a wallpaper folder", modal=True)

        def done(source: Gtk.FileDialog, result) -> None:
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return  # canceled
            if folder is not None:
                self.state.toast(f"Added folder “{folder.get_basename()}” · scanning (demo)")

        dialog.select_folder(self.widget.get_root(), None, done)

    def _set_size(self, size: str) -> None:
        self._card_width = 208 if size == "large" else 156
        self.flow._min = self._card_width
        self._rebuild()

    def _on_card(self, wallpaper: Wallpaper) -> None:
        if self._select_mode:
            self._toggle_selected(wallpaper.id)
            return
        self.inspect(wallpaper)

    def inspect(self, wallpaper: Wallpaper) -> None:
        if self._select_mode:
            self._select.set_active(False)
        previous = self._inspected
        self._inspected = wallpaper.id
        if previous in self._cards:
            self._cards[previous].set_selected(False)
        if wallpaper.id not in self._cards and self._visible(wallpaper):
            self._materialize()  # past the pages shown: give it a card so it is outlined
        if wallpaper.id in self._cards:  # it may have been removed from the library
            self._cards[wallpaper.id].set_selected(True)
        self.inspector.show(wallpaper)
        self.split.set_show_sidebar(True)

    def close_inspector(self) -> None:
        if self._inspected in self._cards:
            self._cards[self._inspected].set_selected(False)
        self._inspected = None
        self.split.set_show_sidebar(False)

    def _review_problem(self, _banner) -> None:
        for wallpaper in self.state.wallpapers:
            if wallpaper.problem:
                self.inspect(wallpaper)
                return

    # -- selection mode ----------------------------------------------------------------------
    def _on_select_mode(self, button: Gtk.ToggleButton) -> None:
        self._select_mode = button.get_active()
        if self._select_mode:
            self.close_inspector()  # one thing at a time: picking, not details
        else:
            self._selected.clear()
        for card in self._cards.values():
            card.set_selectable(self._select_mode)
        self._update_selection_bar()

    def _toggle_selected(self, wid: str) -> None:
        if wid in self._selected:
            self._selected.discard(wid)
        else:
            self._selected.add(wid)
        if wid in self._cards:
            self._cards[wid].set_checked(wid in self._selected)
        self._update_selection_bar()

    def select_all(self) -> None:
        for wid, card in self._cards.items():
            if card.get_visible():
                self._selected.add(wid)
                card.set_checked(True)
        self._update_selection_bar()

    def _update_selection_bar(self) -> None:
        count = len(self._selected)
        self._selection_label.set_label("Click wallpapers to select them" if not count else f"{count} selected")
        self._remove_all.set_sensitive(bool(count))
        self._action_bar.set_revealed(self._select_mode)

    def _bulk_add(self, pid: str) -> None:
        if self._selected:
            self.state.add_to_playlist(pid, sorted(self._selected))
            self._select.set_active(False)

    def _bulk_favorite(self) -> None:
        self.state.favorite_wallpapers(sorted(self._selected))
        self.state.toast(f"Added {len(self._selected)} wallpapers to favorites")
        self._select.set_active(False)

    def _confirm_remove_selected(self) -> None:
        chosen = [w for w in self.state.wallpapers if w.id in self._selected]
        if not chosen:
            return
        count = len(chosen)
        what = f"“{chosen[0].name}”" if count == 1 else f"{count} wallpapers"
        dialog = Adw.AlertDialog(
            heading=f"Remove {what} from the library?",
            body="The files stay on disk. You can add them back by scanning the folder again.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "remove" and self._remove(chosen))
        dialog.present(self.widget.get_root())

    def _remove(self, chosen: list[Wallpaper]) -> None:
        self._select.set_active(False)
        undo = self.state.remove_wallpapers([wallpaper.id for wallpaper in chosen])
        count = len(chosen)
        self.state.toast(f"Removed {count} wallpaper{'s' if count != 1 else ''} from the library", undo)


def create(state) -> Page:
    return LibraryPage(state)
