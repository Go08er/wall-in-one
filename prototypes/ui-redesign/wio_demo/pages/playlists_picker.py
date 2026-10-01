"""“Add wallpapers” for one playlist: search, filter by type, pick several, add.

Picks are numbered in the order you click them, which is the order they are
appended. Wallpapers already in the playlist carry a small badge but can be
added again (a playlist may repeat a wallpaper).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from .. import data, ui
from ..catalog import KIND_LABEL
from ..models import Playlist, Wallpaper
from ..ui import CardGrid

PICKER_CSS = """
.pl-picker-filters { padding: 4px 12px 8px 12px; }
.pl-picker-tray { padding: 8px 12px; border-top: 1px solid alpha(currentColor, 0.10); }
.pl-tray-more { font-weight: 700; font-size: 0.85em; padding: 0 6px; }
"""


def _inert(widget: Gtk.Widget) -> None:
    """Card overlay buttons (the favorite star) become plain indicators here,
    so a click anywhere on the picture toggles the pick."""
    child = widget.get_first_child()
    while child:
        if isinstance(child, Gtk.Button):
            child.set_can_target(False)
            child.set_focusable(False)
        _inert(child)
        child = child.get_next_sibling()


class PlaylistPicker(Adw.Dialog):
    def __init__(self, state, playlist: Playlist, on_add: Callable[[list[str]], None]) -> None:
        super().__init__(content_width=940, content_height=700, width_request=360, height_request=420)
        self.state, self.playlist, self._on_add = state, playlist, on_add
        self._picked: list[str] = []
        self._kind = "all"
        self._query = ""
        self._cards: dict[str, ui.WallpaperCard] = {}
        self._badges: dict[str, ui.PickBadge] = {}
        self.set_title(f"Add to “{playlist.name}”")

        # -- header: Cancel · title · Add N ---------------------------------
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self._title = Adw.WindowTitle(title=f"Add to “{playlist.name}”", subtitle="Choose wallpapers")
        header.set_title_widget(self._title)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self._add = Gtk.Button(label="Add", sensitive=False)
        self._add.add_css_class("suggested-action")
        self._add.set_tooltip_text("Add the picked wallpapers to the end (Ctrl+Enter)")
        self._add.connect("clicked", lambda *_: self._commit())
        header.pack_end(self._add)

        # -- filters ------------------------------------------------------------
        self._filters = Gtk.Box(spacing=8)
        self._filters.add_css_class("pl-picker-filters")
        self.search = Gtk.SearchEntry(placeholder_text="Search wallpapers, tags", hexpand=True)
        self.search.connect("search-changed", self._on_search)
        self.search.set_key_capture_widget(self)
        self._filters.append(self.search)
        kinds_row = Gtk.Box(spacing=8)
        self._kinds = Adw.ToggleGroup()
        for key, label in (("all", "All"), ("still", "Images"), ("video", "Videos"), ("scene", "Scenes")):
            self._kinds.add(Adw.Toggle(name=key, label=label))
        self._kinds.set_active_name("all")
        self._kinds.connect("notify::active-name", self._on_kind)
        kinds_row.append(self._kinds)
        self._count = Gtk.Label(hexpand=True, xalign=1)
        self._count.add_css_class("dimmed")
        self._count.add_css_class("numeric")
        kinds_row.append(self._count)
        self._filters.append(kinds_row)

        # -- grid ------------------------------------------------------------------
        self.grid = CardGrid(
            min_width=176, valign=Gtk.Align.START, margin_start=12, margin_end=12, margin_top=4, margin_bottom=16
        )
        in_playlist = Counter(playlist.entries)
        for wallpaper in state.wallpapers:
            self.grid.append(self._card(wallpaper, in_playlist[wallpaper.id]))
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.grid)
        empty = Adw.StatusPage(
            icon_name="edit-find-symbolic", title="No wallpapers match", description="Try another word or type"
        )
        empty.add_css_class("compact")
        self._stack = Gtk.Stack()
        self._stack.add_named(scroller, "grid")
        self._stack.add_named(empty, "empty")

        # -- tray: what you picked, in order ---------------------------------------
        tray = Gtk.Box(spacing=10)
        tray.add_css_class("pl-picker-tray")
        self._strip = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        tray.append(self._strip)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self._picked_label = Gtk.Label(xalign=0)
        self._picked_label.add_css_class("heading")
        words.append(self._picked_label)
        order = Gtk.Label(label="Added to the end, in this order", xalign=0, ellipsize=3)
        order.add_css_class("dimmed")
        order.add_css_class("caption")
        words.append(order)
        tray.append(words)
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_: self._clear())
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
                Gtk.CallbackAction.new(lambda *_: (self._commit(), True)[1]),
            )
        )
        self.add_controller(shortcuts)
        self.connect("map", lambda *_: self.search.grab_focus())
        self._update()

    # -- cards ---------------------------------------------------------------------
    def _card(self, wallpaper: Wallpaper, already: int) -> ui.WallpaperCard:
        card = ui.WallpaperCard(wallpaper, width=184, on_open=lambda w: self.toggle(w.id))
        _inert(card.frame)
        if already:
            text = "In playlist" if already == 1 else f"In playlist ×{already}"
            badge = ui.pill(text, "object-select-symbolic", "on-image")
            badge.set_halign(Gtk.Align.START)
            badge.set_valign(Gtk.Align.END)
            badge.set_margin_start(8)
            badge.set_margin_bottom(8)
            badge.set_tooltip_text("Already in this playlist — you can add it again")
            card.frame.add_overlay(badge)
        pick = ui.PickBadge(halign=Gtk.Align.END, valign=Gtk.Align.END)
        pick.set_margin_end(8)
        pick.set_margin_bottom(8)
        pick.add_css_class("hover-only")
        card.frame.add_overlay(pick)
        card.set_tooltip_text(f"{wallpaper.name} — click to pick")
        self._cards[wallpaper.id] = card
        self._badges[wallpaper.id] = pick
        return card

    def toggle(self, wid: str) -> None:
        if wid in self._picked:
            self._picked.remove(wid)
        else:
            self._picked.append(wid)
        self._update()

    def pick(self, wids: list[str]) -> None:
        for wid in wids:
            if wid not in self._picked and wid in self._cards:
                self._picked.append(wid)
        self._update()

    def _clear(self) -> None:
        self._picked.clear()
        self._update()

    def _update(self) -> None:
        for wid, card in self._cards.items():
            badge = self._badges[wid]
            picked = wid in self._picked
            card.set_selected(picked)
            badge.set_picked(picked, str(self._picked.index(wid) + 1) if picked else "")
            (badge.remove_css_class if picked else badge.add_css_class)("hover-only")
        count = len(self._picked)
        self._add.set_label(f"Add {count}" if count else "Add")
        self._add.set_sensitive(bool(count))
        self._title.set_subtitle("Choose wallpapers")  # the tray and "Add N" already count
        self._picked_label.set_label("1 wallpaper picked" if count == 1 else f"{count} wallpapers picked")
        child = self._strip.get_first_child()
        while child:
            self._strip.remove(child)
            child = self._strip.get_first_child()
        for wid in self._picked[:6]:
            self._strip.append(ui.thumbnail(data.BY_ID[wid], 48, 27, 5))
        if count > 6:
            more = Gtk.Label(label=f"+{count - 6}")
            more.add_css_class("pl-tray-more")
            more.add_css_class("dimmed")
            self._strip.append(more)
        self._view.set_reveal_bottom_bars(bool(count))
        self._update_count()

    # -- filtering -----------------------------------------------------------------
    def _visible(self, wallpaper: Wallpaper) -> bool:
        if self._kind != "all" and wallpaper.kind != self._kind:
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
            ).lower()
            return all(word in haystack for word in self._query.lower().split())
        return True

    def _refilter(self) -> None:
        for card in self.grid.cards():
            card.set_visible(self._visible(card.wallpaper))
        self._update_count()

    def _update_count(self) -> None:
        shown = sum(1 for w in self.state.wallpapers if self._visible(w))
        total = len(self.state.wallpapers)
        self._count.set_label(f"{shown} of {total}" if shown != total else f"{total} wallpapers")
        self._stack.set_visible_child_name("grid" if shown else "empty")

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._query = entry.get_text().strip()
        self._refilter()

    def _on_kind(self, group: Adw.ToggleGroup, _param) -> None:
        self._kind = group.get_active_name()
        self._refilter()

    # -- result --------------------------------------------------------------------
    def _commit(self) -> None:
        if not self._picked:
            return
        picked = list(self._picked)
        self.close()
        self._on_add(picked)
