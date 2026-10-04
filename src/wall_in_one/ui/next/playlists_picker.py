"""\u201cAdd wallpapers\u201d for one playlist: search, filter by type, pick several, add.

Picks are numbered in the order they are clicked, which is the order they
are added, all in one save. Wallpapers already in the playlist carry a badge
but can be added again (a playlist may repeat a wallpaper). One that
playback skips can't be picked; it says why.

Matching and order are the adapter's (`AppState.library_query`, the
Library's own); only a page of cards is built at a time, like the Library,
so a library of thousands builds 72 cards, with "Show more" for the next.
Filters are inline (``DECISIONS.md``: no configuration in a popover).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gtk, Pango

from wall_in_one.ui.grid import MEDIA_PAGE_SIZE
from wall_in_one.ui.next import widgets
from wall_in_one.ui.next.catalog import quoted
from wall_in_one.ui.next.state import AppState, WallpaperView

PAGE_SIZE: Final = MEDIA_PAGE_SIZE
KINDS: Final = (("all", "All"), ("still", "Images"), ("video", "Videos"), ("scene", "Scenes"))
_CSS: Final = """
.pl-picker-filters { padding: 4px 12px 8px 12px; }
.pl-picker-tray { padding: 8px 12px; border-top: 1px solid alpha(currentColor, 0.10); }
.pl-tray-more { font-weight: 700; font-size: 0.85em; padding: 0 6px; }
"""
_styled = False


def _inert(widget: Gtk.Widget) -> None:
    """Card overlay buttons (the favorite star) become plain indicators here,
    so a click anywhere on the picture toggles the pick."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            child.set_can_target(False)
            child.set_focusable(False)
        _inert(child)
        child = child.get_next_sibling()


class PlaylistPicker(Adw.Dialog):
    """Pick wallpapers for ``name``; ``on_add`` gets their ids, in pick order."""

    def __init__(
        self,
        state: AppState,
        name: str,
        already: Sequence[str],
        on_add: Callable[[list[str]], None],
    ) -> None:
        super().__init__(
            content_width=940, content_height=700, width_request=360, height_request=420
        )
        global _styled
        if not _styled:
            widgets.add_css(_CSS)
            _styled = True
        self.state = state
        self._on_add = on_add
        self._already = Counter(already)
        self._picked: list[str] = []
        self._kind = "all"
        self._query = ""
        sorts = list(state.library_sorts())
        self._sort = sorts[0][0] if sorts else ""
        self._limit = PAGE_SIZE
        self._matches: list[WallpaperView] = []
        self._cards: dict[str, widgets.WallpaperCard] = {}
        self._badges: dict[str, widgets.PickBadge] = {}
        self.set_title(f"Add to {quoted(name)}")

        # -- header: Cancel · title · Add N ---------------------------------
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(
            Adw.WindowTitle(title=f"Add to {quoted(name)}", subtitle="Choose wallpapers")
        )
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.add_button = Gtk.Button(label="Add", sensitive=False)
        self.add_button.add_css_class("suggested-action")
        self.add_button.set_tooltip_text("Add the picked wallpapers to the end (Ctrl+Enter)")
        self.add_button.connect("clicked", lambda *_: self.commit())
        header.pack_end(self.add_button)

        # -- filters, inline -------------------------------------------------
        self._filters = Gtk.Box(spacing=8)
        self._filters.add_css_class("pl-picker-filters")
        self.search = Gtk.SearchEntry(placeholder_text="Search wallpapers", hexpand=True)
        self.search.connect("search-changed", self._on_search)
        self.search.set_key_capture_widget(self)
        self._filters.append(self.search)
        kinds_row = Gtk.Box(spacing=8)
        self._kinds = Adw.ToggleGroup()
        for key, label in KINDS:
            self._kinds.add(Adw.Toggle(name=key, label=label))
        self._kinds.set_active_name("all")
        self._kinds.connect("notify::active-name", self._on_kind)
        kinds_row.append(self._kinds)
        self.count = Gtk.Label(hexpand=True, xalign=1)
        self.count.add_css_class("dimmed")
        self.count.add_css_class("numeric")
        kinds_row.append(self.count)
        self._filters.append(kinds_row)

        # -- grid, a page at a time -------------------------------------------
        self.grid = widgets.CardGrid(
            min_width=176,
            valign=Gtk.Align.START,
            margin_start=12,
            margin_end=12,
            margin_top=4,
            margin_bottom=8,
        )
        self.more = Gtk.Button(halign=Gtk.Align.CENTER, margin_bottom=16)
        self.more.add_css_class("pill")
        self.more.connect("clicked", lambda *_: self.show_more())
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        column.append(self.grid)
        column.append(self.more)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(column)
        empty = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="No wallpapers match",
            description="Try another word or type",
        )
        empty.add_css_class("compact")
        self._stack = Gtk.Stack()
        self._stack.add_named(scroller, "grid")
        self._stack.add_named(empty, "empty")

        # -- tray: what is picked, in order ---------------------------------------
        tray = Gtk.Box(spacing=10)
        tray.add_css_class("pl-picker-tray")
        self._strip = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        tray.append(self._strip)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.picked_label = Gtk.Label(xalign=0)
        self.picked_label.add_css_class("heading")
        words.append(self.picked_label)
        order = Gtk.Label(
            label="Added to the end, in this order", xalign=0, ellipsize=Pango.EllipsizeMode.END
        )
        order.add_css_class("dimmed")
        order.add_css_class("caption")
        words.append(order)
        tray.append(words)
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_: self.clear())
        tray.append(clear)

        self._view = Adw.ToolbarView()
        self._view.add_top_bar(header)
        self._view.add_top_bar(self._filters)
        self._view.set_content(self._stack)
        self._view.add_bottom_bar(tray)
        self._view.set_reveal_bottom_bars(False)
        self.set_child(self._view)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.add_setter(self._filters, "orientation", Gtk.Orientation.VERTICAL)
        self.add_breakpoint(narrow)

        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>Return"),
                Gtk.CallbackAction.new(self._commit_shortcut),
            )
        )
        self.add_controller(shortcuts)
        self.connect("map", lambda *_: self.search.grab_focus())
        self._refilter()

    # -- cards -----------------------------------------------------------------------
    def _card(self, wallpaper: WallpaperView) -> widgets.WallpaperCard:
        card = widgets.WallpaperCard(
            wallpaper,
            width=184,
            on_open=lambda chosen: self.toggle(chosen.id),
            **widgets.card_colors(self.state, wallpaper),
        )
        _inert(card.frame)
        already = self._already[wallpaper.id]
        if already:
            text = "In playlist" if already == 1 else f"In playlist \u00d7{already}"
            badge = widgets.pill(text, "object-select-symbolic", "on-image")
            badge.set_halign(Gtk.Align.START)
            badge.set_valign(Gtk.Align.END)
            badge.set_margin_start(8)
            badge.set_margin_bottom(8)
            badge.set_tooltip_text("Already in this playlist \u2014 you can add it again")
            card.frame.add_overlay(badge)
        if wallpaper.problem:
            card.set_tooltip_text(
                f"{wallpaper.name} \u2014 playback skips it, so it can\u2019t be added: "
                f"{wallpaper.problem}"
            )
        else:
            pick = widgets.PickBadge(halign=Gtk.Align.END, valign=Gtk.Align.END)
            pick.set_margin_end(8)
            pick.set_margin_bottom(8)
            pick.add_css_class("hover-only")
            card.frame.add_overlay(pick)
            self._badges[wallpaper.id] = pick
            card.set_tooltip_text(f"{wallpaper.name} \u2014 click to pick")
        self._cards[wallpaper.id] = card
        return card

    def toggle(self, wid: str) -> None:
        if not self.state.has_wallpaper(wid) or self.state.wallpaper(wid).problem:
            return
        if wid in self._picked:
            self._picked.remove(wid)
        else:
            self._picked.append(wid)
        self._update()

    def pick_wallpapers(self, wids: Sequence[str]) -> None:
        for wid in wids:
            if wid not in self._picked:
                self.toggle(wid)

    @property
    def picked(self) -> list[str]:
        return list(self._picked)

    def clear(self) -> None:
        self._picked.clear()
        self._update()

    def _update(self) -> None:
        for wid, card in self._cards.items():
            badge = self._badges.get(wid)
            picked = wid in self._picked
            card.set_selected(picked)
            if badge is not None:
                badge.set_picked(picked, str(self._picked.index(wid) + 1) if picked else "")
                if picked:
                    badge.remove_css_class("hover-only")
                else:
                    badge.add_css_class("hover-only")
        count = len(self._picked)
        self.add_button.set_label(f"Add {count}" if count else "Add")
        self.add_button.set_sensitive(bool(count))
        self.picked_label.set_label(
            "1 wallpaper picked" if count == 1 else f"{count} wallpapers picked"
        )
        while (child := self._strip.get_first_child()) is not None:
            self._strip.remove(child)
        for wid in self._picked[:6]:
            self._strip.append(widgets.thumbnail(self.state.wallpaper(wid), 48, 27, 5))
        if count > 6:
            more = Gtk.Label(label=f"+{count - 6}")
            more.add_css_class("pl-tray-more")
            more.add_css_class("dimmed")
            self._strip.append(more)
        self._view.set_reveal_bottom_bars(bool(count))

    # -- matching, a page at a time ----------------------------------------------------
    def _refilter(self) -> None:
        self._matches = list(
            self.state.library_query(
                kind=self._kind, favorites=False, text=self._query, sort=self._sort
            )
        )
        self._limit = PAGE_SIZE
        self._materialize()

    def _materialize(self) -> None:
        self.grid.remove_all()
        self._cards.clear()
        self._badges.clear()
        self._append(self._matches[: self._limit])

    def _append(self, wallpapers: Sequence[WallpaperView]) -> None:
        for wallpaper in wallpapers:
            self.grid.append(self._card(wallpaper))
        shown, total = len(self._cards), len(self._matches)
        self.more.set_visible(shown < total)
        self.more.set_label(f"Show {min(PAGE_SIZE, total - shown)} more · {shown} of {total}")
        everything = len(self.state.wallpapers)
        self.count.set_label(
            f"{total} of {everything}" if total != everything else f"{everything} wallpapers"
        )
        self._stack.set_visible_child_name("grid" if total else "empty")
        self._update()

    def show_more(self) -> None:
        """One more page of cards, without rebuilding the ones shown."""
        start = self._limit
        self._limit += PAGE_SIZE
        self._append(self._matches[start : self._limit])

    @property
    def card_ids(self) -> list[str]:
        """The wallpapers that have a card now, in order."""
        return list(self._cards)

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._query = entry.get_text().strip()
        self._refilter()

    def _on_kind(self, group: Adw.ToggleGroup, _param: object) -> None:
        self._kind = group.get_active_name() or "all"
        self._refilter()

    # -- result ----------------------------------------------------------------------
    def _commit_shortcut(self, *_args: object) -> bool:
        self.commit()
        return True

    def commit(self) -> None:
        if not self._picked:
            return
        picked = list(self._picked)
        self.close()
        self._on_add(picked)
