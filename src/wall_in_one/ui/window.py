"""The main window.

At this stage it exists to prove the colour pipeline end to end: resolve the
palette, render it to CSS, apply it live, and show where it came from. The
library and playlist views land on top of this once the pipeline is trusted.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from wall_in_one import config
from wall_in_one.theme import source
from wall_in_one.theme.palette import Palette

#: Tokens worth showing as a swatch strip. The full 72 are available to CSS;
#: these are the ones that tell you at a glance whether sync is working.
_SWATCH_TOKENS: tuple[tuple[str, str], ...] = (
    ("primary", "Primary"),
    ("secondary", "Secondary"),
    ("tertiary", "Tertiary"),
    ("error", "Error"),
    ("surface", "Surface"),
    ("surface_container", "Container"),
    ("outline", "Outline"),
)


class MainWindow(Adw.ApplicationWindow):
    """Application window. Owns the settings that affect its own appearance."""

    def __init__(self, application: Adw.Application, settings: config.Settings) -> None:
        super().__init__(application=application)
        self._settings = settings
        self._on_settings_changed: list[Any] = []

        self.set_title("Wall-in-One")
        self.set_default_size(880, 620)

        self._status_row = Adw.ActionRow(title="Palette source")
        self._swatches = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._swatches.set_homogeneous(True)

        self.set_content(self._build_content())

    # -- construction ----------------------------------------------------

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Wall-in-One", subtitle="Wallpaper manager"))
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        page.add(self._build_palette_group())
        page.add(self._build_appearance_group())

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(page)
        scroller.set_vexpand(True)
        toolbar.set_content(scroller)
        return toolbar

    def _build_palette_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Colour",
            description="Colours follow Noctalia's active palette.",
        )
        group.add(self._status_row)

        swatch_row = Adw.PreferencesRow()
        swatch_row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.append(self._swatches)
        swatch_row.set_child(box)
        group.add(swatch_row)

        refresh = Gtk.Button(label="Reload palette")
        refresh.set_halign(Gtk.Align.START)
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda _button: self.reload_palette())
        refresh_row = Adw.PreferencesRow()
        refresh_row.set_activatable(False)
        refresh.set_margin_top(8)
        refresh.set_margin_bottom(8)
        refresh.set_margin_start(12)
        refresh_row.set_child(refresh)
        group.add(refresh_row)
        return group

    def _build_appearance_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Appearance",
            description=(
                "Translucency is applied by this app. Blur behind it is the "
                "compositor's job -- see docs/niri.md for the window rule."
            ),
        )

        adjustment = Gtk.Adjustment(
            lower=config.MIN_OPACITY,
            upper=1.0,
            step_increment=0.01,
            page_increment=0.05,
            value=self._settings.opacity,
        )
        scale = Gtk.Scale(adjustment=adjustment, orientation=Gtk.Orientation.HORIZONTAL)
        scale.set_digits(2)
        scale.set_draw_value(True)
        scale.set_hexpand(True)
        scale.set_size_request(260, -1)
        scale.connect("value-changed", self._on_opacity_changed)

        row = Adw.ActionRow(
            title="Window opacity",
            subtitle="Lower values let the compositor show and blur through",
        )
        row.add_suffix(scale)
        group.add(row)
        return group

    # -- behaviour -------------------------------------------------------

    def _on_opacity_changed(self, scale: Gtk.Scale) -> None:
        value = round(scale.get_value(), 2)
        if abs(value - self._settings.opacity) < 1e-9:
            return
        from dataclasses import replace

        self._settings = replace(self._settings, opacity=value).validated()
        self.emit_settings_changed()

    def emit_settings_changed(self) -> None:
        for callback in self._on_settings_changed:
            callback(self._settings)

    def connect_settings_changed(self, callback: Any) -> None:
        self._on_settings_changed.append(callback)

    @property
    def settings(self) -> config.Settings:
        return self._settings

    def reload_palette(self) -> source.ResolvedPalette:
        resolved = source.resolve(scheme=self._settings.preview_scheme)
        self.show_palette(resolved)
        return resolved

    def show_palette(self, resolved: source.ResolvedPalette) -> None:
        self._status_row.set_subtitle(f"{resolved.origin.value} - {resolved.detail}")
        self._rebuild_swatches(resolved.palette)

    def _rebuild_swatches(self, palette: Palette) -> None:
        child = self._swatches.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._swatches.remove(child)
            child = following

        for token, label in _SWATCH_TOKENS:
            if token not in palette.colours:
                continue
            self._swatches.append(_swatch(palette, token, label))


def _swatch(palette: Palette, token: str, label: str) -> Gtk.Widget:
    colour = palette[token]
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

    chip = Gtk.Box()
    chip.set_size_request(-1, 44)
    # Set inline rather than through the stylesheet: these are data, not theme,
    # and regenerating 70-odd rules on every palette change to express them
    # would be the wrong shape.
    provider = Gtk.CssProvider()
    provider.load_from_string(
        f".wio-swatch-{token} {{ background-color: {colour.hex};"
        " border-radius: 8px; border: 1px solid alpha(currentColor, 0.15); }"
    )
    chip.add_css_class(f"wio-swatch-{token}")
    chip.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    caption = Gtk.Label(label=label)
    caption.add_css_class("caption")
    value = Gtk.Label(label=colour.hex)
    value.add_css_class("caption")
    value.add_css_class("dim-label")

    box.append(chip)
    box.append(caption)
    box.append(value)
    return box
