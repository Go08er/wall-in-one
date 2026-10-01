"""Palettes: browse, apply, duplicate and edit Noctalia palettes.

One dialog. The editor is pushed as a second page inside it (Adw.NavigationView)
instead of stacking another dialog on top, which the current app does.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from .. import ui
from ..models import Palette
from .settings_widgets import rounded

# The 14 core keys of a Noctalia palette, in the order the editor shows them.
KEYS: list[tuple[str, str]] = [
    ("primary", "Primary"),
    ("on_primary", "On primary"),
    ("secondary", "Secondary"),
    ("on_secondary", "On secondary"),
    ("tertiary", "Tertiary"),
    ("on_tertiary", "On tertiary"),
    ("error", "Error"),
    ("on_error", "On error"),
    ("surface", "Surface"),
    ("on_surface", "On surface"),
    ("surface_variant", "Surface variant"),
    ("on_surface_variant", "On surface variant"),
    ("outline", "Outline"),
    ("shadow", "Shadow"),
]

ORIGINS = [
    ("custom", "Yours", "Editable"),
    ("builtin", "Built-in", "Come with Noctalia · read-only"),
    ("community", "Community", "From Noctalia's catalog · cached yesterday"),
]


# ---------------------------------------------------------------------------
# Preview of the full key set
# ---------------------------------------------------------------------------


class PalettePreview(Gtk.DrawingArea):
    """A tiny app mock-up painted with all 14 keys, so edits are visible at once."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__()
        self.values = values
        self.set_content_height(116)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def _draw(self, _area, cr, width: int, height: int) -> None:
        def use(key: str, alpha: float = 1.0) -> None:
            value = self.values.get(key, "#888888").lstrip("#")
            r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
            cr.set_source_rgba(r, g, b, alpha)

        rounded(cr, 0.5, 0.5, width - 1, height - 1, 12)
        use("surface")
        cr.fill_preserve()
        use("outline", 0.8)
        cr.set_line_width(1)
        cr.stroke()
        # Top bar
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
        # Card with shadow and outline
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
        # Buttons: primary + secondary
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
        # Right column: tertiary avatar + error badge + text
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


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class PalettesDialog(Adw.Dialog):
    def __init__(self, state, on_changed: Callable[[], None]) -> None:
        super().__init__(title="Palettes", content_width=600, content_height=720)
        self.state = state
        self._on_changed = on_changed  # the palette actions emit "settings" themselves
        self._query = ""
        self._rows: list[tuple[Adw.ActionRow, Palette]] = []
        self._row_origin: dict[Adw.ActionRow, str] = {}

        self.nav = Adw.NavigationView()
        page = Adw.NavigationPage(title="Palettes", tag="list")
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.search = Gtk.SearchEntry(placeholder_text="Search palettes", hexpand=True)
        self.search.connect("search-changed", self._on_search)
        search_box = Gtk.Box(margin_start=18, margin_end=18, margin_bottom=6)
        search_box.append(self.search)
        view.add_top_bar(search_box)

        self._groups: dict[str, Adw.PreferencesGroup] = {}
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24,
            margin_start=18,
            margin_end=18,
            margin_top=12,
            margin_bottom=24,
        )
        for origin, title, description in ORIGINS:
            group = Adw.PreferencesGroup(title=title, description=description)
            if origin == "community":
                group.set_header_suffix(
                    ui.icon_button(
                        "view-refresh-symbolic",
                        "Check for new community palettes",
                        lambda *_: self.state.toast("Community palettes are up to date"),
                        "flat",
                    )
                )
            self._groups[origin] = group
            box.append(group)
        self._scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self._scroller.set_child(Adw.Clamp(maximum_size=640, child=box))
        empty = Adw.StatusPage(
            icon_name="edit-find-symbolic", title="No palettes match", description="Try another name."
        )
        empty.add_css_class("compact")
        self._stack = Gtk.Stack()
        self._stack.add_named(self._scroller, "list")
        self._stack.add_named(empty, "empty")
        view.set_content(self._stack)
        page.set_child(view)
        self.nav.add(page)
        self.set_child(self.nav)
        self.rebuild()

    # -- list ----------------------------------------------------------------
    def rebuild(self) -> None:
        for row, _palette in self._rows:
            self._groups[self._row_origin[row]].remove(row)
        self._rows = []
        self._row_origin: dict[Adw.ActionRow, str] = {}
        for origin, _title, _description in ORIGINS:
            group = self._groups[origin]
            for palette in self.state.palettes(origin):
                row = self._row(palette)
                group.add(row)
                self._rows.append((row, palette))
                self._row_origin[row] = origin
        self._filter()

    def _row(self, palette: Palette) -> Adw.ActionRow:
        row = Adw.ActionRow(title=palette.name, use_markup=False)
        swatches = ui.Swatches(palette.strip(self.state.dark), size=14)
        swatches.set_tooltip_text("Surface, primary, secondary, tertiary, error")
        row.add_prefix(swatches)
        applied = self.state.applied_palette()
        if applied is not None and applied.name == palette.name:
            on = ui.pill("On desktop", "object-select-symbolic", "accent")
            on.set_valign(Gtk.Align.CENTER)
            row.add_suffix(on)
        else:
            apply = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
            apply.add_css_class("flat")
            apply.set_tooltip_text("Use these colors on the desktop now")
            apply.connect("clicked", lambda *_: self._apply(palette))
            row.add_suffix(apply)
        if palette.editable:
            edit = ui.icon_button("document-edit-symbolic", "Edit", lambda *_: self.edit(palette), "flat")
            edit.set_valign(Gtk.Align.CENTER)
            row.add_suffix(edit)
            row.set_activatable_widget(edit)
        duplicate = ui.icon_button(
            "edit-copy-symbolic", "Duplicate as your own palette", lambda *_: self._duplicate(palette), "flat"
        )
        duplicate.set_valign(Gtk.Align.CENTER)
        row.add_suffix(duplicate)
        return row

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._query = entry.get_text().strip().lower()
        self._filter()

    def _filter(self) -> None:
        words = self._query.split()
        shown_by_origin: dict[str, int] = {origin: 0 for origin in self._groups}
        for row, palette in self._rows:
            haystack = f"{palette.name} {palette.origin} {dict((o, t) for o, t, _ in ORIGINS)[palette.origin]}".lower()
            visible = all(word in haystack for word in words)
            row.set_visible(visible)
            shown_by_origin[palette.origin] += visible
        for origin, group in self._groups.items():
            # Keep "Yours" visible when empty and not searching, as a hint to duplicate.
            group.set_visible(bool(shown_by_origin[origin]) or (origin == "custom" and not words))
            if origin == "custom":
                group.set_description(
                    "Editable" if shown_by_origin[origin] or words else "Duplicate a palette to make your own"
                )
        self._stack.set_visible_child_name("list" if any(shown_by_origin.values()) else "empty")

    # -- actions -------------------------------------------------------------
    def _apply(self, palette: Palette) -> None:
        restore = self.state.apply_palette(palette.name)
        self.rebuild()
        self._on_changed()

        def undo() -> None:
            restore()
            self.rebuild()
            self._on_changed()

        self.state.toast(f"“{palette.name}” is on the desktop until the wallpaper changes", undo)

    def _duplicate(self, palette: Palette) -> None:
        copy, remove = self.state.duplicate_palette(palette.name)
        self.rebuild()
        self._on_changed()
        GLib.idle_add(lambda: (self._scroller.get_vadjustment().set_value(0), False)[1])

        def undo() -> None:
            if copy in self.state.palettes("custom"):
                remove()
                self.rebuild()
                self._on_changed()

        self.state.toast(f"Duplicated as “{copy.name}”", undo)

    def edit(self, palette: Palette) -> None:
        self.nav.push(PaletteEditor(self, palette))

    def delete(self, palette: Palette) -> None:
        restore = self.state.delete_palette(palette.name)
        self.rebuild()
        self._on_changed()

        def undo() -> None:
            restore()
            self.rebuild()
            self._on_changed()

        self.state.toast(f"Deleted “{palette.name}”", undo)

    def saved(self, palette: Palette) -> None:
        self.rebuild()
        self._on_changed()
        self.state.toast(f"Saved “{palette.name}”")


class PaletteEditor(Adw.NavigationPage):
    """Edit the 14 keys of a custom palette, light and dark side by side."""

    CELL = 76

    def __init__(self, owner: PalettesDialog, palette: Palette) -> None:
        super().__init__(title="Edit palette", tag="editor")
        self.owner = owner
        self.palette = palette
        self._light = dict(palette.light)
        self._dark = dict(palette.dark)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_back_button=False, show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: owner.nav.pop())
        header.pack_start(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        header.pack_end(save)
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
        self._name = Adw.EntryRow(title="Name", text=palette.name)
        self._name.connect("changed", lambda *_: self._name.remove_css_class("error"))
        names.append(self._name)
        box.append(names)

        previews = Gtk.Box(spacing=12, homogeneous=True)
        self._previews: dict[str, PalettePreview] = {}
        for variant, values in (("light", self._light), ("dark", self._dark)):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            label = Gtk.Label(label=variant.upper(), xalign=0)
            label.add_css_class("section-label")
            column.append(label)
            preview = PalettePreview(values)
            self._previews[variant] = preview
            column.append(preview)
            previews.append(column)
        box.append(previews)

        table = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        head = Gtk.Box(margin_start=12, margin_end=12)
        for text, width, expand in (("COLOR", -1, True), ("LIGHT", self.CELL, False), ("DARK", self.CELL, False)):
            label = Gtk.Label(label=text, xalign=0 if expand else 0.5, hexpand=expand)
            label.set_size_request(width, -1)
            if not expand:
                label.set_max_width_chars(1)  # never wider than its cell
            label.add_css_class("section-label")
            head.append(label)
        table.append(head)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        for key, title in KEYS:
            line = Gtk.Box(margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
            name = Gtk.Label(label=title, xalign=0, hexpand=True)
            line.append(name)
            for values in (self._light, self._dark):
                # Fixed-width cells that never expand keep both columns under their headers.
                cell = Gtk.Box(hexpand=False)
                cell.set_size_request(self.CELL, -1)
                button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False, title=title))
                button.set_rgba(ui.rgba(values[key]))
                button.set_hexpand(True)
                button.set_halign(Gtk.Align.CENTER)
                button.update_property(
                    [Gtk.AccessibleProperty.LABEL], [f"{title}, {'light' if values is self._light else 'dark'}"]
                )
                button.set_tooltip_text(f"{title} · {'light' if values is self._light else 'dark'}")
                button.connect("notify::rgba", self._on_color, values, key)
                cell.append(button)
                line.append(cell)
            row = Gtk.ListBoxRow(activatable=False, child=line)
            rows.append(row)
        table.append(rows)
        box.append(table)

        delete = Gtk.Button(label="Delete palette…", halign=Gtk.Align.START)
        delete.add_css_class("flat")
        delete.add_css_class("error")
        delete.connect("clicked", self._confirm_delete)
        box.append(delete)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(Adw.Clamp(maximum_size=640, child=box))
        view.set_content(scroller)
        self.set_child(view)

    def _on_color(self, button: Gtk.ColorDialogButton, _param, values: dict[str, str], key: str) -> None:
        rgba: Gdk.RGBA = button.get_rgba()
        values[key] = f"#{round(rgba.red * 255):02x}{round(rgba.green * 255):02x}{round(rgba.blue * 255):02x}"
        for preview in self._previews.values():
            preview.queue_draw()

    def _save(self, *_args) -> None:
        name = self._name.get_text().strip()
        clash = self.owner.state.find_palette(name)
        if not name or (clash is not None and clash is not self.palette):
            self._name.add_css_class("error")
            self.owner.state.toast("Choose a name no other palette uses")
            return
        self.owner.state.save_palette(self.palette.name, name, self._light, self._dark)
        self.owner.nav.pop()
        self.owner.saved(self.palette)

    def _confirm_delete(self, *_args) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Delete “{self.palette.name}”?", body="Wallpapers using it go back to their own colors."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def done(_dialog, response: str) -> None:
            if response == "delete":
                self.owner.nav.pop()
                self.owner.delete(self.palette)

        dialog.connect("response", done)
        dialog.present(self)
