"""Settings dialog.

Everything that used to be the whole window lives here now, plus the four
controls that previously existed only as `ctl` verbs: shuffle, cycle, cycle
interval, and dynamics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from wall_in_one import config
from wall_in_one.theme import source
from wall_in_one.theme.noctalia import ALL_SCHEMES
from wall_in_one.theme.palette import Palette

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application

#: Tokens worth showing as a swatch strip. All 72 are available to CSS; these
#: are the ones that tell you at a glance whether sync is working.
_SWATCH_TOKENS: tuple[tuple[str, str], ...] = (
    ("primary", "Primary"),
    ("secondary", "Secondary"),
    ("tertiary", "Tertiary"),
    ("error", "Error"),
    ("surface", "Surface"),
    ("surface_container", "Container"),
    ("outline", "Outline"),
)


class PreferencesDialog(Adw.PreferencesDialog):
    """Playback, colour, and appearance settings."""

    def __init__(self, application: Application) -> None:
        super().__init__()
        self._app = application
        # Set while widgets are being populated from settings, so that
        # programmatic changes do not read as user edits and write back.
        self._loading = False

        self.set_title("Settings")
        page = Adw.PreferencesPage()
        page.add(self._build_playback_group())
        page.add(self._build_colour_group())
        page.add(self._build_appearance_group())
        self.add(page)

        self._load(application.settings)
        self.show_palette(application.resolved_palette)

    # -- construction ----------------------------------------------------

    def _build_playback_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Playback")

        self._shuffle = Adw.SwitchRow(
            title="Shuffle",
            subtitle="Visit every wallpaper once before repeating",
        )
        self._shuffle.connect("notify::active", self._on_changed)
        group.add(self._shuffle)

        self._cycle = Adw.SwitchRow(
            title="Cycle",
            subtitle="Change wallpaper on a timer",
        )
        self._cycle.connect("notify::active", self._on_changed)
        group.add(self._cycle)

        self._interval = Adw.SpinRow(
            title="Cycle interval",
            subtitle="Seconds between changes",
            adjustment=Gtk.Adjustment(lower=5, upper=24 * 60 * 60, step_increment=30, value=300),
        )
        self._interval.connect("notify::value", self._on_changed)
        group.add(self._interval)

        self._dynamics = Adw.SwitchRow(
            title="Dynamics",
            subtitle="Play video wallpapers. Off shows their paired stills instead",
        )
        self._dynamics.connect("notify::active", self._on_changed)
        group.add(self._dynamics)
        return group

    def _build_colour_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Colour",
            description="Colours follow Noctalia's active palette.",
        )

        self._palette_source = Adw.ActionRow(title="Palette source")
        group.add(self._palette_source)

        self._scheme = Adw.ComboRow(
            title="Scheme",
            subtitle="Generator used when a palette is derived from the wallpaper",
            model=Gtk.StringList.new(list(ALL_SCHEMES)),
        )
        self._scheme.connect("notify::selected", self._on_changed)
        group.add(self._scheme)

        self._swatches = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._swatches.set_homogeneous(True)
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

        reload_row = Adw.ActionRow(
            title="Reload palette",
            subtitle="Re-read the colours Noctalia last rendered",
        )
        reload_button = Gtk.Button(label="Reload")
        reload_button.set_valign(Gtk.Align.CENTER)
        reload_button.add_css_class("flat")
        reload_button.connect("clicked", self._on_reload_palette)
        reload_row.add_suffix(reload_button)
        group.add(reload_row)
        return group

    def _build_appearance_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Appearance",
            description=(
                "Translucency is applied by this app. Blur behind it is the "
                "compositor's job -- see docs/niri.md for the window rule."
            ),
        )
        self._opacity = Adw.SpinRow(
            title="Window opacity",
            subtitle="Lower values let the compositor show and blur through",
            adjustment=Gtk.Adjustment(
                lower=config.MIN_OPACITY, upper=1.0, step_increment=0.01, value=1.0
            ),
            digits=2,
        )
        self._opacity.connect("notify::value", self._on_changed)
        group.add(self._opacity)
        return group

    # -- state -----------------------------------------------------------

    def _load(self, settings: config.Settings) -> None:
        self._loading = True
        try:
            self._shuffle.set_active(settings.shuffle)
            self._cycle.set_active(settings.cycle_enabled)
            self._interval.set_value(settings.cycle_interval)
            self._dynamics.set_active(settings.dynamics_enabled)
            self._opacity.set_value(settings.opacity)
            if settings.preview_scheme in ALL_SCHEMES:
                self._scheme.set_selected(ALL_SCHEMES.index(settings.preview_scheme))
        finally:
            self._loading = False

    def _on_changed(self, *_arguments: object) -> None:
        if self._loading:
            return
        scheme_index = self._scheme.get_selected()
        self._app.update_settings(
            shuffle=self._shuffle.get_active(),
            cycle_enabled=self._cycle.get_active(),
            cycle_interval=int(self._interval.get_value()),
            dynamics_enabled=self._dynamics.get_active(),
            opacity=round(self._opacity.get_value(), 2),
            preview_scheme=ALL_SCHEMES[scheme_index]
            if scheme_index < len(ALL_SCHEMES)
            else config.Settings().preview_scheme,
        )

    def _on_reload_palette(self, _button: Gtk.Button) -> None:
        self.show_palette(self._app.reload_palette())

    def show_palette(self, resolved: source.ResolvedPalette | None) -> None:
        if resolved is None:
            self._palette_source.set_subtitle("not resolved yet")
            return
        # `detail` already reads as a sentence ("generated from x.png with ..."),
        # so prefixing the origin would stutter. The one case worth naming is the
        # fallback, where the colours are ours rather than Noctalia's.
        prefix = "" if resolved.is_live else "fallback palette - "
        self._palette_source.set_subtitle(f"{prefix}{resolved.detail}")
        self._rebuild_swatches(resolved.palette)

    def _rebuild_swatches(self, palette: Palette) -> None:
        while (child := self._swatches.get_first_child()) is not None:
            self._swatches.remove(child)
        for token, label in _SWATCH_TOKENS:
            if token in palette.colours:
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
