"""Palettes: browse, apply, duplicate and edit Noctalia's palettes; preview every scheme.

One dialog with two views, as the classic palette browser has:

* **Installed**: every palette Noctalia has, yours first, then built-in,
  community and legacy, searchable and listed a page at a time (fairly across
  origins, so a large community cache hides none), with the files that could
  not be read. Apply goes through Noctalia, the only path that covers
  built-ins; a legacy palette has to be duplicated first. Duplicate copies a
  palette into yours; only yours can be edited, in place, under its own name.
* **Schemes**: what each generator makes of one of your stills. "Use" makes
  one the default adaptive scheme (optionally Noctalia's too); "Save…" keeps
  it as a palette of yours.

No two palettes a person can pick share a name. A copy starts as "<name> copy"
(numbered when taken), and the editor checks the name as it is typed: Save
stays off, with the reason under the name, until no other palette has it.

The editor is pushed inside the dialog (`Adw.NavigationView`) rather than
stacked as a second dialog, and no configuration sits in a popover. There is
no delete or rename: the app has neither. The palette manager
(`PaletteManager`) does every read and write off the GTK thread.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk

from wall_in_one.ui.next import widgets
from wall_in_one.ui.next.settings_state import (
    PALETTE_ORIGINS,
    PALETTE_PAGE_SIZE,
    PALETTE_ROLES,
    SCHEMES,
    PaletteDraft,
    PaletteItem,
    PaletteManager,
    SchemePreview,
    SettingsAccess,
)
from wall_in_one.ui.next.state import AppState

#: Most unreadable files listed before the rest are summarized.
MAX_SKIPPED_ROWS: Final = 24
_ORIGIN_EMPTY: Final = {
    "custom": "Duplicate any palette to start one",
    "builtin": "This build of Noctalia reports no built-in palettes",
    "community": "Browse community palettes in Noctalia to cache some",
    "legacy": "Nothing left over from an older Noctalia",
}


def rounded(cr: cairo.Context[Any], x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class PalettePreview(Gtk.DrawingArea):
    """A tiny app mock-up painted with all fourteen colours, so edits show at once."""

    def __init__(self, values: Mapping[str, str]) -> None:
        super().__init__()
        self.values = values
        self.set_content_height(116)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def _draw(
        self, _area: Gtk.DrawingArea, cr: cairo.Context[Any], width: int, height: int
    ) -> None:
        def use(key: str, alpha: float = 1.0) -> None:
            color = widgets.rgba(self.values.get(key, "#888888"))
            cr.set_source_rgba(color.red, color.green, color.blue, alpha)

        rounded(cr, 0.5, 0.5, width - 1, height - 1, 12)
        use("surface")
        cr.fill_preserve()
        use("outline", 0.8)
        cr.set_line_width(1)
        cr.stroke()
        rounded(cr, 8, 8, width - 16, 20, 10)
        use("surface_variant")
        cr.fill()
        rounded(cr, 13, 12, 38, 12, 6)
        use("primary")
        cr.fill()
        rounded(cr, 19, 16.5, 26, 3, 1.5)
        use("on_primary")
        cr.fill()
        rounded(cr, 58, 15, min(70, width * 0.3), 6, 3)
        use("on_surface_variant", 0.8)
        cr.fill()
        cx, cy, cw, ch = 10, 38, width * 0.62, height - 48
        rounded(cr, cx + 1, cy + 3, cw, ch, 9)
        use("shadow", 0.35)
        cr.fill()
        rounded(cr, cx, cy, cw, ch, 9)
        use("surface_variant")
        cr.fill_preserve()
        use("outline")
        cr.set_line_width(1)
        cr.stroke()
        rounded(cr, cx + 10, cy + 10, cw * 0.55, 6, 3)
        use("on_surface")
        cr.fill()
        rounded(cr, cx + 10, cy + 22, cw * 0.75, 4, 2)
        use("on_surface_variant")
        cr.fill()
        by = cy + ch - 24
        rounded(cr, cx + 10, by, 44, 16, 8)
        use("primary")
        cr.fill()
        rounded(cr, cx + 18, by + 6.5, 28, 3, 1.5)
        use("on_primary")
        cr.fill()
        rounded(cr, cx + 60, by, 40, 16, 8)
        use("secondary")
        cr.fill()
        rounded(cr, cx + 67, by + 6.5, 26, 3, 1.5)
        use("on_secondary")
        cr.fill()
        rx = cx + cw + 12
        if rx + 30 < width:
            cr.arc(rx + 12, cy + 14, 11, 0, math.tau)
            use("tertiary")
            cr.fill()
            cr.arc(rx + 12, cy + 14, 4, 0, math.tau)
            use("on_tertiary")
            cr.fill()
            cr.arc(rx + 21, cy + 6, 6, 0, math.tau)
            use("error")
            cr.fill()
            rounded(cr, rx + 19, cy + 3.5, 4, 5, 1)
            use("on_error")
            cr.fill()
            for index in range(3):
                rounded(cr, rx, cy + 34 + index * 10, max(8, width - rx - 12 - index * 8), 4, 2)
                use("on_surface" if index == 0 else "on_surface_variant", 0.9)
                cr.fill()


def _hex_of(rgba: Gdk.RGBA) -> str:
    channels = (rgba.red, rgba.green, rgba.blue)
    red, green, blue = (round(min(1.0, max(0.0, value)) * 255) for value in channels)
    return f"#{red:02x}{green:02x}{blue:02x}"


class _SchemeCard(Gtk.Box):
    """One generator's result on the chosen still: its colours, Use and Save."""

    def __init__(self, scheme: str, label: str, dialog: PalettesDialog) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.scheme = scheme
        self.add_css_class("card")
        self.set_margin_top(2)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        head = Gtk.Box(spacing=6)
        title = Gtk.Label(label=label, xalign=0, hexpand=True)
        title.add_css_class("heading")
        head.append(title)
        self.default = widgets.pill("Default", "object-select-symbolic", "accent")
        self.default.set_valign(Gtk.Align.CENTER)
        self.default.set_visible(False)
        head.append(self.default)
        box.append(head)
        self.body = Gtk.Stack()
        self.spinner = Adw.Spinner(halign=Gtk.Align.START)
        self.body.add_named(self.spinner, "pending")
        self.swatches = widgets.Swatches([], size=16)
        self.swatches.set_halign(Gtk.Align.START)
        self.body.add_named(self.swatches, "colors")
        self.error = Gtk.Label(xalign=0, wrap=True)
        self.error.add_css_class("dimmed")
        self.error.add_css_class("caption")
        self.body.add_named(self.error, "error")
        box.append(self.body)
        buttons = Gtk.Box(spacing=6)
        self.use = Gtk.Button(label="Use")
        self.use.set_tooltip_text("Make this the default adaptive scheme")
        self.use.connect("clicked", lambda *_: dialog.use_scheme(scheme))
        self.save = Gtk.Button(label="Save…")
        self.save.set_tooltip_text("Keep these colours as a palette of yours")
        self.save.connect("clicked", lambda *_: dialog.save_scheme(scheme))
        for button in (self.use, self.save):
            button.add_css_class("flat")
            buttons.append(button)
        box.append(buttons)
        self.append(box)
        self.preview: SchemePreview | None = None

    def pending(self) -> None:
        self.preview = None
        self.body.set_visible_child_name("pending")
        self.save.set_sensitive(False)

    def display(self, preview: SchemePreview | None, dark: bool) -> None:
        self.preview = preview
        if preview is None:
            self.pending()
            return
        if preview.error or not (preview.dark or preview.light):
            self.error.set_label(preview.error or "Nothing to show")
            self.body.set_visible_child_name("error")
            self.save.set_sensitive(False)
            return
        self.swatches.set_colors(list(preview.dark if dark else preview.light))
        self.body.set_visible_child_name("colors")
        self.save.set_sensitive(True)


class PalettesDialog(Adw.Dialog):
    """The palette manager over `PaletteManager` (`SettingsAccess.palette_manager`)."""

    def __init__(
        self, state: AppState, access: SettingsAccess, *, schemes: bool = False, dark: bool = True
    ) -> None:
        super().__init__(title="Palettes", content_width=640, content_height=760)
        self.state = state
        self.access = access
        self.manager: PaletteManager = access.palette_manager
        self._dark = dark
        self._query = ""
        self._limit = PALETTE_PAGE_SIZE
        self._groups: list[Adw.PreferencesGroup] = []
        self._rows: dict[str, Adw.ActionRow] = {}
        self._source = ""
        self._sources: tuple[tuple[str, str], ...] = ()
        self._cards: dict[str, _SchemeCard] = {}
        self._closed = False

        self.nav = Adw.NavigationView()
        page = Adw.NavigationPage(title="Palettes", tag="list")
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.views = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.views, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        rescan = widgets.icon_button(
            "view-refresh-symbolic", "Rescan palettes", lambda *_: self.manager.rescan_palettes()
        )
        header.pack_start(rescan)
        self.variant = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.variant.add(Adw.Toggle(name="dark", label="Dark"))
        self.variant.add(Adw.Toggle(name="light", label="Light"))
        self.variant.set_active_name("dark" if dark else "light")
        self.variant.set_tooltip_text("Which variant the colours show")
        self.variant.connect("notify::active-name", self._on_variant)
        header.pack_end(self.variant)
        view.add_top_bar(header)
        self.views.add_titled_with_icon(
            self._build_installed(), "installed", "Installed", "view-list-symbolic"
        )
        self.views.add_titled_with_icon(
            self._build_schemes(), "schemes", "Schemes", "applications-graphics-symbolic"
        )
        self.views.connect("notify::visible-child-name", lambda *_: self._on_view())
        view.set_content(self.views)
        page.set_child(view)
        self.nav.add(page)
        self.set_child(self.nav)

        self._handler = state.connect("changed", self._on_changed)
        self.connect("closed", self._on_closed)
        self.manager.open_palettes()
        self.rebuild()
        if schemes:
            self.views.set_visible_child_name("schemes")

    # -- installed -------------------------------------------------------------
    def _build_installed(self) -> Gtk.Widget:
        self._list = Adw.PreferencesPage()
        search_group = Adw.PreferencesGroup()
        self.search = Gtk.SearchEntry(placeholder_text="Search palettes")
        self.search.connect("search-changed", self._on_search)
        search_group.add(self.search)
        self._list.add(search_group)
        self._status = Adw.PreferencesGroup()
        self._status_row = Adw.ActionRow(use_markup=False)
        self._status_spinner = Adw.Spinner()
        self._status_retry = Gtk.Button(label="Retry", valign=Gtk.Align.CENTER)
        self._status_retry.connect("clicked", lambda *_: self.manager.rescan_palettes())
        self._status_row.add_prefix(self._status_spinner)
        self._status_row.add_suffix(self._status_retry)
        self._status.add(self._status_row)
        self._list.add(self._status)
        self._more = Adw.PreferencesGroup()
        self._more_row = Adw.ActionRow()
        self._more_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self._more_button.connect("clicked", lambda *_: self.show_more())
        self._more_row.add_suffix(self._more_button)
        self._more.add(self._more_row)
        return self._list

    def _matching(self) -> list[PaletteItem]:
        words = self._query.split()
        labels = {origin: label for origin, label, _description in PALETTE_ORIGINS}
        return [
            item
            for item in self.manager.palette_list().items
            if all(
                word in f"{item.name} {item.origin} {labels.get(item.origin, '')}".lower()
                for word in words
            )
        ]

    def _visible(self, matching: list[PaletteItem]) -> list[PaletteItem]:
        """A page at a time, fairly across origins, so a large community cache hides none."""
        buckets = [
            [item for item in matching if item.origin == origin]
            for origin, _label, _description in PALETTE_ORIGINS
        ]
        shown: list[PaletteItem] = []
        index = 0
        while len(shown) < self._limit:
            added = False
            for bucket in buckets:
                if index < len(bucket) and len(shown) < self._limit:
                    shown.append(bucket[index])
                    added = True
            if not added:
                break
            index += 1
        return shown

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._query = entry.get_text().strip().lower()
        self._limit = PALETTE_PAGE_SIZE
        self.rebuild()

    def show_more(self) -> None:
        self._limit += PALETTE_PAGE_SIZE
        self.rebuild()

    def rebuild(self) -> None:
        """Show the catalogue as the manager last reported it."""
        listed = self.manager.palette_list()
        if listed.phase == "loading":
            self._status_row.set_title(
                "Refreshing installed palettes…" if listed.items else "Loading installed palettes…"
            )
            self._status_row.set_subtitle("Palette files are read outside the interface thread.")
        elif listed.phase == "error":
            self._status_row.set_title("Installed palettes could not be loaded")
            self._status_row.set_subtitle(listed.error)
        elif listed.phase == "idle":
            self._status_row.set_title("The palette catalogue has not loaded yet")
            self._status_row.set_subtitle("Choose Retry to scan the installed sources.")
        self._status.set_visible(listed.phase != "ready")
        self._status_spinner.set_visible(listed.phase == "loading")
        self._status_retry.set_visible(listed.phase in ("error", "idle"))

        for group in self._groups:
            self._list.remove(group)
        self._groups = []
        self._rows = {}
        matching = self._matching()
        visible = self._visible(matching)
        searching = bool(self._query)
        for origin, label, description in PALETTE_ORIGINS:
            items = [item for item in visible if item.origin == origin]
            total = sum(item.origin == origin for item in matching)
            if origin == "legacy" and not total:
                continue  # nothing left over: no group to explain
            note = description
            if total > len(items):
                note += f" Showing {len(items)} of {total}."
            group = Adw.PreferencesGroup(title=label, description=GLib.markup_escape_text(note))
            if not items:
                empty = Adw.ActionRow(
                    title="No matches" if searching else "Nothing here yet",
                    subtitle="Try another name" if searching else _ORIGIN_EMPTY[origin],
                )
                empty.set_activatable(False)
                group.add(empty)
            for item in items:
                row = self._row(item)
                self._rows[item.key] = row
                group.add(row)
            self._groups.append(group)
        if listed.skipped:
            skipped = Adw.PreferencesGroup(
                title="Skipped", description="Files in a palette folder that could not be read."
            )
            for note in listed.skipped[:MAX_SKIPPED_ROWS]:
                row = Adw.ActionRow(title=note, use_markup=False)
                row.set_activatable(False)
                row.add_css_class("dimmed")
                skipped.add(row)
            remaining = len(listed.skipped) - MAX_SKIPPED_ROWS
            if remaining > 0:
                skipped.add(
                    Adw.ActionRow(
                        title=f"{remaining} more skipped files",
                        subtitle="Fix the listed files, then rescan to see the next ones.",
                    )
                )
            self._groups.append(skipped)
        remaining = len(matching) - len(visible)
        if remaining > 0:
            self._more_row.set_title(f"{len(visible)} of {len(matching)} matching palettes shown")
            self._more_row.set_subtitle("Every palette stays searchable.")
            self._more_button.set_label(f"Load {min(PALETTE_PAGE_SIZE, remaining)} more")
            self._groups.append(self._more)
        for group in self._groups:
            self._list.add(group)

    def _row(self, item: PaletteItem) -> Adw.ActionRow:
        row = Adw.ActionRow(title=item.name, subtitle=item.detail, use_markup=False)
        colors = item.dark if self._dark else item.light
        swatches = widgets.Swatches(list(colors), size=14)
        swatches.set_tooltip_text(
            "Surface, primary, secondary, tertiary, error"
            if colors
            else "Noctalia shows these colours once the palette is applied"
        )
        row.add_prefix(swatches)
        apply = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        apply.add_css_class("flat")
        apply.connect("clicked", lambda *_: self.manager.apply_catalog_palette(item.key))
        if item.can_apply:
            apply.set_tooltip_text("Use these colours on the desktop now")
        else:
            # Noctalia has no source name for it: the way out is a copy.
            apply.set_sensitive(False)
            apply.set_tooltip_text("Noctalia no longer reads this layout; duplicate it first")
        row.add_suffix(apply)
        if item.can_edit:
            edit = widgets.icon_button(
                "document-edit-symbolic", "Edit", lambda *_: self.edit(item, copy=False), "flat"
            )
            edit.set_valign(Gtk.Align.CENTER)
            row.add_suffix(edit)
        duplicate = widgets.icon_button(
            "edit-copy-symbolic",
            "Duplicate into your own palettes and edit",
            lambda *_: self.edit(item, copy=True),
            "flat",
        )
        duplicate.set_valign(Gtk.Align.CENTER)
        duplicate.set_sensitive(item.can_copy)
        row.add_suffix(duplicate)
        return row

    def row(self, key: str) -> Adw.ActionRow | None:
        """The row shown for a palette, for tests."""
        return self._rows.get(key)

    # -- the editor ----------------------------------------------------------------
    def edit(self, item: PaletteItem, *, copy: bool) -> None:
        def opened(draft: PaletteDraft | None) -> None:
            if draft is not None and not self._closed:
                self.open_editor(draft)

        self.manager.palette_draft(item.key, copy=copy, done=opened)

    def open_editor(self, draft: PaletteDraft) -> PaletteEditor:
        editor = PaletteEditor(self, draft)
        self.nav.push(editor)
        return editor

    # -- schemes -------------------------------------------------------------------
    def _build_schemes(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Preview source",
            description="What each generator makes of one of your stills: the same code "
            "Noctalia runs, so this is the result rather than an approximation.",
        )
        self._source_row = Adw.ComboRow(title="Wallpaper", model=Gtk.StringList.new([]))
        self._source_row.connect("notify::selected", self._on_source)
        group.add(self._source_row)
        self.sync = Adw.SwitchRow(
            title="Also set the scheme in Noctalia",
            subtitle="Otherwise “Use” only changes the default for adaptive wallpapers",
        )
        group.add(self.sync)
        page.add(group)
        grid = Adw.PreferencesGroup(title="Schemes")
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            min_children_per_line=2,
            max_children_per_line=3,
            column_spacing=8,
            row_spacing=8,
        )
        for scheme, label in SCHEMES:
            card = _SchemeCard(scheme, label, self)
            self._cards[scheme] = card
            flow.append(card)
        grid.add(flow)
        page.add(grid)
        return page

    def _on_view(self) -> None:
        if self.views.get_visible_child_name() == "schemes" and not self._sources:
            self._load_sources()

    def _load_sources(self) -> None:
        self._mark_default()
        sources = self.manager.scheme_sources()
        self._sources = sources.items
        self._source_row.set_model(Gtk.StringList.new([label for _id, label in sources.items]))
        if not sources.items:
            self._source_row.set_subtitle("No still wallpapers in the library")
            for card in self._cards.values():
                card.display(SchemePreview("", card.scheme, error="Nothing to generate from"), True)
            return
        self._source_row.set_selected(sources.selected)
        self._preview(sources.items[min(sources.selected, len(sources.items) - 1)][0])

    def _on_source(self, row: Adw.ComboRow, _param: object) -> None:
        index = row.get_selected()
        if 0 <= index < len(self._sources):
            self._preview(self._sources[index][0])

    def _preview(self, source: str) -> None:
        if source == self._source:
            return
        self._source = source
        for card in self._cards.values():
            card.pending()
        self.manager.preview_schemes(source, self._arrived)

    def _arrived(self, preview: SchemePreview) -> None:
        if self._closed or preview.source != self._source:
            return  # a wallpaper already left; the manager keeps it for coming back
        card = self._cards.get(preview.scheme)
        if card is not None:
            card.display(preview, self._dark)

    def _mark_default(self) -> None:
        default = self.access.setting("preview_scheme")
        for scheme, card in self._cards.items():
            card.default.set_visible(scheme == default)

    def use_scheme(self, scheme: str) -> None:
        self.manager.use_scheme(scheme, sync_noctalia=self.sync.get_active())

    def save_scheme(self, scheme: str) -> None:
        draft = self.manager.scheme_draft(self._source, scheme)
        if draft is None:
            self.state.toast("That scheme has not finished generating")
            return
        self.open_editor(draft)

    # -- both ------------------------------------------------------------------------
    def _on_variant(self, group: Adw.ToggleGroup, _param: object) -> None:
        self._dark = group.get_active_name() != "light"
        self.rebuild()
        for card in self._cards.values():
            if card.preview is not None:
                card.display(card.preview, self._dark)

    def _on_changed(self, _state: AppState, topic: str) -> None:
        if topic == "palettes":
            self.rebuild()
            editor = self.nav.get_visible_page()
            if isinstance(editor, PaletteEditor):
                editor.validate()  # the names it is checked against may have moved
        elif topic == "settings":
            self._mark_default()

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        if self._closed:
            return
        self._closed = True
        self.state.disconnect(self._handler)
        self.manager.close_palettes()


class PaletteEditor(Adw.NavigationPage):
    """The fourteen colours of a palette, light and dark side by side, and its name."""

    CELL: Final = 76

    def __init__(self, owner: PalettesDialog, draft: PaletteDraft) -> None:
        super().__init__(title=draft.title, tag="editor")
        self.owner = owner
        self.draft = draft
        self.values: dict[str, dict[str, str]] = {
            mode: dict(draft.colors.get(mode, {})) for mode in ("light", "dark")
        }
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(
            show_back_button=False, show_end_title_buttons=False, show_start_title_buttons=False
        )
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: owner.nav.pop())
        header.pack_start(cancel)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda *_: self.save())
        header.pack_end(self.save_button)
        view.add_top_bar(header)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=18,
            margin_end=18,
            margin_top=6,
            margin_bottom=24,
        )
        names = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        names.add_css_class("boxed-list")
        self.name = Adw.EntryRow(title="Name", text=draft.name)
        self.name.set_editable(draft.renamable)
        if not draft.renamable:
            self.name.set_tooltip_text("Editing in place; duplicate it to rename")
        self.name.connect("changed", lambda _row: self.validate())
        names.append(self.name)
        box.append(names)
        # Why Save waits: a name no other palette a person can pick already has.
        self.problem = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.problem.add_css_class("error")
        self.problem.add_css_class("caption")
        box.append(self.problem)

        previews = Gtk.Box(spacing=12, homogeneous=True)
        self._previews: list[PalettePreview] = []
        for mode in ("light", "dark"):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            label = Gtk.Label(label=mode.upper(), xalign=0)
            label.add_css_class("caption-heading")
            column.append(label)
            preview = PalettePreview(self.values[mode])
            self._previews.append(preview)
            column.append(preview)
            previews.append(column)
        box.append(previews)

        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        self.buttons: dict[tuple[str, str], Gtk.ColorDialogButton] = {}
        for role, title in PALETTE_ROLES:
            line = Gtk.Box(margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
            line.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            for mode in ("light", "dark"):
                cell = Gtk.Box(hexpand=False)
                cell.set_size_request(self.CELL, -1)
                button = Gtk.ColorDialogButton(
                    dialog=Gtk.ColorDialog(with_alpha=False, title=title)
                )
                button.set_rgba(widgets.rgba(self.values[mode].get(role, "#808080")))
                button.set_hexpand(True)
                button.set_halign(Gtk.Align.CENTER)
                button.update_property([Gtk.AccessibleProperty.LABEL], [f"{title}, {mode}"])
                button.set_tooltip_text(f"{title} · {mode}")
                button.connect("notify::rgba", self._on_color, mode, role)
                self.buttons[(mode, role)] = button
                cell.append(button)
                line.append(cell)
            rows.append(Gtk.ListBoxRow(activatable=False, child=line))
        box.append(rows)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(Adw.Clamp(maximum_size=640, child=box))
        view.set_content(scroller)
        self.set_child(view)
        self._saving = False
        self.validate()

    def _on_color(
        self, button: Gtk.ColorDialogButton, _param: object, mode: str, role: str
    ) -> None:
        self.values[mode][role] = _hex_of(button.get_rgba())
        for preview in self._previews:
            preview.queue_draw()

    def set_color(self, mode: str, role: str, value: str) -> None:
        """Pick a colour as the colour button would."""
        self.buttons[(mode, role)].set_rgba(widgets.rgba(value))

    def validate(self) -> str:
        """Check the name now; Save stays off, with the reason shown, until it is free."""
        problem = self.owner.manager.palette_name_problem(self.draft, self.name.get_text())
        self.problem.set_label(problem)
        self.problem.set_visible(bool(problem))
        if problem:
            self.name.add_css_class("error")
        else:
            self.name.remove_css_class("error")
        self.save_button.set_sensitive(not problem and not self._saving)
        return problem

    def save(self) -> None:
        if self._saving or self.validate():
            return
        self._saving = True
        self.save_button.set_sensitive(False)

        def done(saved: bool) -> None:
            self._saving = False
            if saved and self.owner.nav.get_visible_page() is self:
                self.owner.nav.pop()
            else:
                self.validate()

        self.owner.manager.save_palette_draft(self.draft, self.name.get_text(), self.values, done)
