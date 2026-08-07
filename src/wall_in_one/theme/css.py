"""Render a palette as GTK CSS.

Two layers come out of this:

* Every token as a ``@define-color`` named ``wio_<token>``, so any widget can
  reach the full Noctalia palette by name.
* Overrides for the libadwaita named colours, so stock widgets pick the palette
  up without each one needing a rule.

Translucency is applied only to the window background. Making every surface
translucent stacks alpha and turns text muddy -- one translucent plane with the
compositor blurring behind it is what actually looks right.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from wall_in_one.theme.palette import Colour, Palette

#: libadwaita named colour -> the Noctalia token that should drive it, with a
#: fallback token for palettes written by an older Noctalia that predates it.
#: See https://gnome.pages.gitlab.gnome.org/libadwaita/doc/main/named-colors.html
_ADWAITA_MAPPING: Final[tuple[tuple[str, str, str], ...]] = (
    ("accent_color", "primary", "primary"),
    ("accent_bg_color", "primary", "primary"),
    ("accent_fg_color", "on_primary", "on_primary"),
    ("destructive_color", "error", "error"),
    ("destructive_bg_color", "error", "error"),
    ("destructive_fg_color", "on_error", "on_error"),
    ("success_color", "tertiary", "primary"),
    ("success_bg_color", "tertiary", "primary"),
    ("success_fg_color", "on_tertiary", "on_primary"),
    ("warning_color", "secondary", "primary"),
    ("warning_bg_color", "secondary", "primary"),
    ("warning_fg_color", "on_secondary", "on_primary"),
    ("error_color", "error", "error"),
    ("error_bg_color", "error", "error"),
    ("error_fg_color", "on_error", "on_error"),
    ("window_bg_color", "surface", "surface"),
    ("window_fg_color", "on_surface", "on_surface"),
    ("view_bg_color", "surface_container_low", "surface"),
    ("view_fg_color", "on_surface", "on_surface"),
    ("headerbar_bg_color", "surface_container", "surface"),
    ("headerbar_fg_color", "on_surface", "on_surface"),
    ("headerbar_border_color", "outline_variant", "outline"),
    ("headerbar_backdrop_color", "surface_dim", "surface"),
    ("sidebar_bg_color", "surface_container_low", "surface"),
    ("sidebar_fg_color", "on_surface", "on_surface"),
    ("sidebar_backdrop_color", "surface_dim", "surface"),
    ("sidebar_border_color", "outline_variant", "outline"),
    ("secondary_sidebar_bg_color", "surface_container_lowest", "surface"),
    ("secondary_sidebar_fg_color", "on_surface", "on_surface"),
    ("card_bg_color", "surface_container", "surface_variant"),
    ("card_fg_color", "on_surface", "on_surface"),
    ("dialog_bg_color", "surface_container_high", "surface_variant"),
    ("dialog_fg_color", "on_surface", "on_surface"),
    ("popover_bg_color", "surface_container_high", "surface_variant"),
    ("popover_fg_color", "on_surface", "on_surface"),
    ("thumbnail_bg_color", "surface_container", "surface_variant"),
    ("thumbnail_fg_color", "on_surface", "on_surface"),
    ("shade_color", "shadow", "shadow"),
    ("scrim_color", "scrim", "shadow"),
    ("borders", "outline_variant", "outline"),
)

#: Named colours whose job is to sit behind the window and therefore inherit the
#: translucency setting. Everything else stays opaque so text keeps its
#: contrast.
_TRANSLUCENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "window_bg_color",
        "view_bg_color",
        "headerbar_bg_color",
        "headerbar_backdrop_color",
        "sidebar_bg_color",
        "sidebar_backdrop_color",
        "secondary_sidebar_bg_color",
    }
)


def _define(name: str, value: str) -> str:
    return f"@define-color {name} {value};"


def token_definitions(palette: Palette) -> Iterable[str]:
    """``@define-color wio_<token>`` for every token the palette carries."""
    for token in sorted(palette.colours):
        yield _define(f"wio_{token}", palette[token].hex)


def adwaita_definitions(palette: Palette, opacity: float) -> Iterable[str]:
    for name, token, fallback in _ADWAITA_MAPPING:
        colour = palette.get(token, fallback)
        alpha = opacity if name in _TRANSLUCENT_NAMES else 1.0
        yield _define(name, colour.css(alpha))


def _structural_rules(palette: Palette, opacity: float) -> str:
    """Rules that named colours alone cannot express.

    GTK paints an opaque window background of its own beneath the themed one,
    so the window and its immediate background children have to be cleared
    explicitly for translucency to reach the compositor.
    """
    surface = palette["surface"]
    outline = palette.get("outline_variant", "outline")
    container = palette.get("surface_container", "surface")
    primary = palette["primary"]
    on_primary = palette.get("on_primary", "on_surface")
    # Badges sit on top of the wallpaper image, not on a themed surface, so
    # they need their own scrim to stay readable over an arbitrary picture.
    scrim = palette.get("scrim", "shadow").css(0.55)
    window_background = surface.hex if opacity >= 1.0 else surface.css(opacity)

    return f"""
window.background,
window.background.csd {{
    background-color: {window_background};
}}

.wio-translucent-surface {{
    background-color: {window_background};
}}

.wio-hairline {{
    border-color: {outline.hex};
}}

/* The wallpaper grid. Tiles are images, so they take their colour from the
   palette only at their edges and in the "this one is up" marker. */
.wio-tile-image {{
    border-radius: 10px;
}}

.wio-tile-blank {{
    background-color: {container.hex};
    border-radius: 10px;
}}

.wio-tile-current .wio-tile-image {{
    outline: 3px solid {primary.hex};
    outline-offset: -3px;
}}

.wio-tile-current label {{
    color: {primary.hex};
    font-weight: bold;
}}

.wio-badge {{
    background-color: {scrim};
    color: {on_primary.hex};
    border-radius: 6px;
    padding: 2px 6px;
}}

/* The star cannot carry its own state: in several icon themes -- Papirus
   among them -- `starred-symbolic` and `non-starred-symbolic` are both solid
   stars, and once symbolic recolouring flattens them they are the same
   picture. So the colour says it, from the palette rather than from whatever
   the icon theme happened to ship. */
.wio-star {{
    color: {on_primary.hex};
    opacity: 0.55;
}}

.wio-star:checked {{
    color: {primary.hex};
    opacity: 1;
}}

/* Both overlay buttons stay out of the way until the tile is pointed at or
   focused, so five tiles are five wallpapers rather than ten buttons. */
.wio-tile-action {{
    opacity: 0;
}}

.wio-tile:hover .wio-tile-action,
.wio-tile-action:focus,
.wio-tile-action:focus-within,
.wio-tile-action:checked {{
    opacity: 1;
}}
""".strip()


def render(palette: Palette, *, opacity: float = 1.0) -> str:
    """Build the full stylesheet for ``palette`` at the given window opacity."""
    clamped = min(1.0, max(0.0, opacity))
    sections = (
        "/* generated by wall-in-one -- do not edit */",
        f"/* mode: {palette.mode}  tokens: {len(palette.colours)}  opacity: {clamped:.2f} */",
        "\n".join(token_definitions(palette)),
        "\n".join(adwaita_definitions(palette, clamped)),
        _structural_rules(palette, clamped),
    )
    return "\n\n".join(sections) + "\n"


def contrasting_foreground(background: Colour, palette: Palette) -> Colour:
    """Pick the palette's light or dark 'on' colour for an arbitrary swatch.

    Used for overlay text on wallpaper thumbnails, where the backdrop is an
    image rather than a themed surface.
    """
    if background.is_dark:
        return palette.get("inverse_on_surface", "on_surface")
    return palette["on_surface"]
