"""Resolve the palette the app should paint itself with.

Three tiers, best first:

1. **The template output.** If `--install-theme-template` has been run,
   Noctalia renders the live palette to `palette.json` on every change and
   pushes us a reload. This is exact and covers all four palette sources
   (builtin, wallpaper, community, custom).
2. **Generated from the current wallpaper.** If the template is not installed
   but Noctalia is running and its palette is wallpaper-derived, we can
   reproduce it precisely by calling the same generator Noctalia uses.
3. **A built-in fallback.** So the app starts and is usable with no Noctalia at
   all.

Tier 2 cannot cover a builtin palette -- those are compiled into Noctalia's
binary and not exposed by the CLI -- which is the concrete reason the template
is worth installing rather than optional.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from wall_in_one import paths
from wall_in_one.theme import noctalia
from wall_in_one.theme.palette import Colour, Mode, Palette, PaletteError


class Origin(Enum):
    TEMPLATE = "template"
    GENERATED = "generated"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class ResolvedPalette:
    palette: Palette
    origin: Origin
    detail: str

    @property
    def is_live(self) -> bool:
        """Whether this tracks Noctalia's actual palette."""
        return self.origin is not Origin.FALLBACK


#: A neutral dark palette, used only when Noctalia is absent or silent. Kept to
#: the tokens `theme.css` actually maps, since `Palette.get` degrades the rest.
_FALLBACK_TOKENS: Final[dict[str, str]] = {
    "source_color": "#6f7fa8",
    "primary": "#adc6ff",
    "on_primary": "#002e69",
    "primary_container": "#284777",
    "on_primary_container": "#d8e2ff",
    "secondary": "#bfc6dc",
    "on_secondary": "#293041",
    "secondary_container": "#3f4759",
    "on_secondary_container": "#dbe2f9",
    "tertiary": "#debcdf",
    "on_tertiary": "#402843",
    "on_tertiary_container": "#fbd7fc",
    "error": "#ffb4ab",
    "on_error": "#690005",
    "surface": "#131318",
    "on_surface": "#e4e1e9",
    "surface_variant": "#44464f",
    "on_surface_variant": "#c5c6d0",
    "surface_dim": "#131318",
    "surface_bright": "#39383f",
    "surface_container_lowest": "#0e0e13",
    "surface_container_low": "#1b1b21",
    "surface_container": "#1f1f25",
    "surface_container_high": "#2a2930",
    "surface_container_highest": "#35343a",
    "outline": "#8f909a",
    "outline_variant": "#44464f",
    "shadow": "#000000",
    "scrim": "#000000",
    "inverse_surface": "#e4e1e9",
    "inverse_on_surface": "#303036",
    "inverse_primary": "#415e91",
    "background": "#131318",
    "on_background": "#e4e1e9",
}


def fallback_palette(mode: Mode = "dark") -> Palette:
    colours = {token: Colour.parse(value) for token, value in _FALLBACK_TOKENS.items()}
    return Palette(mode=mode, colours=colours)


def from_template(path: Path | None = None) -> ResolvedPalette | None:
    """Load the palette Noctalia last rendered for us, if it exists."""
    target = path if path is not None else paths.palette_path()
    if not target.is_file():
        return None
    try:
        palette = Palette.load_template_output(target)
    except PaletteError:
        return None
    return ResolvedPalette(
        palette=palette,
        origin=Origin.TEMPLATE,
        detail=f"live from Noctalia via {target}",
    )


def from_current_wallpaper(scheme: str | None = None) -> ResolvedPalette | None:
    """Reproduce the palette Noctalia would derive from the live wallpaper."""
    try:
        wallpaper = noctalia.current_wallpaper()
        if wallpaper is None or not wallpaper.is_file():
            return None
        selection = noctalia.current_scheme_selection()
        mode = noctalia.current_mode()
    except noctalia.NoctaliaError:
        return None

    # Only meaningful when Noctalia is actually deriving its palette from the
    # wallpaper. For any other source, regenerating would show colours the rest
    # of the desktop is not using.
    if selection.source != "wallpaper":
        return None

    effective = scheme or selection.name or noctalia.DEFAULT_SCHEME
    try:
        pair = noctalia.generate(wallpaper, effective)
    except noctalia.NoctaliaError:
        return None

    return ResolvedPalette(
        palette=pair.for_mode(mode),
        origin=Origin.GENERATED,
        detail=f"generated from {wallpaper.name} with {effective}",
    )


def resolve(*, scheme: str | None = None) -> ResolvedPalette:
    """Best available palette. Never raises -- always returns something usable."""
    from_file = from_template()
    if from_file is not None:
        return from_file

    generated = from_current_wallpaper(scheme)
    if generated is not None:
        return generated

    mode: Mode = "dark"
    with contextlib.suppress(noctalia.NoctaliaError):
        mode = noctalia.current_mode()

    reason = (
        "Noctalia not available"
        if not noctalia.is_available()
        else "no rendered palette; run `wall-in-one --install-theme-template`"
    )
    return ResolvedPalette(
        palette=fallback_palette(mode),
        origin=Origin.FALLBACK,
        detail=reason,
    )
