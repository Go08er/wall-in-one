"""Store: find wallpapers online and save them to the Library.

Two providers: Wallhaven (images) and MotionBGS (live wallpapers). The page
opens on what is popular instead of an empty "search first" screen; typing
replaces it with results. Filters apply as soon as they change. Downloads show
progress on the card, can be canceled, and end with a toast that can apply
the new wallpaper straight away.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import store_catalog as catalog
from .. import ui
from ..models import StoreItem
from . import Page
from .store_widgets import CSS, ColorDot, StoreCard, StorePreview, color_button

PAGE_SIZE = 16
LOAD_DELAY = 380  # ms of pretend network time

PAGE_CSS = (
    CSS
    + """
menubutton.store-filter > button { border-radius: 999px; padding: 3px 12px; min-height: 26px; }
menubutton.store-filter.active > button {
  background-color: alpha(var(--accent-bg-color), 0.16);
  color: var(--accent-color);
  box-shadow: inset 0 0 0 1px alpha(var(--accent-bg-color), 0.55);
}
menubutton.store-filter > button:disabled { opacity: 0.5; }
.store-heading { font-size: 1.3em; font-weight: 800; }
.store-dest { font-size: 0.88em; }
.store-dest button.link { padding: 0 2px; min-height: 0; }
.store-key-hint { padding: 6px 10px 2px 10px; }
.store-key-hint button.link { padding: 0; min-height: 0; }
.store-palette { padding: 6px; }
"""
)

_css_installed = False


def _install_css() -> None:
    global _css_installed
    if not _css_installed:
        ui.add_css(PAGE_CSS)
        _css_installed = True


def _plain(text: str) -> str:
    return text.replace("_", "__")


class StorePage(Page):
    name = "store"
    title = "Store"

    def __init__(self, state) -> None:
        super().__init__(state)
        _install_css()
        self._provider = "Wallhaven"
        self._query = ""
        self._sort = "toplist"
        self._sort_chosen = False  # the person picked a sort; don't switch it for them
        self._range = "1w"
        self._reverse = False
        self._size = "any"
        self._ratio = "any"
        self._color: str | None = None
        self._categories = set(catalog.CATEGORIES)
        self._purity = {"SFW"}
        self._genre = "all"
        self._quality = "any"
        self._quiet = False  # suppress refreshes while several options change at once
        self._instant = False  # screenshots: skip the pretend network delay

        self._results: list[StoreItem] = []
        self._shown = 0
        self._cards: dict[str, StoreCard] = {}
        self._progress: dict[str, float] = {}
        self._timers: dict[str, int] = {}
        self._apply_after: set[str] = set()
        self._qualities: dict[str, str | None] = {}
        self._batch: set[str] = set()
        self._batch_size = 0
        self._select_mode = False
        self._picked: set[str] = set()
        self._dialog: StorePreview | None = None
        self._pending = 0
        self._countdown = 0
        self._countdown_timer = 0

        self._group = Gio.SimpleActionGroup()
        self._actions: dict[str, Gio.SimpleAction] = {}
        self._install_actions()

        # -- header ------------------------------------------------------------
        self.search = Gtk.SearchEntry(placeholder_text="Search Wallhaven", search_delay=450)
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda *_: self._on_query(self.search.get_text()))
        self.search.connect("activate", lambda *_: self._on_query(self.search.get_text(), force=True))
        self._title_box = Adw.Clamp(maximum_size=460, child=self.search)

        self._select = ui.select_toggle()
        self._select.connect("toggled", self._on_select_mode)
        more = Gio.Menu()
        workshop = Gio.Menu()
        workshop.append("Steam Workshop…", "store.workshop")
        more.append_section(None, workshop)
        setup = Gio.Menu()
        setup.append("Wallhaven API key…", "store.api-key")
        setup.append("Download folder…", "store.folder")
        more.append_section(None, setup)
        self._more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more)
        self._more.set_tooltip_text("More")
        self._more.insert_action_group("store", self._group)

        # -- filter row ----------------------------------------------------------
        self._filters = Adw.WrapBox(child_spacing=8, line_spacing=8, margin_start=18, margin_top=10, margin_bottom=10)
        self._providers = Adw.ToggleGroup()
        for name, provider in catalog.PROVIDERS.items():
            content = Gtk.Box(spacing=6, margin_start=6, margin_end=6)
            content.append(Gtk.Image(icon_name=provider.icon))
            content.append(Gtk.Label(label=name))
            self._providers.add(
                Adw.Toggle(name=name, child=content, tooltip=f"{provider.noun.capitalize()} from {provider.site}")
            )
        self._providers.set_active_name(self._provider)
        self._providers.connect("notify::active-name", self._on_provider)
        self._filters.append(self._providers)

        self._sort_button = self._filter_button("Sort order")
        self._size_button = self._filter_button("Minimum resolution")
        self._ratio_button = self._filter_button("Aspect ratio")
        self._color_button = self._filter_button("Main color")
        self._category_button = self._filter_button("Categories")
        self._purity_button = self._filter_button("Content rating")
        self._genre_button = self._filter_button("Category")
        self._quality_button = self._filter_button("Quality")
        self._sort_button.remove_css_class("store-filter")
        self._sort_button.add_css_class("flat")
        self._sort_button.set_always_show_arrow(False)
        self._sort_button.set_valign(Gtk.Align.START)
        self._wallhaven_filters = [
            self._size_button,
            self._ratio_button,
            self._color_button,
            self._category_button,
            self._purity_button,
        ]
        self._motion_filters = [self._genre_button, self._quality_button]
        self._reset = Gtk.Button(label="Reset")
        self._reset.add_css_class("flat")
        self._reset.set_tooltip_text("Back to the default filters")
        self._reset.connect("clicked", lambda *_: self._reset_filters())
        for widget in (*self._wallhaven_filters, *self._motion_filters, self._reset):
            self._filters.append(widget)
        self._filters.set_hexpand(True)
        # The right end stays free for the Select pill hanging over the results' corner.
        filter_row = Gtk.Box(spacing=8, margin_end=ui.CORNER_RESERVE)
        filter_row.append(self._filters)
        self._sort_button.set_margin_top(10)
        filter_row.append(self._sort_button)
        self._build_static_menus()
        self._color_button.set_popover(self._color_popover())

        # -- results ---------------------------------------------------------------
        self._heading = Gtk.Label(xalign=0, wrap=True)
        self._heading.add_css_class("store-heading")
        self._count = Gtk.Label(xalign=0, valign=Gtk.Align.BASELINE_CENTER)
        self._count.add_css_class("dimmed")
        self._count.add_css_class("numeric")
        titles = Gtk.Box(spacing=10, hexpand=True)
        self._heading.set_valign(Gtk.Align.BASELINE_CENTER)
        titles.append(self._heading)
        titles.append(self._count)
        self._dest = self._destination()
        self._heading_row = Gtk.Box(
            spacing=12, margin_start=18, margin_end=ui.CORNER_RESERVE, margin_top=20, margin_bottom=2
        )
        self._heading_row.append(titles)
        self._heading_row.append(self._dest)

        # Equal columns (see ui.CardGrid): FlowBox gives some columns an extra
        # pixel, which height-for-width cards report as allocation criticals.
        self.flow = ui.CardGrid(
            min_width=220,
            max_columns=10,
            valign=Gtk.Align.START,
            margin_start=14,
            margin_end=14,
            margin_top=6,
            margin_bottom=8,
        )
        self._footer = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            margin_bottom=22,
            margin_top=4,
            hhomogeneous=False,
            vhomogeneous=False,
        )
        load_more = Gtk.Button(label="Load more", halign=Gtk.Align.CENTER)
        load_more.add_css_class("pill")
        load_more.connect("clicked", lambda *_: self._load_more())
        self._footer.add_named(load_more, "more")
        spinner = Adw.Spinner(halign=Gtk.Align.CENTER, height_request=24, width_request=24)
        self._footer.add_named(spinner, "loading")
        self._end_label = ui.dim("That's everything", wrap=False)
        self._end_label.set_halign(Gtk.Align.CENTER)
        self._footer.add_named(self._end_label, "end")

        results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        results.append(self._heading_row)
        results.append(self.flow)
        results.append(self._footer)
        self._scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._scroller.set_child(results)
        self._scroller.connect("edge-reached", self._on_edge)

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120, vexpand=True
        )
        self._stack.add_named(self._scroller, "results")
        self._stack.add_named(self._loading_page(), "loading")
        self._empty = Adw.StatusPage(icon_name="edit-find-symbolic")
        self._empty_buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self._empty.set_child(self._empty_buttons)
        self._stack.add_named(self._empty, "empty")
        self._error = Adw.StatusPage()
        self._error_buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self._error.set_child(self._error_buttons)
        self._stack.add_named(self._error, "error")

        # -- selection bar -----------------------------------------------------------
        self._action_bar = Gtk.ActionBar(revealed=False)
        select_all = Gtk.Button(label="Select all")
        select_all.add_css_class("flat")
        select_all.set_tooltip_text("Pick every result shown that isn't in your library yet")
        select_all.connect("clicked", lambda *_: self.select_all())
        self._action_bar.pack_start(select_all)
        self._selection_label = Gtk.Label()
        self._action_bar.pack_start(self._selection_label)
        self._batch_button = Gtk.Button(label="Download")
        self._batch_button.add_css_class("suggested-action")
        self._batch_button.connect("clicked", lambda *_: self._download_picked())
        self._action_bar.pack_end(self._batch_button)

        # Narrow windows: the provider row stays, the filters become one scrolling strip.
        self._chip_strip = Gtk.Box(spacing=8, margin_start=18, margin_end=18, margin_bottom=8)
        self._chip_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vscrollbar_policy=Gtk.PolicyType.NEVER, visible=False
        )
        self._chip_scroller.set_child(self._chip_strip)

        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        listing.append(filter_row)
        listing.append(self._chip_scroller)
        listing.append(self._stack)
        hanging = Gtk.Overlay(child=listing)
        ui.hang_on_corner(hanging, self._stack, self._select)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(hanging)
        content.append(self._action_bar)
        breakpoint_bin = Adw.BreakpointBin(width_request=360, height_request=300)
        breakpoint_bin.set_child(content)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
        narrow.add_setter(self._heading_row, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self._heading_row, "spacing", 4)
        narrow.add_setter(self._dest, "halign", Gtk.Align.START)
        narrow.connect("apply", lambda *_: self._set_narrow(True))
        narrow.connect("unapply", lambda *_: self._set_narrow(False))
        breakpoint_bin.add_breakpoint(narrow)
        self.widget = breakpoint_bin
        self.widget.insert_action_group("store", self._group)

        state.connect("changed", self._on_changed)
        self._sync_controls()
        self._refresh(instant=True)

    # -- Page API ------------------------------------------------------------------
    def title_widget(self) -> Gtk.Widget:
        return self._title_box

    def header_end(self) -> list[Gtk.Widget]:
        return [self._more]

    def focus_search(self) -> bool:
        self.search.grab_focus()
        return True

    def activate(self, argument: str | None) -> None:
        if not argument:
            return
        for name in catalog.PROVIDERS:
            if argument.lower() == name.lower():
                self._providers.set_active_name(name)

    def demo(self, scene: str) -> None:
        what, _, arg = scene.partition(":")
        self._instant = True
        self._reset_demo()
        if what == "search":
            self._set_query(arg)
        elif what == "provider":
            self._providers.set_active_name(arg)
        elif what == "downloading":
            if arg:
                self._providers.set_active_name(arg)
            targets = [i for i in self._results[: self._shown] if not i.in_library]
            for item, fraction in zip((targets[0], targets[4]), (0.42, 0.77), strict=True):
                self._progress[item.id] = fraction
                self._update_item(item)
        elif what == "preview":
            self._show_demo_item(arg)
        elif what == "preview-downloading":
            item = self._show_demo_item(arg)
            self._progress[item.id] = 0.58
            self._update_item(item)
        elif what in ("error", "rate-limit", "challenge"):
            if what == "challenge":
                self._providers.set_active_name("MotionBGS")
            self._show_problem(what)
        elif what == "loading":
            self._set_query(arg or "forest")
            self._stack.set_visible_child_name("loading")
        elif what == "filters":
            button = {
                "sort": self._sort_button,
                "size": self._size_button,
                "ratio": self._ratio_button,
                "color": self._color_button,
                "categories": self._category_button,
                "genre": self._genre_button,
                "quality": self._quality_button,
            }.get(arg, self._purity_button)
            if button in self._motion_filters:
                self._providers.set_active_name("MotionBGS")

            def show() -> bool:
                if self._chip_scroller.get_visible():
                    # Narrow: bring the chip into view first.
                    adjustment = self._chip_scroller.get_hadjustment()
                    bounds = button.compute_bounds(self._chip_strip)
                    if bounds[0]:
                        adjustment.set_value(bounds[1].get_x() - 18)
                button.popup()
                return False

            GLib.timeout_add(300, show)
        elif what == "active":
            self._set_radio("size", "2160")
            self._set_radio("ratio", "16:9")
            self._set_color("0066cc")
        elif what == "empty":
            self._set_query(arg or "zebra crossing")
        elif what == "hover":
            card = self._cards.get(arg) or next(iter(self._cards.values()))
            card.add_css_class("force-hover")
        elif what == "select":
            self._select.set_active(True)
            for item in [i for i in self._results[: self._shown] if not i.in_library][:3]:
                self._toggle_pick(item)
        elif what == "more":
            self._load_more()
            GLib.timeout_add(250, self._scroll_to_end)
        elif what == "saved":
            item = next(i for i in self._results if not i.in_library)
            self._progress[item.id] = 0.99
            self._finish(item)
        elif what == "menu":
            card = self._cards.get(arg) or next(iter(self._cards.values()))
            GLib.timeout_add(300, lambda: (self.popup_menu(card, 120, 70), False)[1])
        elif what == "like":
            item = self.state.store_item(arg or "wallhaven-0")
            self.more_like(item)
        elif what == "nsfw-key":
            self._demo_key = True
            self.state.set_wallhaven_key_saved(True)
            self._sync_key()
            self._sync_controls()
            GLib.timeout_add(300, lambda: (self._purity_button.popup(), False)[1])

    def _reset_demo(self) -> None:
        if getattr(self, "_demo_key", False):
            self._demo_key = False
            self.state.set_wallhaven_key_saved(False)
            self._sync_key()
        for button in (self._sort_button, *self._wallhaven_filters, *self._motion_filters, self._more):
            button.popdown()
        if self._dialog:
            self._dialog.force_close()
        for item_id in list(self._progress):
            self.cancel(self.state.store_item(item_id))
        self._select.set_active(False)
        self._quiet = True
        self._providers.set_active_name("Wallhaven")
        self._reset_filters(refresh=False)
        self._genre = "all"
        self._quality = "any"
        self._sync_radio("genre", "all")
        self._sync_radio("quality", "any")
        self.search.set_text("")
        self._query = ""
        self._quiet = False
        self._refresh(instant=True)

    def _show_demo_item(self, item_id: str) -> StoreItem:
        item = self.state.store_item(item_id) or self._results[0]
        if item.provider != self._provider:
            self._providers.set_active_name(item.provider)
        self.preview(item)
        return item

    def _scroll_to_end(self) -> bool:
        adjustment = self._scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        return False

    # -- actions ---------------------------------------------------------------------
    def _install_actions(self) -> None:
        def simple(name: str, callback, parameter: bool = True) -> None:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s") if parameter else None)
            action.connect("activate", lambda _a, value: callback(value.get_string() if value else None))
            self._group.add_action(action)
            self._actions[name] = action

        def radio(name: str, initial: str, callback) -> None:
            action = Gio.SimpleAction.new_stateful(name, GLib.VariantType.new("s"), GLib.Variant("s", initial))

            def change(act: Gio.SimpleAction, value: GLib.Variant) -> None:
                act.set_state(value)
                callback(value.get_string())

            action.connect("change-state", change)
            self._group.add_action(action)
            self._actions[name] = action

        def check(name: str, initial: bool, callback) -> None:
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant("b", initial))

            def change(act: Gio.SimpleAction, value: GLib.Variant) -> None:
                act.set_state(value)
                callback(value.get_boolean())

            action.connect("change-state", change)
            self._group.add_action(action)
            self._actions[name] = action

        radio("sort", self._sort, self._on_sort)
        radio("range", self._range, lambda v: self._changed("_range", v))
        check("reverse", False, lambda v: self._changed("_reverse", v))
        radio("size", "any", lambda v: self._changed("_size", v))
        radio("ratio", "any", lambda v: self._changed("_ratio", v))
        for category in catalog.CATEGORIES:
            check(f"cat-{category.lower()}", True, lambda v, c=category: self._on_category(c, v))
        for purity in catalog.PURITIES:
            check(purity.lower(), purity == "SFW", lambda v, p=purity: self._on_purity(p, v))
        radio("genre", "all", lambda v: self._changed("_genre", v))
        radio("quality", "any", lambda v: self._changed("_quality", v))

        def find(item_id: str) -> StoreItem:
            return self.state.store_item(item_id)

        simple("preview", lambda v: self.preview(find(v)))
        simple("download", lambda v: self.download(find(v)))
        simple("apply", lambda v: self.apply_item(find(v)))
        simple("show", lambda v: self.show_in_library(find(v)))
        simple("site", lambda v: self.open_site(find(v)))
        simple("like", lambda v: self.more_like(find(v)))
        simple("workshop", lambda _v: self.state.toast("Would open the Wallpaper Engine Steam Workshop"), False)
        simple("api-key", lambda _v: self.state.navigate("settings:online"), False)
        simple("folder", lambda _v: self.state.navigate("settings:library"), False)
        self._sync_key()

    def _set_radio(self, name: str, value: str) -> None:
        self._actions[name].change_state(GLib.Variant("s", value))

    def _sync_radio(self, name: str, value: str) -> None:
        """Move a radio's check mark without running its callback."""
        self._actions[name].set_state(GLib.Variant("s", value))

    def _sync_check(self, name: str, value: bool) -> None:
        self._actions[name].set_state(GLib.Variant("b", value))

    # -- filter widgets ------------------------------------------------------------------
    def _set_narrow(self, narrow: bool) -> None:
        for chip in (*self._wallhaven_filters, *self._motion_filters, self._reset):
            parent = chip.get_parent()
            if parent is not None:
                parent.remove(chip)
            (self._chip_strip if narrow else self._filters).append(chip)
        self._chip_scroller.set_visible(narrow)
        self._filters.set_margin_bottom(8 if narrow else 6)

    def _filter_button(self, tooltip: str) -> Gtk.MenuButton:
        button = Gtk.MenuButton(always_show_arrow=True, tooltip_text=tooltip)
        button.add_css_class("store-filter")
        return button

    def _build_static_menus(self) -> None:
        def radio_menu(action: str, options) -> Gio.Menu:
            menu = Gio.Menu()
            for key, label, *_rest in options:
                menu.append(_plain(label), f"store.{action}::{key}")
            return menu

        self._size_button.set_menu_model(radio_menu("size", catalog.SIZES))
        self._ratio_button.set_menu_model(radio_menu("ratio", catalog.RATIOS))
        categories = Gio.Menu()
        for category in catalog.CATEGORIES:
            categories.append(category, f"store.cat-{category.lower()}")
        self._category_button.set_menu_model(categories)
        self._genre_button.set_menu_model(radio_menu("genre", catalog.MOTION_CATEGORIES))
        self._quality_button.set_menu_model(radio_menu("quality", catalog.MOTION_QUALITIES))
        self._build_purity_menu()
        self._build_sort_menu()

    def _build_sort_menu(self) -> None:
        menu = Gio.Menu()
        sorts = Gio.Menu()
        for key, label in catalog.SORTS:
            if key == "relevance" and not self._query:
                continue
            sorts.append(label, f"store.sort::{key}")
        menu.append_section(None, sorts)
        if self._sort == "toplist":
            ranges = Gio.Menu()
            for key, label, _phrase in catalog.RANGES:
                ranges.append(label, f"store.range::{key}")
            menu.append_section("Popular in", ranges)
        if self._sort not in ("random", "relevance"):
            order = Gio.Menu()
            order.append("Reverse order", "store.reverse")
            menu.append_section(None, order)
        self._sort_button.set_menu_model(menu)
        # It sits at the window's right edge: open towards the left.
        self._sort_button.get_popover().set_halign(Gtk.Align.END)

    def _build_purity_menu(self) -> None:
        menu = Gio.Menu()
        levels = Gio.Menu()
        for purity in catalog.PURITIES:
            levels.append(purity, f"store.{purity.lower()}")
        menu.append_section(None, levels)
        if not self._has_key():
            hint = Gio.Menu()
            item = Gio.MenuItem.new(None, None)
            item.set_attribute_value("custom", GLib.Variant("s", "key-hint"))
            hint.append_item(item)
            menu.append_section(None, hint)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        if not self._has_key():
            row = Gtk.Box(spacing=8)
            row.add_css_class("store-key-hint")
            icon = Gtk.Image(icon_name="dialog-password-symbolic", valign=Gtk.Align.START, margin_top=2)
            icon.add_css_class("dimmed")
            row.append(icon)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            label = Gtk.Label(label="NSFW needs an API key", xalign=0)
            label.add_css_class("dimmed")
            label.add_css_class("caption")
            text.append(label)
            add = Gtk.Button(label="Add key…", halign=Gtk.Align.START)
            add.add_css_class("link")
            add.add_css_class("caption")
            add.set_tooltip_text("Open Settings › Online")

            def go(*_args) -> None:
                popover.popdown()
                self.state.navigate("settings:online")

            add.connect("clicked", go)
            text.append(add)
            row.append(text)
            popover.add_child(row, "key-hint")
        self._purity_button.set_popover(popover)

    def _color_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("store-palette")
        top = Gtk.Box(spacing=8)
        label = Gtk.Label(label="MAIN COLOR", xalign=0, hexpand=True)
        label.add_css_class("section-label")
        top.append(label)
        self._any_color = Gtk.Button(label="Any color")
        self._any_color.add_css_class("flat")
        self._any_color.add_css_class("caption")
        self._any_color.connect("clicked", lambda *_: (popover.popdown(), self._set_color(None)))
        top.append(self._any_color)
        box.append(top)
        grid = Gtk.Grid(row_spacing=2, column_spacing=2)
        self._palette_dots: dict[str, ColorDot] = {}
        for index, color in enumerate(catalog.COLORS):
            button = color_button(color, f"#{color}", 24)
            self._palette_dots[color] = button.get_child()
            button.connect(
                "clicked", lambda *_, c=color: (popover.popdown(), self._set_color(None if self._color == c else c))
            )
            grid.attach(button, index % 10, index // 10, 1, 1)
        box.append(grid)
        popover.set_child(box)
        return popover

    def _destination(self) -> Gtk.Widget:
        root = self.state.download_folder
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.add_css_class("store-dest")
        icon = Gtk.Image(icon_name="folder-download-symbolic")
        icon.add_css_class("dimmed")
        box.append(icon)
        label = Gtk.Label(label=f"Saves to {root}", ellipsize=1)  # Pango.EllipsizeMode.START
        label.add_css_class("dimmed")
        self._dest_label = label
        box.append(label)
        change = Gtk.Button(label="Change")
        change.add_css_class("link")
        change.set_tooltip_text("Choose the download folder in Settings")
        change.connect("clicked", lambda *_: self.state.navigate("settings:library"))
        box.append(change)
        box.set_tooltip_text(f"Downloads go to Wall-in-One/Downloads/{self._provider} in your first library folder")
        return box

    def _loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER
        )
        box.append(Adw.Spinner(width_request=32, height_request=32))
        self._loading_label = ui.dim("Searching Wallhaven…", wrap=False)
        box.append(self._loading_label)
        return box

    # -- syncing controls with the options ------------------------------------------------
    def _has_key(self) -> bool:
        return self.state.wallhaven_key_saved

    def _sync_key(self) -> None:
        if "nsfw" in self._actions:
            self._actions["nsfw"].set_enabled(self._has_key())
            if not self._has_key() and "NSFW" in self._purity:
                self._purity.discard("NSFW")
                self._sync_check("nsfw", False)
        if hasattr(self, "_purity_button"):
            self._build_purity_menu()

    def _sync_controls(self) -> None:
        wallhaven = self._provider == "Wallhaven"
        for widget in (*self._wallhaven_filters, self._sort_button):
            widget.set_visible(wallhaven)
        for widget in self._motion_filters:
            widget.set_visible(not wallhaven)
        self.search.set_placeholder_text(f"Search {self._provider}")
        self._dest.set_tooltip_text(
            f"Downloads go to Wall-in-One/Downloads/{self._provider} in your first library folder"
        )

        sort_label = catalog.SORT_LABEL[self._sort]
        self._sort_button.set_child(
            Adw.ButtonContent(icon_name="view-sort-descending-symbolic", label=sort_label, can_shrink=True)
        )
        self._sort_button.set_tooltip_text("Sort order")
        size_label = next(s[2] for s in catalog.SIZES if s[0] == self._size)
        self._mark(self._size_button, size_label, self._size != "any")
        ratio_label = next(r[2] for r in catalog.RATIOS if r[0] == self._ratio)
        self._mark(self._ratio_button, ratio_label, self._ratio != "any")
        color_box = Gtk.Box(spacing=6)
        if self._color:
            color_box.append(ColorDot(self._color, 14))
        color_box.append(Gtk.Label(label="Color" if self._color else "Any color"))
        self._color_button.set_child(color_box)
        self._set_active(self._color_button, bool(self._color))
        self._any_color.set_sensitive(bool(self._color))
        for color, dot in self._palette_dots.items():
            dot.set_selected(color == self._color)
        chosen = [c for c in catalog.CATEGORIES if c in self._categories]
        self._mark(
            self._category_button,
            "All categories" if len(chosen) == len(catalog.CATEGORIES) else ", ".join(chosen),
            len(chosen) != len(catalog.CATEGORIES),
        )
        purity = [p for p in catalog.PURITIES if p in self._purity]
        self._mark(self._purity_button, " + ".join(purity), purity != ["SFW"])
        self._actions["nsfw"].set_enabled(self._has_key())

        genre = dict(catalog.MOTION_CATEGORIES)[self._genre]
        self._mark(self._genre_button, genre, self._genre != "all")
        searching = bool(self._query.strip())
        self._genre_button.set_sensitive(not searching)
        self._genre_button.set_tooltip_text("Clear the search to browse by category" if searching else "Category")
        quality = next(q[2] for q in catalog.MOTION_QUALITIES if q[0] == self._quality)
        self._mark(self._quality_button, quality, self._quality != "any")
        self._reset.set_visible(self._filters_active())

    def _mark(self, button: Gtk.MenuButton, label: str, active: bool) -> None:
        button.set_label(label)
        self._set_active(button, active)

    @staticmethod
    def _set_active(button: Gtk.Widget, active: bool) -> None:
        if active:
            button.add_css_class("active")
        else:
            button.remove_css_class("active")

    def _filters_active(self) -> bool:
        if self._provider == "Wallhaven":
            return (
                self._size != "any"
                or self._ratio != "any"
                or bool(self._color)
                or self._categories != set(catalog.CATEGORIES)
                or self._purity != {"SFW"}
            )
        return self._genre != "all" or self._quality != "any"

    # -- option callbacks -------------------------------------------------------------------
    def _changed(self, attribute: str, value) -> None:
        setattr(self, attribute, value)
        self._refresh()

    def _on_sort(self, value: str) -> None:
        self._sort = value
        self._sort_chosen = True
        self._build_sort_menu()
        self._refresh()

    def _on_category(self, category: str, value: bool) -> None:
        if not value and self._categories == {category}:
            # At least one category stays on (an empty search would mean nothing).
            self._sync_check(f"cat-{category.lower()}", True)
            return
        if value:
            self._categories.add(category)
        else:
            self._categories.discard(category)
        self._refresh()

    def _on_purity(self, purity: str, value: bool) -> None:
        if not value and self._purity == {purity}:
            self._sync_check(purity.lower(), True)
            return
        if value:
            self._purity.add(purity)
        else:
            self._purity.discard(purity)
        self._refresh()

    def _set_color(self, color: str | None) -> None:
        self._color = color
        self._refresh()

    def _on_provider(self, group: Adw.ToggleGroup, _param) -> None:
        provider = group.get_active_name()
        if provider == self._provider or provider not in catalog.PROVIDERS:
            return
        self._provider = provider
        self._loading_label.set_label(f"Loading {provider}…")
        self._refresh()

    def _set_query(self, text: str) -> None:
        self.search.set_text(text)
        self._on_query(text, force=True)

    def _on_query(self, text: str, force: bool = False) -> None:
        text = text.strip()
        if text == self._query and not force:
            return
        if text == self._query and force and self._stack.get_visible_child_name() == "results":
            return
        self._query = text
        # Searching ranks by relevance unless the person chose a sort.
        if not self._sort_chosen:
            self._sort = "relevance" if text else "toplist"
            self._sync_radio("sort", self._sort)
        elif not text and self._sort == "relevance":
            self._sort = "toplist"
            self._sync_radio("sort", self._sort)
        self._build_sort_menu()
        self._refresh()

    def _reset_filters(self, refresh: bool = True) -> None:
        quiet = self._quiet
        self._quiet = True
        if self._provider == "Wallhaven" or not refresh:
            self._size, self._ratio, self._color = "any", "any", None
            self._categories = set(catalog.CATEGORIES)
            self._purity = {"SFW"}
            self._sync_radio("size", "any")
            self._sync_radio("ratio", "any")
            for category in catalog.CATEGORIES:
                self._sync_check(f"cat-{category.lower()}", True)
            for purity in catalog.PURITIES:
                self._sync_check(purity.lower(), purity == "SFW")
            if not refresh:
                self._sort, self._sort_chosen, self._range, self._reverse = "toplist", False, "1w", False
                self._sync_radio("sort", "toplist")
                self._sync_radio("range", "1w")
                self._sync_check("reverse", False)
                self._build_sort_menu()
        if self._provider == "MotionBGS" or not refresh:
            self._genre, self._quality = "all", "any"
            self._sync_radio("genre", "all")
            self._sync_radio("quality", "any")
        self._quiet = quiet
        if refresh:
            self._refresh()

    # -- results --------------------------------------------------------------------------------
    def _current_query(self) -> catalog.Query:
        heights = {key: height for key, _l, _b, height in catalog.SIZES}
        return catalog.Query(
            provider=self._provider,
            text=self._query,
            sort=self._sort,
            top_range=self._range,
            reverse=self._reverse,
            min_height=heights[self._size],
            ratio=self._ratio,
            color=self._color,
            categories=frozenset(self._categories),
            genre=self._genre,
            quality=self._quality,
        )

    def _heading_text(self) -> str:
        text = self._query
        if text.startswith("like:"):
            source = self.state.store_like_source(text)
            return f"More like “{source.title}”" if source else "Similar wallpapers"
        if text:
            return f"Results for “{text}”"
        if self._provider == "MotionBGS":
            genre = dict(catalog.MOTION_CATEGORIES)[self._genre]
            base = "Latest live wallpapers" if self._genre == "all" else f"Latest in {genre}"
            return base + (" · 4K" if self._quality == "4k" else "")
        if self._sort == "toplist":
            phrase = next(r[2] for r in catalog.RANGES if r[0] == self._range)
            return f"Popular {phrase}"
        return {
            "date_added": "Latest",
            "hot": "Hot right now",
            "views": "Most viewed",
            "favorites": "Most favorited",
            "random": "Random picks",
        }.get(self._sort, "Popular this week")

    def _refresh(self, instant: bool = False) -> None:
        if self._quiet:
            return
        self._sync_controls()
        self._stop_countdown()
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = 0
        if instant or self._instant:
            self._show_results()
            return
        self._loading_label.set_label(f"Searching {self._provider}…" if self._query else f"Loading {self._provider}…")
        self._stack.set_visible_child_name("loading")
        self._pending = GLib.timeout_add(LOAD_DELAY, self._show_results)

    def _show_results(self) -> bool:
        self._pending = 0
        self._results = self.state.store_search(self._current_query())
        self._clear_cards()
        self._shown = 0
        self._heading.set_label(self._heading_text())
        if not self._results:
            self._show_empty()
            return False
        self._append(PAGE_SIZE)
        self._stack.set_visible_child_name("results")
        self._scroller.get_vadjustment().set_value(0)
        return False

    def _clear_cards(self) -> None:
        self.flow.remove_all()
        self._cards = {}

    def _append(self, count: int) -> None:
        for item in self._results[self._shown : self._shown + count]:
            card = StoreCard(item, self)
            if self._select_mode:
                card.set_selectable(True)
                card.set_checked(item.id in self._picked)
            self._cards[item.id] = card
            self.flow.append(card)
        self._shown = min(len(self._results), self._shown + count)
        self._count.set_label(f"{self._shown} of {len(self._results)}")
        self._footer.set_visible_child_name("more" if self._shown < len(self._results) else "end")
        if self._dialog:
            self._dialog._items = self._results[: self._shown]

    def _on_edge(self, _scroller, position: Gtk.PositionType) -> None:
        if position == Gtk.PositionType.BOTTOM and not self._instant:
            self._load_more()

    def _load_more(self) -> None:
        if self._shown >= len(self._results) or self._footer.get_visible_child_name() == "loading":
            return
        if self._instant:
            self._append(PAGE_SIZE)
            return
        self._footer.set_visible_child_name("loading")

        def done() -> bool:
            self._append(PAGE_SIZE)
            return False

        GLib.timeout_add(600, done)

    def _show_empty(self) -> None:
        child = self._empty_buttons.get_first_child()
        while child:
            self._empty_buttons.remove(child)
            child = self._empty_buttons.get_first_child()
        if self._query:
            self._empty.set_title(f"No results for “{self._query}”")
            self._empty.set_description("Try other words" + (" or fewer filters" if self._filters_active() else ""))
            clear = Gtk.Button(label="Clear search")
            clear.add_css_class("pill")
            clear.connect("clicked", lambda *_: self._set_query(""))
            self._empty_buttons.append(clear)
        else:
            self._empty.set_title("Nothing matches these filters")
            self._empty.set_description("Try a different size, ratio or color")
        if self._filters_active():
            reset = Gtk.Button(label="Reset filters")
            reset.add_css_class("pill")
            reset.connect("clicked", lambda *_: self._reset_filters())
            self._empty_buttons.append(reset)
        self._count.set_label("")
        self._stack.set_visible_child_name("empty")

    # -- problems (offline, rate limit, browser check) -------------------------------------------
    def _show_problem(self, kind: str) -> None:
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = 0
        child = self._error_buttons.get_first_child()
        while child:
            self._error_buttons.remove(child)
            child = self._error_buttons.get_first_child()
        provider = catalog.PROVIDERS[self._provider]
        retry = Gtk.Button(label="Try again")
        retry.add_css_class("pill")
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda *_: self._retry())
        if kind == "rate-limit":
            self._error.set_icon_name("preferences-system-time-symbolic")
            self._error.set_title(f"{provider.name} needs a short break")
            self._countdown = 8
            self._update_countdown()
            self._countdown_timer = GLib.timeout_add_seconds(1, self._tick_countdown)
            retry.set_label("Try now")
            self._error_buttons.append(retry)
        elif kind == "challenge":
            self._error.set_icon_name("channel-secure-symbolic")
            self._error.set_title(f"{provider.name} wants a browser check")
            self._error.set_description(f"Open {provider.site} in your browser once, then try again")
            site = Gtk.Button(label=f"Open {provider.site}")
            site.add_css_class("pill")
            site.connect("clicked", lambda *_: self.state.toast(f"Would open {provider.site} in your browser"))
            self._error_buttons.append(site)
            self._error_buttons.append(retry)
        else:
            self._error.set_icon_name("network-offline-symbolic")
            self._error.set_title(f"Can't reach {provider.name}")
            self._error.set_description("Check your connection, then try again")
            self._error_buttons.append(retry)
            library = Gtk.Button(label="Go to Library")
            library.add_css_class("pill")
            library.connect("clicked", lambda *_: self.state.navigate("library"))
            self._error_buttons.append(library)
        self._count.set_label("")
        self._stack.set_visible_child_name("error")

    def _update_countdown(self) -> None:
        self._error.set_description(f"Too many searches in a row. Trying again in {self._countdown} s")

    def _tick_countdown(self) -> bool:
        self._countdown -= 1
        if self._countdown <= 0:
            self._countdown_timer = 0
            self._retry()
            return False
        self._update_countdown()
        return True

    def _stop_countdown(self) -> None:
        if self._countdown_timer:
            GLib.source_remove(self._countdown_timer)
            self._countdown_timer = 0

    def _retry(self) -> None:
        instant, self._instant = self._instant, False
        self._refresh()
        self._instant = instant

    # -- controller API used by cards and the preview dialog ----------------------------------------
    def progress_of(self, item: StoreItem) -> float | None:
        return self._progress.get(item.id)

    def quality_of(self, item: StoreItem) -> str | None:
        """The MotionBGS quality being downloaded (None = the best available)."""
        return self._qualities.get(item.id)

    def open_item(self, item: StoreItem) -> None:
        if self._select_mode:
            self._toggle_pick(item)
        else:
            self.preview(item)

    def preview(self, item: StoreItem) -> None:
        items = self._results[: self._shown] if any(i.id == item.id for i in self._results) else [item]
        if self._dialog:
            self._dialog.show_item(item)
            return
        self._dialog = StorePreview(self, items, item)
        self._dialog.present(self.widget.get_root())

    def dialog_closed(self, dialog: StorePreview) -> None:
        if self._dialog is dialog:
            self._dialog = None

    def download(self, item: StoreItem, apply: bool = False, quality: str | None = None, batch: bool = False) -> None:
        if item.in_library:
            if apply:
                self.apply_item(item)
            return
        if apply:
            self._apply_after.add(item.id)
        if item.id in self._progress:
            return
        self._progress[item.id] = 0.0
        self._qualities[item.id] = quality
        if batch:
            self._batch.add(item.id)
        self._timers[item.id] = GLib.timeout_add(40, self._tick, item)
        self._update_item(item)

    def _tick(self, item: StoreItem) -> bool:
        # ~1.7–2.4 s, a little uneven like a real transfer.
        step = 0.016 + (item.seed % 5) * 0.0015 + (0.006 if int(self._progress[item.id] * 40) % 3 else 0)
        self._progress[item.id] = min(1.0, self._progress[item.id] + step)
        if self._progress[item.id] >= 1.0:
            self._timers.pop(item.id, None)
            self._finish(item)
            return False
        self._update_item(item)
        return True

    def _finish(self, item: StoreItem) -> None:
        self._progress.pop(item.id, None)
        wid = self.state.import_store_item(item.id, self._qualities.get(item.id), fresh=True)
        self._update_item(item)
        if item.id in self._apply_after:
            self._apply_after.discard(item.id)
            self.state.apply(wid, self.state.scope)
        elif item.id in self._batch:
            self._batch.discard(item.id)
            if not self._batch:
                count = self._batch_size
                self._toast_with_action(
                    f"{count} wallpapers saved to Library", "Show", lambda: self.state.navigate("library")
                )
        else:
            self._toast_with_action(f"“{item.title}” saved to Library", "Apply", lambda: self.apply_item(item))

    def cancel(self, item: StoreItem | None) -> None:
        if item is None:
            return
        timer = self._timers.pop(item.id, None)
        if timer:
            GLib.source_remove(timer)
        self._progress.pop(item.id, None)
        self._apply_after.discard(item.id)
        self._batch.discard(item.id)
        self._update_item(item)

    def apply_item(self, item: StoreItem) -> None:
        if not item.in_library:
            self.download(item, apply=True)
            return
        self.state.apply(self.state.import_store_item(item.id, self._qualities.get(item.id)), self.state.scope)

    def show_in_library(self, item: StoreItem) -> None:
        self.state.navigate(f"library:{self.state.import_store_item(item.id, self._qualities.get(item.id))}")

    def open_site(self, item: StoreItem) -> None:
        self.state.toast(f"Would open {catalog.url(item)} in your browser")

    def search_for(self, text: str) -> None:
        if self._dialog:
            self._dialog.close()
        self._set_query(text)

    def more_like(self, item: StoreItem) -> None:
        if item.provider != self._provider:
            self._providers.set_active_name(item.provider)
        if item.provider == "Wallhaven":
            self.search_for(f"like:{catalog.site_id(item)}")
        else:
            self.search_for(catalog.tags(item)[0])

    def search_color(self, color: str) -> None:
        if self._dialog:
            self._dialog.close()
        if self._provider != "Wallhaven":
            self._providers.set_active_name("Wallhaven")
        self._set_color(color)

    def popup_menu(self, card: StoreCard, x: float, y: float) -> None:
        item = card.item
        menu = Gio.Menu()
        first = Gio.Menu()
        first.append("Preview", f"store.preview::{item.id}")
        if item.in_library:
            first.append("Apply", f"store.apply::{item.id}")
            first.append("Show in Library", f"store.show::{item.id}")
        elif item.id not in self._progress:
            first.append("Download", f"store.download::{item.id}")
            first.append("Download & apply", f"store.apply::{item.id}")
        menu.append_section(None, first)
        second = Gio.Menu()
        second.append("More like this", f"store.like::{item.id}")
        second.append(f"Open on {catalog.PROVIDERS[item.provider].site}", f"store.site::{item.id}")
        menu.append_section(None, second)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_has_arrow(False)
        popover.set_parent(card.frame)
        popover.set_pointing_to(_point(x, y))
        popover.connect("closed", lambda p: GLib.idle_add(_unparent, p))
        popover.popup()

    # -- helpers ------------------------------------------------------------------------------------
    def _update_item(self, item: StoreItem) -> None:
        card = self._cards.get(item.id)
        if card:
            card.update()
        if self._dialog and self._dialog.item.id == item.id:
            self._dialog.refresh()
        if self._select_mode:
            self._update_selection_bar()

    def _toast_with_action(self, text: str, label: str, callback) -> None:
        """A toast whose button is not "Undo" (state.toast only offers Undo)."""
        overlay = self.widget.get_ancestor(Adw.ToastOverlay)
        if overlay is None:
            self.state.toast(text)
            return
        toast = Adw.Toast(title=text, timeout=5, button_label=label)
        toast.connect("button-clicked", lambda *_: callback())
        overlay.add_toast(toast)

    def _on_changed(self, _state, topic: str) -> None:
        if topic == "settings":
            self._sync_key()
            self._sync_controls()
            self._dest_label.set_label(f"Saves to {self.state.download_folder}")

    # -- selection mode --------------------------------------------------------------------------------
    def _on_select_mode(self, button: Gtk.ToggleButton) -> None:
        self._select_mode = button.get_active()
        if not self._select_mode:
            self._picked.clear()
        for card in self._cards.values():
            card.set_selectable(self._select_mode)
        self._update_selection_bar()

    def select_all(self) -> None:
        for item_id, card in self._cards.items():
            item = card.item
            if not item.in_library and item_id not in self._progress:
                self._picked.add(item_id)
                card.set_checked(True)
        self._update_selection_bar()

    def _toggle_pick(self, item: StoreItem) -> None:
        if item.in_library or item.id in self._progress:
            return
        if item.id in self._picked:
            self._picked.discard(item.id)
        else:
            self._picked.add(item.id)
        if item.id in self._cards:
            self._cards[item.id].set_checked(item.id in self._picked)
        self._update_selection_bar()

    def _update_selection_bar(self) -> None:
        count = len(self._picked)
        self._selection_label.set_label("Click wallpapers to pick them" if not count else f"{count} selected")
        self._batch_button.set_label(f"Download {count}" if count else "Download")
        self._batch_button.set_sensitive(bool(count))
        self._action_bar.set_revealed(self._select_mode)

    def _download_picked(self) -> None:
        picked = [self.state.store_item(item_id) for item_id in self._picked]
        self._batch_size = len(picked)
        self._select.set_active(False)
        for item in picked:
            self.download(item, batch=True)


def _point(x: float, y: float) -> Gdk.Rectangle:
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    return rect


def _unparent(popover: Gtk.Popover) -> bool:
    if popover.get_parent() is not None:
        popover.unparent()
    return False


def create(state) -> Page:
    return StorePage(state)
