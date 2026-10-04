"""Store: find wallpapers on Wallhaven and MotionBGS and download them into the library.

The page opens on what is popular rather than an empty "search first" screen,
but only once it is first shown: the shell builds every page with the window,
and building one must not reach a website. Typing searches (after a pause),
and a changed filter searches again at once; an answer that a newer search
superseded never reaches the grid.

**What it reads.** Only `AppState.store_access` (`StoreAccess`), plus the
state's toasts, navigation and ``changed`` topics. It never imports the
providers, `Browser` or the session.

**Decisions it keeps** (DECISIONS.md): the filters are inline, in a
`Gtk.Revealer` under the provider row, never in a popover; results are
bounded pages of at most 40 cards with Previous and Next (`store_paging`),
never an infinite grid; downloads go into the first library folder, and
there is no folder of their own to choose.

**Downloads** run one at a time, as the backend does: a card being fetched
shows a spinner, the ones waiting say so, and the footer says "Downloading
2 of 5". Nothing pretends to know how many bytes have arrived, and nothing
offers a cancel the backend can't do.

**Keys.** Enter previews, Space picks (into selection mode), Ctrl+Enter
downloads or applies, Ctrl+A picks this page's downloadable results, Ctrl+D
downloads what is picked, and Ctrl+F is the shell's search.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from wall_in_one.ui.next import widgets
from wall_in_one.ui.next.catalog import quoted
from wall_in_one.ui.next.page import Page, not_ported
from wall_in_one.ui.next.state import AppState
from wall_in_one.ui.next.store_paging import ResultPages
from wall_in_one.ui.next.store_state import (
    CATEGORIES,
    MODES,
    MOTIONBGS,
    PROVIDER,
    PROVIDERS,
    PURITIES,
    QUALITIES,
    RATIOS,
    SIZES,
    SORTS,
    TOP_RANGES,
    WALLHAVEN,
    StoreAccess,
    StoreDetail,
    StoreItemView,
    StoreProblem,
    StoreQuery,
    StoreResults,
    option,
)
from wall_in_one.ui.next.store_widgets import (
    ChipRow,
    ColourPalette,
    StoreCard,
    StorePreview,
    filter_label,
    install_css,
)

#: Card width in the grid (the Library's large size).
CARD_WIDTH: Final = 220
#: Typing pauses this long before a search goes out (every key would be a request).
SEARCH_DELAY_MS: Final = 500
#: The NSFW limitation's way out: the Settings page's Wallhaven key row.
KEY_SETTINGS: Final = "settings:wallhaven"


def _index(table: Sequence[tuple[str, ...]], key: str) -> int:
    return next((at for at, row in enumerate(table) if row[0] == key), 0)


def _dropdown(table: Sequence[tuple[str, ...]], key: str, tooltip: str) -> Gtk.DropDown:
    dropdown = Gtk.DropDown.new_from_strings([row[1] for row in table])
    dropdown.set_selected(_index(table, key))
    dropdown.set_tooltip_text(tooltip)
    dropdown.set_valign(Gtk.Align.CENTER)
    return dropdown


def _labelled(text: str, child: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.append(filter_label(text))
    box.append(child)
    return box


class StorePage(Page):
    name = "store"
    title = "Store"

    def __init__(self, state: AppState, store: StoreAccess) -> None:
        super().__init__(state)
        install_css()
        self.store = store
        self.query = StoreQuery()
        self._sort_chosen = False  # the person picked a sort; don't switch it for them
        self._like_title = ""
        self.pages = ResultPages()
        self._generation = 0
        self.loading = False  # a fresh search is out
        self.fetching = False  # the next provider page is out
        self._started = False
        self._quiet = False  # controls being set from the query, not by a person
        self.cards: dict[str, StoreCard] = {}
        self._pictures: dict[str, Gdk.Paintable] = {}
        self.select_mode = False
        self.picked: dict[str, StoreItemView] = {}
        self.dialog: StorePreview | None = None
        self._menu: Gtk.PopoverMenu | None = None
        self._pending_scene = ""
        self.problem: StoreProblem | None = None

        # -- header ------------------------------------------------------------
        self.search = Gtk.SearchEntry(
            placeholder_text="Search Wallhaven", search_delay=SEARCH_DELAY_MS, hexpand=True
        )
        self.search.connect("search-changed", lambda *_: self._on_text(self.search.get_text()))
        self.search.connect(
            "activate", lambda *_: self._on_text(self.search.get_text(), force=True)
        )
        self._title_box = Adw.Clamp(maximum_size=460, child=self.search)
        more = Gio.Menu()
        more.append("Steam Workshop…", "store.workshop")
        self._more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more)
        self._more.set_tooltip_text("More")
        self._more.set_visible(False)

        # -- the provider row ----------------------------------------------------------
        self._providers = Adw.ToggleGroup(can_shrink=False)
        for provider in PROVIDERS:
            content = Gtk.Box(spacing=6, margin_start=6, margin_end=6)
            content.append(Gtk.Image(icon_name=provider.icon))
            content.append(Gtk.Label(label=provider.title))
            self._providers.add(
                Adw.Toggle(
                    name=provider.name,
                    child=content,
                    tooltip=f"{provider.noun.capitalize()} from {provider.site}",
                )
            )
        self._providers.set_active_name(self.query.provider)
        self._providers.connect("notify::active-name", self._on_provider)
        self.filters_button = Gtk.ToggleButton()
        self._filters_content = Adw.ButtonContent(icon_name="pan-down-symbolic", label="Filters")
        self.filters_button.set_child(self._filters_content)
        self.filters_button.add_css_class("chip")
        self.filters_button.set_tooltip_text("Show the filters")
        self.filters_button.connect("toggled", self._on_filters_toggled)
        self._reset = Gtk.Button(label="Reset")
        self._reset.add_css_class("flat")
        self._reset.set_tooltip_text("Back to the default filters")
        self._reset.connect("clicked", lambda *_: self.reset_filters())
        self.sort = _dropdown(SORTS, self.query.sort, "Sort order")
        self.sort.connect("notify::selected", self._on_sort)
        provider_row = Gtk.Box(spacing=8, margin_start=18, margin_top=10, margin_bottom=6)
        # The right end stays free for the Select pill hanging over the results' corner.
        provider_row.set_margin_end(widgets.CORNER_RESERVE)
        provider_row.append(self._providers)
        provider_row.append(self.filters_button)
        provider_row.append(self._reset)
        provider_row.append(Gtk.Box(hexpand=True))
        provider_row.append(self.sort)
        provider_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
        )
        provider_scroller.set_child(provider_row)

        # -- the filters, inline (DECISIONS: never a popover) --------------------------
        self.revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.set_child(self._build_filters())

        # -- results -------------------------------------------------------------------
        self._heading = Gtk.Label(xalign=0, wrap=True)
        self._heading.add_css_class("store-heading")
        self._count = Gtk.Label(xalign=0)
        self._count.add_css_class("dimmed")
        self._count.add_css_class("numeric")
        titles = Gtk.Box(spacing=10, hexpand=True)
        titles.append(self._heading)
        titles.append(self._count)
        self._dest = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self._dest.add_css_class("store-dest")
        dest_icon = Gtk.Image(icon_name="folder-download-symbolic")
        dest_icon.add_css_class("dimmed")
        self._dest.append(dest_icon)
        self._dest_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.START)
        self._dest_label.add_css_class("dimmed")
        self._dest.append(self._dest_label)
        self._heading_row = Gtk.Box(
            spacing=12,
            margin_start=18,
            margin_end=widgets.CORNER_RESERVE,
            margin_top=20,
            margin_bottom=2,
        )
        self._heading_row.append(titles)
        self._heading_row.append(self._dest)

        self.flow = widgets.CardGrid(
            min_width=CARD_WIDTH,
            max_columns=10,
            valign=Gtk.Align.START,
            margin_start=14,
            margin_end=14,
            margin_top=6,
            margin_bottom=14,
        )
        results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        results.append(self._heading_row)
        results.append(self.flow)
        self._scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._scroller.set_child(results)

        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120, vexpand=True
        )
        self.stack.add_named(self._scroller, "results")
        self.stack.add_named(self._loading_page(), "loading")
        self._empty = Adw.StatusPage(icon_name="edit-find-symbolic")
        self._empty_buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self._empty.set_child(self._empty_buttons)
        self.stack.add_named(self._empty, "empty")
        self._error = Adw.StatusPage()
        self._error_buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self._error.set_child(self._error_buttons)
        self.stack.add_named(self._error, "error")

        # -- the pager: bounded pages, never an infinite grid ----------------------------
        pager = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=4, margin_bottom=4)
        self.previous = widgets.icon_button(
            "go-previous-symbolic", "Previous page", lambda *_: self.show_previous(), "flat"
        )
        self.next = widgets.icon_button(
            "go-next-symbolic", "Next page", lambda *_: self.show_next(), "flat"
        )
        self.page_label = Gtk.Label()
        self.page_label.add_css_class("caption")
        self.page_label.add_css_class("numeric")
        self._fetch_spinner = Adw.Spinner(width_request=16, height_request=16, visible=False)
        self.queue_label = Gtk.Label(xalign=1, hexpand=True)
        self.queue_label.add_css_class("caption")
        self.queue_label.add_css_class("numeric")
        pager.append(self.previous)
        pager.append(self.page_label)
        pager.append(self.next)
        pager.append(self._fetch_spinner)
        pager.append(self.queue_label)
        self._pager = pager

        # -- selection -------------------------------------------------------------------
        self._select = widgets.select_toggle()
        self._select.set_tooltip_text("Pick several results to download together")
        self._select.connect("toggled", self._on_select_mode)
        self._action_bar = Gtk.ActionBar(revealed=False)
        select_all = Gtk.Button(label="Select all")
        select_all.add_css_class("flat")
        select_all.set_tooltip_text("Pick every result on this page that isn't in your library")
        select_all.connect("clicked", lambda *_: self.select_all())
        self._action_bar.pack_start(select_all)
        self._selection_label = Gtk.Label()
        self._action_bar.pack_start(self._selection_label)
        self._batch = Gtk.Button(label="Download")
        self._batch.add_css_class("suggested-action")
        self._batch.connect("clicked", lambda *_: self.download_picked())
        self._action_bar.pack_end(self._batch)

        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        listing.append(provider_scroller)
        listing.append(self.revealer)
        listing.append(self.stack)
        hanging = Gtk.Overlay(child=listing)
        widgets.hang_on_corner(hanging, self.stack, self._select)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(hanging)
        content.append(self._pager)
        content.append(self._action_bar)
        breakpoint_bin = Adw.BreakpointBin(width_request=360, height_request=300)
        breakpoint_bin.set_child(content)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
        narrow.add_setter(self._heading_row, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self._heading_row, "spacing", 4)
        narrow.add_setter(self._dest, "halign", Gtk.Align.START)
        breakpoint_bin.add_breakpoint(narrow)
        self.widget = breakpoint_bin
        self._install_actions()
        self._install_shortcuts()

        state.connect("changed", self._on_changed)
        self._sync_controls()
        self._sync_destination()
        self._sync_facts()
        self.stack.set_visible_child_name("loading")
        self._pager.set_visible(False)

    # -- construction ----------------------------------------------------------------
    def _build_filters(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.add_css_class("store-filters")
        query = self.query

        # -- Wallhaven
        wallhaven = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        first = Adw.WrapBox(child_spacing=18, line_spacing=12)
        self.top_range = _dropdown(TOP_RANGES, query.top_range, "Which top list")
        self.top_range.connect(
            "notify::selected",
            lambda *_: self._changed(top_range=TOP_RANGES[self.top_range.get_selected()][0]),
        )
        # Only the top list has a window, so it shows only then.
        self.top_range_row = _labelled("Top list covers", self.top_range)
        first.append(self.top_range_row)
        self.size = _dropdown(SIZES, query.size, "Minimum resolution")
        self.size.connect(
            "notify::selected", lambda *_: self._changed(size=SIZES[self.size.get_selected()][0])
        )
        first.append(_labelled("Minimum size", self.size))
        self.ratio = _dropdown(RATIOS, query.ratio, "Aspect ratio")
        self.ratio.connect(
            "notify::selected", lambda *_: self._changed(ratio=RATIOS[self.ratio.get_selected()][0])
        )
        first.append(_labelled("Aspect ratio", self.ratio))
        self.reverse = Gtk.CheckButton(label="Reverse order", valign=Gtk.Align.END)
        self.reverse.connect("toggled", lambda *_: self._changed(reverse=self.reverse.get_active()))
        first.append(self.reverse)
        wallhaven.append(first)

        second = Adw.WrapBox(child_spacing=18, line_spacing=12)
        self.categories = ChipRow(
            CATEGORIES, query.categories, lambda: self._changed(categories=self.categories.chosen)
        )
        second.append(_labelled("Categories", self.categories))
        self.purity = ChipRow(
            PURITIES, query.purity, lambda: self._changed(purity=self.purity.chosen)
        )
        rating = _labelled("Content rating", self.purity)
        # The provider's own words for why NSFW is off (a missing key, or what
        # is wrong with the stored one), and where a key is entered.
        self.key_hint = Gtk.Box(spacing=6)
        self.key_hint.add_css_class("store-key-hint")
        key_icon = Gtk.Image(icon_name="dialog-password-symbolic")
        key_icon.add_css_class("dimmed")
        self.key_hint.append(key_icon)
        self.key_hint_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        self.key_hint_label.add_css_class("dimmed")
        self.key_hint.append(self.key_hint_label)
        add_key = Gtk.Button(label="Add a key in Settings")
        add_key.add_css_class("link")
        add_key.connect("clicked", lambda *_: self.state.navigate(KEY_SETTINGS))
        self.key_hint.append(add_key)
        rating.append(self.key_hint)
        second.append(rating)
        wallhaven.append(second)

        colour_head = Gtk.Box(spacing=8)
        colour_head.append(filter_label("Main colour"))
        self.any_colour = Gtk.Button(label="Any colour")
        self.any_colour.add_css_class("flat")
        self.any_colour.add_css_class("caption")
        self.any_colour.connect("clicked", lambda *_: self._set_colour(""))
        colour_head.append(self.any_colour)
        self.palette = ColourPalette(self._set_colour)
        colours = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        colours.append(colour_head)
        colours.append(self.palette)
        wallhaven.append(colours)
        self.wallhaven_filters = wallhaven
        box.append(wallhaven)

        # -- MotionBGS
        motion = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        row = Adw.WrapBox(child_spacing=18, line_spacing=12)
        self.mode = _dropdown(MODES, query.mode, "What to browse")
        self.mode.connect(
            "notify::selected", lambda *_: self._changed(mode=MODES[self.mode.get_selected()][0])
        )
        row.append(_labelled("Browse", self.mode))
        self.genre = Gtk.Entry(placeholder_text="e.g. anime", valign=Gtk.Align.CENTER)
        self.genre.connect(
            "activate", lambda *_: self._changed(genre=self.genre.get_text().strip())
        )
        self.genre.set_tooltip_text("A MotionBGS genre; press Enter to browse it")
        self.genre_row = _labelled("Genre", self.genre)
        row.append(self.genre_row)
        self.quality = _dropdown(QUALITIES, query.quality, "Which file a download takes")
        self.quality.connect("notify::selected", lambda *_: self._set_quality())
        row.append(_labelled("Download quality", self.quality))
        motion.append(row)
        self.motion_hint = widgets.dim(
            "MotionBGS browses or searches; clear the search box to browse 4K, HD, "
            "latest, or by genre."
        )
        motion.append(self.motion_hint)
        self.motion_filters = motion
        box.append(motion)
        return box

    def _loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        box.append(Adw.Spinner(width_request=32, height_request=32))
        self._loading_label = widgets.dim("Loading Wallhaven…", wrap=False)
        box.append(self._loading_label)
        return box

    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()

        def add(name: str, callback: Callable[[StoreItemView], None]) -> None:
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))

            def run(_action: Gio.SimpleAction, value: GLib.Variant | None) -> None:
                item = self._item(value.get_string()) if value is not None else None
                if item is not None:
                    callback(item)

            action.connect("activate", run)
            group.add_action(action)

        add("preview", self.preview)
        add("download", lambda item: self.download(item))
        add("download-apply", lambda item: self.download(item, apply=True))
        add("apply", self.apply_item)
        add("show", self.show_in_library)
        add("like", self.more_like)
        add("site", self.open_site)
        workshop = Gio.SimpleAction.new("workshop", None)
        workshop.connect("activate", lambda *_: self.store.open_workshop())
        group.add_action(workshop)
        self.widget.insert_action_group("store", group)
        self._more.insert_action_group("store", group)

    def _install_shortcuts(self) -> None:
        """Ctrl+A picks this page, Ctrl+D downloads the picks (the classic Store's).

        Local to the page: in the search box Ctrl+A still selects its text.
        """
        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.LOCAL)

        def bind(trigger: str, work: Callable[[], None]) -> None:
            def run(_widget: Gtk.Widget, _arguments: GLib.Variant | None) -> bool:
                work()
                return True

            shortcuts.add_shortcut(
                Gtk.Shortcut(
                    trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                    action=Gtk.CallbackAction.new(run),
                )
            )

        bind("<Control>a", self.select_all)
        bind("<Control>d", self.download_picked)
        self.widget.add_controller(shortcuts)

    # -- Page API ------------------------------------------------------------------------
    def title_widget(self) -> Gtk.Widget:
        return self._title_box

    def header_end(self) -> list[Gtk.Widget]:
        return [self._more]

    def focus_search(self) -> bool:
        self.search.grab_focus()
        return True

    def activate(self, argument: str | None) -> None:
        """Shown: the first time, the opening search goes out; the key is re-asked each time."""
        self.store.refresh_facts()
        provider = (argument or "").casefold()
        if provider in PROVIDER and provider != self.query.provider:
            self._quiet = True
            self._providers.set_active_name(provider)
            self._quiet = False
            self.query = replace(self.query, provider=provider)
            self._sync_controls()
            if self._started:
                self.refresh()
        if not self._started:
            self._started = True
            self.refresh()
        elif (self.loading or self.fetching) and not self.store.searching():
            # Shown again while still waiting, but the answer will never come:
            # the adapter was closed while it was out. Ask again.
            self.refresh()

    # -- searching ---------------------------------------------------------------------
    def refresh(self) -> None:
        """A new search for the query as the controls say it."""
        self._sync_controls()
        if not self._started:
            return
        self._generation += 1
        generation = self._generation
        self.pages.reset()
        self.loading, self.fetching = True, False
        self.problem = None
        self._clear_cards()
        self.picked.clear()  # a new search forgets the selection, as the classic one did
        self._update_selection_bar()
        provider = PROVIDER[self.query.provider].title
        searching = bool(self.query.text.strip())
        self._loading_label.set_label(
            f"Searching {provider}…" if searching else f"Loading {provider}…"
        )
        self.stack.set_visible_child_name("loading")
        self._update_pager()
        self.store.search_store(self.query, 1, lambda answer: self._answered(generation, 1, answer))

    def _answered(self, generation: int, page: int, answer: StoreResults | StoreProblem) -> None:
        if generation != self._generation:
            return  # a newer search owns the grid now
        if page == 1:
            self.loading = False
        else:
            self.fetching = False
        if isinstance(answer, StoreProblem):
            if page == 1:
                self._show_problem(answer)
            else:
                # Keep every page reached; stop offering a Next that fails.
                self.pages.stop()
                self.state.toast(answer.message)
                self._update_pager()
            return
        before = self.pages.pages
        self.pages.add(answer, page)
        if page == 1:
            if not self.pages.pages:
                self._show_empty()
                return
            self._materialize(0)
        elif self.pages.pages > before:
            self._materialize(before, focus=True)
        else:
            self._update_pager()
        scene, self._pending_scene = self._pending_scene, ""
        if scene:
            self._scene(scene)

    def show_previous(self) -> None:
        if self.pages.can_previous and not self.loading:
            self._materialize(self.pages.index - 1, focus=True)

    def show_next(self) -> None:
        """The next kept page, or the provider's next page when every kept one is seen."""
        if self.loading or self.fetching:
            return
        if self.pages.next_is_kept:
            self._materialize(self.pages.index + 1, focus=True)
            return
        if not self.pages.has_more:
            return
        self.fetching = True
        self._update_pager()
        generation = self._generation
        page = self.pages.provider_page + 1
        self.store.search_store(
            self.query, page, lambda answer: self._answered(generation, page, answer)
        )

    def _materialize(self, index: int, *, focus: bool = False) -> None:
        """Make cards for page ``index`` only; the results stay records."""
        self.pages.show(index)
        self._clear_cards()
        for item in self.pages.current:
            card = StoreCard(item, self, CARD_WIDTH)
            self.cards[item.id] = card
            self._sync_card(card)
            if self.select_mode:
                card.set_selectable(True)
                card.set_checked(item.id in self.picked)
            self.flow.append(card)
        self._heading.set_label(self._heading_text())
        self._count.set_label(self.pages.summary())
        self.stack.set_visible_child_name("results")
        self._scroller.get_vadjustment().set_value(0)
        self._update_pager()
        self._update_selection_bar()
        if self.dialog is not None:
            self.dialog.items = list(self.pages.current)
        # Only this page's pictures; a page turned past drops its queue.
        self.store.item_pictures(self.pages.current, self._on_picture)
        if focus and self.pages.current:
            first = self.cards.get(self.pages.current[0].id)
            if first is not None:
                first.grab_focus()

    def _clear_cards(self) -> None:
        menu = self._menu
        if menu is not None and menu.get_parent() is not None:
            menu.popdown()
            menu.unparent()
        self.flow.remove_all()
        self.cards = {}
        self._pictures = {}

    def _on_picture(self, item: StoreItemView, picture: Gdk.Paintable) -> None:
        self._pictures[item.id] = picture
        card = self.cards.get(item.id)
        if card is not None and card.item is item:
            card.set_picture(picture)
        if self.dialog is not None and self.dialog.item.id == item.id:
            self.dialog.set_picture(picture)

    def _update_pager(self) -> None:
        shown = self.pages.pages > 0 and self.stack.get_visible_child_name() == "results"
        self.page_label.set_label(self.pages.label())
        self.previous.set_sensitive(self.pages.can_previous and not self.loading)
        self.next.set_sensitive(self.pages.can_next and not self.loading and not self.fetching)
        self._fetch_spinner.set_visible(self.fetching)
        self.next.set_tooltip_text("Loading the next page…" if self.fetching else "Next page")
        for widget in (self.previous, self.page_label, self.next):
            widget.set_visible(shown)
        self.queue_label.set_label(self.store.downloads.text)
        self._pager.set_visible(shown or bool(self.store.downloads.text))

    def _heading_text(self) -> str:
        query = self.query
        text = query.text.strip()
        if text.startswith("like:"):
            return (
                f"More like {quoted(self._like_title)}"
                if self._like_title
                else "Similar wallpapers"
            )
        if text:
            return f"Results for {quoted(text)}"
        if query.provider == MOTIONBGS:
            if query.mode == "genre":
                return f"Live wallpapers in {quoted(query.genre)}"
            return {
                "4k": "4K live wallpapers",
                "hd": "HD live wallpapers",
            }.get(query.mode, "Latest live wallpapers")
        if query.sort == "toplist":
            return f"Popular {option(TOP_RANGES, query.top_range, 2)}"
        return {
            "date_added": "Latest",
            "hot": "Hot right now",
            "views": "Most viewed",
            "favorites": "Most favorited",
            "random": "Random picks",
        }.get(query.sort, "Wallpapers")

    def _show_empty(self) -> None:
        _clear(self._empty_buttons)
        text = self.query.text.strip()
        if text:
            self._empty.set_title(f"No results for {quoted(text)}")
            self._empty.set_description(
                "Try other words" + (" or fewer filters" if self.filters_active() else "")
            )
            clear = Gtk.Button(label="Clear search")
            clear.add_css_class("pill")
            clear.connect("clicked", lambda *_: self.set_text(""))
            self._empty_buttons.append(clear)
        else:
            self._empty.set_title("Nothing matches these filters")
            self._empty.set_description("Try a different size, ratio or colour")
        if self.filters_active():
            reset = Gtk.Button(label="Reset filters")
            reset.add_css_class("pill")
            reset.connect("clicked", lambda *_: self.reset_filters())
            self._empty_buttons.append(reset)
        self._count.set_label("")
        self.stack.set_visible_child_name("empty")
        self._update_pager()

    def _show_problem(self, problem: StoreProblem) -> None:
        self.problem = problem
        _clear(self._error_buttons)
        provider = PROVIDER[self.query.provider]
        retry = Gtk.Button(label="Try again")
        retry.add_css_class("pill")
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda *_: self.refresh())
        message = problem.message
        if problem.kind == "rate-limit":
            self._error.set_icon_name("preferences-system-time-symbolic")
            self._error.set_title(f"{provider.title} needs a short break")
            self._error.set_description(f"Too many searches in a row. {message}")
            self._error_buttons.append(retry)
        elif problem.kind == "challenge":
            self._error.set_icon_name("channel-secure-symbolic")
            self._error.set_title(f"{provider.title} wants a browser check")
            self._error.set_description(
                f"Open {provider.site} in your browser once, then try again"
            )
            self._error_buttons.append(retry)
        elif problem.kind in ("validation", "credential", "invalid-request"):
            self._error.set_icon_name("dialog-information-symbolic")
            self._error.set_title("That search can't be made")
            self._error.set_description(message)
            if self.filters_active():
                reset = Gtk.Button(label="Reset filters")
                reset.add_css_class("pill")
                reset.connect("clicked", lambda *_: self.reset_filters())
                self._error_buttons.append(reset)
        else:
            self._error.set_icon_name("network-offline-symbolic")
            self._error.set_title(f"Can't reach {provider.title}")
            self._error.set_description(message)
            self._error_buttons.append(retry)
            library = Gtk.Button(label="Go to Library")
            library.add_css_class("pill")
            library.connect("clicked", lambda *_: self.state.navigate("library"))
            self._error_buttons.append(library)
        self._count.set_label("")
        self.stack.set_visible_child_name("error")
        self._update_pager()

    # -- the controls ---------------------------------------------------------------------
    def _changed(self, **fields: object) -> None:
        if self._quiet:
            return
        self.query = replace(self.query, **fields)  # type: ignore[arg-type]
        self.refresh()

    def _on_provider(self, group: Adw.ToggleGroup, _param: object) -> None:
        provider = group.get_active_name()
        if self._quiet or provider not in PROVIDER or provider == self.query.provider:
            return
        self.query = replace(self.query, provider=provider)
        self.refresh()

    def _on_text(self, text: str, force: bool = False) -> None:
        text = text.strip()
        if text == self.query.text and not force:
            return
        self.set_text(text, typed=True)

    def set_text(self, text: str, *, typed: bool = False, like: str = "") -> None:
        """Search for ``text``: relevance first, unless the person chose a sort."""
        sort = self.query.sort
        if not self._sort_chosen:
            sort = "relevance" if text else "toplist"
        elif not text and sort == "relevance":
            sort = "toplist"
        self._like_title = like
        if not typed and self.search.get_text().strip() != text:
            self._quiet = True
            self.search.set_text(text)
            self._quiet = False
        self.query = replace(self.query, text=text, sort=sort)
        self.refresh()

    def _on_sort(self, dropdown: Gtk.DropDown, _param: object) -> None:
        if self._quiet:
            return
        self._sort_chosen = True
        self._changed(sort=SORTS[dropdown.get_selected()][0])

    def _set_colour(self, colour: str) -> None:
        self.palette.set_colour(colour)
        self._changed(colour=colour)

    def _set_quality(self) -> None:
        """MotionBGS's quality picks the file a download takes; the results stay."""
        if not self._quiet:
            self.query = replace(self.query, quality=QUALITIES[self.quality.get_selected()][0])
            self._sync_controls()

    def _on_filters_toggled(self, button: Gtk.ToggleButton) -> None:
        self.revealer.set_reveal_child(button.get_active())
        self._filters_content.set_icon_name(
            "pan-up-symbolic" if button.get_active() else "pan-down-symbolic"
        )
        button.set_tooltip_text("Hide the filters" if button.get_active() else "Show the filters")
        if button.get_active():
            self.store.refresh_facts()  # a key saved meanwhile is noticed here too

    def filters_active(self) -> bool:
        query, default = self.query, StoreQuery(provider=self.query.provider)
        if query.provider == WALLHAVEN:
            return (
                query.size != default.size
                or query.ratio != default.ratio
                or bool(query.colour)
                or query.categories != default.categories
                or query.purity != default.purity
            )
        return query.mode != default.mode or bool(query.genre) or bool(query.quality)

    def reset_filters(self) -> None:
        """Back to the default filters of the provider shown."""
        default = StoreQuery()
        if self.query.provider == WALLHAVEN:
            self.query = replace(
                self.query,
                size=default.size,
                ratio=default.ratio,
                colour="",
                categories=default.categories,
                purity=default.purity,
            )
        else:
            self.query = replace(self.query, mode=default.mode, genre="", quality="")
        self.refresh()

    def _sync_controls(self) -> None:
        """Every control shows the query (without searching again)."""
        query = self.query
        quiet, self._quiet = self._quiet, True
        try:
            wallhaven = query.provider == WALLHAVEN
            self._providers.set_active_name(query.provider)
            self.wallhaven_filters.set_visible(wallhaven)
            self.motion_filters.set_visible(not wallhaven)
            self.sort.set_visible(wallhaven)
            self.sort.set_selected(_index(SORTS, query.sort))
            self.search.set_placeholder_text(f"Search {PROVIDER[query.provider].title}")
            self.top_range.set_selected(_index(TOP_RANGES, query.top_range))
            self.top_range_row.set_visible(query.uses_range)
            self.reverse.set_active(query.reverse)
            self.reverse.set_visible(query.can_reverse)
            self.size.set_selected(_index(SIZES, query.size))
            self.ratio.set_selected(_index(RATIOS, query.ratio))
            self.palette.set_colour(query.colour)
            self.any_colour.set_sensitive(bool(query.colour))
            self.categories.set_chosen(query.categories)
            self.purity.set_chosen(query.purity)
            self.mode.set_selected(_index(MODES, query.mode))
            self.genre.set_text(query.genre)
            self.quality.set_selected(_index(QUALITIES, query.quality))
            # MotionBGS takes a query or a browse mode, never both.
            searching = bool(query.text.strip())
            self.mode.set_sensitive(not searching)
            self.genre.set_sensitive(not searching)
            self.genre_row.set_visible(query.mode == "genre")
            self.motion_hint.set_visible(searching)
            active = self.filters_active()
            self._reset.set_visible(active)
            if active:
                self.filters_button.add_css_class("accent")
            else:
                self.filters_button.remove_css_class("accent")
        finally:
            self._quiet = quiet

    def _sync_facts(self) -> None:
        """The key's limitation and the Workshop, as the adapter last read them."""
        blocked = self.store.nsfw_blocked
        self.purity.set_enabled("NSFW", not blocked, blocked)
        self.key_hint.set_visible(bool(blocked))
        self.key_hint_label.set_label(blocked)
        self._more.set_visible(self.store.has_workshop)
        if blocked and "NSFW" in self.query.purity:
            # NSFW can't be had: drop it rather than search for an empty grid.
            self.query = replace(
                self.query, purity=(self.query.purity - {"NSFW"}) or frozenset({"SFW"})
            )
            self.refresh()

    def _sync_destination(self) -> None:
        destination = self.store.download_destination
        blocked = self.store.download_blocked()
        provider = PROVIDER[self.query.provider].title
        self._dest_label.set_label(
            f"Saves to {destination}" if destination else "No library folder yet"
        )
        self._dest.set_tooltip_text(
            blocked
            or f"Downloads go to Wall-in-One/{provider} in your first library folder, {destination}"
        )

    # -- what changes elsewhere --------------------------------------------------------------
    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic == "store":
            self._sync_all_cards()
            self._sync_facts()
            self._update_pager()
        elif topic == "library":
            self._sync_all_cards()
        elif topic == "settings":
            self._sync_destination()
            self._sync_all_cards()
            self._sync_facts()
            if self._started:
                self.store.refresh_facts()  # a key saved meanwhile is noticed

    def _busy(self, item: StoreItemView) -> str:
        busy = self.store.downloads.busy
        if item.id not in busy:
            return ""
        return "downloading" if busy[0] == item.id else "waiting"

    def _sync_card(self, card: StoreCard) -> None:
        item = card.item
        card.update(
            in_library=self.store.in_library(item),
            busy=self._busy(item),
            apply_blocked=self.store.apply_item_blocked(item),
        )
        if not card.can_pick and self.picked.pop(item.id, None) is not None:
            card.set_checked(False)

    def _sync_all_cards(self) -> None:
        for card in self.cards.values():
            self._sync_card(card)
        self._sync_dialog()
        self._update_selection_bar()

    def _sync_dialog(self) -> None:
        dialog = self.dialog
        if dialog is None:
            return
        item = dialog.item
        dialog.update(
            in_library=self.store.in_library(item),
            busy=self._busy(item),
            apply_blocked=self.store.apply_item_blocked(item),
            download_blocked=self.store.download_blocked(),
        )

    # -- what cards and the dialog ask (StoreController) ------------------------------------
    def _item(self, item_id: str) -> StoreItemView | None:
        for item in self.pages.current:
            if item.id == item_id:
                return item
        if self.dialog is not None and self.dialog.item.id == item_id:
            return self.dialog.item
        return self.picked.get(item_id)

    def open_item(self, item: StoreItemView) -> None:
        if self.select_mode:
            self.toggle_pick(item)
        else:
            self.preview(item)

    def preview(self, item: StoreItemView) -> None:
        picture = self._pictures.get(item.id)
        if self.dialog is None:
            quality = self.query.quality if item.provider == MOTIONBGS else ""
            self.dialog = StorePreview(self, self.pages.current, item, quality=quality)
            root = self.widget.get_root()
            self.dialog.present(root if isinstance(root, Gtk.Widget) else None)
            if picture is not None:
                self.dialog.set_picture(picture)
        else:
            self.dialog.show_item(item, picture)
        self._sync_dialog()
        self.store.item_detail(item, lambda detail: self._detail_arrived(item, detail))

    def _detail_arrived(self, item: StoreItemView, detail: StoreDetail) -> None:
        dialog = self.dialog
        if dialog is not None and dialog.item.id == item.id and dialog.detail is None:
            dialog.show_detail(detail)
            self._sync_dialog()

    def dialog_closed(self, dialog: StorePreview) -> None:
        if self.dialog is dialog:
            self.dialog = None

    def download(
        self, item: StoreItemView, *, apply: bool = False, variant: str | None = None
    ) -> None:
        if apply and self.store.in_library(item):
            self.apply_item(item)
            return
        if variant is None:
            variant = self.query.quality if item.provider == MOTIONBGS else ""
        self.picked.pop(item.id, None)
        self.store.download(item, variant=variant, then="apply" if apply else "")
        self._update_selection_bar()

    def apply_item(self, item: StoreItemView) -> None:
        if not self.store.in_library(item):
            self.download(item, apply=True)
            return
        self.store.apply_item(item)

    def show_in_library(self, item: StoreItemView) -> None:
        self.store.show_item(item)

    def open_site(self, item: StoreItemView) -> None:
        self.store.open_site(item)

    def search_for(self, text: str, *, like: str = "") -> None:
        if self.dialog is not None:
            self.dialog.close()
        if text.startswith("like:") and self.query.provider != WALLHAVEN:
            self.query = replace(self.query, provider=WALLHAVEN)
        self.set_text(text, like=like)

    def more_like(self, item: StoreItemView) -> None:
        if item.provider == WALLHAVEN:
            identifier = item.id.partition(":")[2] or item.id
            self.search_for(f"like:{identifier}", like=item.title)

    def search_colour(self, colour: str) -> None:
        if self.dialog is not None:
            self.dialog.close()
        self.query = replace(self.query, provider=WALLHAVEN, colour=colour)
        self.refresh()

    def popup_menu(self, card: StoreCard, x: float, y: float) -> None:
        item = card.item
        menu = Gio.Menu()
        first = Gio.Menu()
        first.append("Preview", f"store.preview::{item.id}")
        if card.in_library:
            if not self.store.apply_item_blocked(item):
                first.append("Apply", f"store.apply::{item.id}")
                first.append("Show in Library", f"store.show::{item.id}")
        elif not card.busy and not self.store.download_blocked():
            first.append("Download", f"store.download::{item.id}")
            first.append("Download & apply", f"store.download-apply::{item.id}")
        menu.append_section(None, first)
        second = Gio.Menu()
        if item.provider == WALLHAVEN:
            second.append("More like this", f"store.like::{item.id}")
        site = PROVIDER[item.provider].site if item.provider in PROVIDER else "its site"
        second.append(f"Open on {site}", f"store.site::{item.id}")
        menu.append_section(None, second)
        popover = self._menu
        if popover is None:
            popover = Gtk.PopoverMenu.new_from_model(menu)
            popover.set_has_arrow(False)
            self._menu = popover
        else:
            popover.set_menu_model(menu)
        if popover.get_parent() is not card.frame:
            if popover.get_parent() is not None:
                popover.unparent()
            popover.set_parent(card.frame)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    # -- selection mode -----------------------------------------------------------------
    def _on_select_mode(self, button: Gtk.ToggleButton) -> None:
        self.select_mode = button.get_active()
        if not self.select_mode:
            self.picked.clear()
        for card in self.cards.values():
            card.set_selectable(self.select_mode)
        self._update_selection_bar()

    def _enter_select_mode(self) -> None:
        if not self.select_mode:
            self._select.set_active(True)

    def toggle_pick(self, item: StoreItemView) -> None:
        card = self.cards.get(item.id)
        if card is not None and not card.can_pick:
            return
        self._enter_select_mode()
        if item.id in self.picked:
            del self.picked[item.id]
        else:
            self.picked[item.id] = item
        if card is not None:
            card.set_checked(item.id in self.picked)
        self._update_selection_bar()

    def select_all(self) -> None:
        """Pick this page's downloadable results; picks on other pages stay."""
        if not self.cards:
            return
        self._enter_select_mode()
        for item in self.pages.current:
            card = self.cards.get(item.id)
            if card is not None and card.can_pick:
                self.picked[item.id] = item
                card.set_checked(True)
        self._update_selection_bar()

    def download_picked(self) -> None:
        """Queue every pick (one downloads at a time) and let go of the selection."""
        picked = list(self.picked.values())
        if not picked:
            return
        self.picked.clear()
        self._select.set_active(False)
        for item in picked:
            self.download(item)

    def _update_selection_bar(self) -> None:
        count = len(self.picked)
        self._selection_label.set_label(
            f"{count} selected" if count else "Click results to pick them"
        )
        self._batch.set_label(f"Download {count}" if count else "Download")
        self._batch.set_sensitive(bool(count) and not self.store.download_blocked())
        self._action_bar.set_revealed(self.select_mode)

    # -- the prototype's screenshot scenes -------------------------------------------------
    def demo(self, scene: str) -> None:
        """Start every scene from a clean page; the scene runs once the results are in."""
        self._reset_demo()
        if scene not in ("", "reset"):
            self._pending_scene = scene

    def _scene(self, scene: str) -> None:
        what, _, argument = scene.partition(":")
        if what == "search":
            self.set_text(argument)
        elif what == "provider":
            self._providers.set_active_name(argument.casefold())
        elif what == "downloading":
            for item in [i for i in self.pages.current if self.cards[i.id].can_pick][:2]:
                self.download(item)
        elif what == "preview":
            shown = self._item(argument) or (self.pages.current[0] if self.pages.current else None)
            if shown is not None:
                self.preview(shown)
        elif what in ("error", "rate-limit", "challenge"):
            if what == "challenge":
                self.query = replace(self.query, provider=MOTIONBGS)
                self._sync_controls()
            kind = "transport" if what == "error" else what
            self._show_problem(StoreProblem(kind, "The site did not answer."))
        elif what == "loading":
            self.stack.set_visible_child_name("loading")
        elif what == "filters":
            self.filters_button.set_active(True)
        elif what == "select":
            for item in [i for i in self.pages.current if self.cards[i.id].can_pick][:3]:
                self.toggle_pick(item)
        elif what == "saved":
            target = next((i for i in self.pages.current if self.cards[i.id].can_pick), None)
            if target is not None:
                self.download(target)
        elif what == "like":
            source = self._item(argument)
            if source is not None:
                self.more_like(source)
        elif what == "active":
            self.query = replace(self.query, size="2160", ratio="16x9", colour="0066cc")
            self.refresh()
        elif what == "empty":
            self.set_text(argument or "zebra crossing")
        elif what == "hover":
            card = self.cards.get(argument) or next(iter(self.cards.values()), None)
            if card is not None:
                card.add_css_class("force-hover")
        elif what == "menu":
            card = self.cards.get(argument) or next(iter(self.cards.values()), None)
            if card is not None:
                self.popup_menu(card, 120, 70)
        elif what == "nsfw-key":
            self.filters_button.set_active(True)

    def _reset_demo(self) -> None:
        if self.dialog is not None:
            self.dialog.force_close()
        self._select.set_active(False)
        self.filters_button.set_active(False)
        self._sort_chosen = False
        self._quiet = True
        self.search.set_text("")
        self._quiet = False
        self.query = StoreQuery()
        self._started = True
        self.refresh()


def _clear(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        box.remove(child)
        child = box.get_first_child()


def create(state: AppState) -> Page:
    """The Store over ``state.store_access``, or its placeholder when there is none."""
    store = state.store_access
    if store is None:
        return not_ported("store", "Store", "folder-download-symbolic")(state)
    return StorePage(state, store)
