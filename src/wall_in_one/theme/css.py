"""Render a palette as GTK CSS.

Two layers come out of this:

* Every token as a ``@define-color`` named ``wio_<token>``, so any widget can
  reach the full Noctalia palette by name.
* Overrides for both libadwaita's named colours and CSS variables, so stock
  widgets pick the palette up without each one needing a rule.

Translucency is applied only to the window background. Making every surface
translucent stacks alpha and turns text muddy -- one translucent plane with the
compositor blurring behind it is what actually looks right.

The new interface (``--ui=next``) has its own see-through styles instead,
chosen in ``ui.toml``: a page-background dial and a panel dial. `render` adds
them as one extra section, scoped to ``window.wio-glass``, so the classic
window never matches it; see `glass_layers`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from wall_in_one.theme.palette import Colour, Palette

#: libadwaita named colour -> the Noctalia token that should drive it, with a
#: fallback token for palettes written by an older Noctalia that predates it.
#: See https://gnome.pages.gitlab.gnome.org/libadwaita/doc/main/css-variables.html
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


def adwaita_variables() -> Iterable[str]:
    """Keep modern widgets on the same live colours as legacy named consumers.

    Noctalia's startup-loaded GTK stylesheet defines CSS variables explicitly;
    refreshing only the named colours leaves those variables stale. Alias them
    in our live provider so fallback tokens and translucency stay identical.
    """
    yield ":root {"
    for name, _token, _fallback in _ADWAITA_MAPPING:
        variable = "border-color" if name == "borders" else name.replace("_", "-")
        yield f"    --{variable}: @{name};"
    yield "}"


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

/* A live-sort row is still the real row, not a drag icon. These transitions
   soften only lift/drop state; position and scale use the measured FLIP path. */
.wio-reorder-handle {{
    min-width: 46px;
    min-height: 72px;
    padding: 0;
    border-radius: 9px;
}}

.wio-reorder-row {{
    transition: box-shadow 140ms ease, opacity 140ms ease;
}}

.wio-reorder-lifted {{
    background-color: @card_bg_color;
    border-radius: 12px;
    box-shadow: 0 22px 48px alpha(@shade_color, 0.42);
    opacity: 0.98;
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


@dataclass(frozen=True, slots=True)
class Glass:
    """The new window's two opacity dials, each from 0 (clear) to 1 (solid).

    ``background`` is the page behind lists and grids; ``panel`` is what sits
    on it or beside it: the sidebar, header, player bar, inspector and cards.
    """

    background: float
    panel: float


#: The libadwaita surfaces `glass_layers` paints, as `adwaita_definitions`
#: resolves them from the palette.
GLASS_SURFACES: Final[tuple[str, ...]] = (
    "window",
    "view",
    "headerbar",
    "sidebar",
    "secondary-sidebar",
    "card",
)


def glass_surfaces(palette: Palette) -> dict[str, str]:
    """The opaque surface colours glass is cut from, keyed as in `GLASS_SURFACES`."""
    tokens = {name: (token, fallback) for name, token, fallback in _ADWAITA_MAPPING}
    return {
        surface: palette.get(*tokens[f"{surface.replace('-', '_')}_bg_color"]).hex
        for surface in GLASS_SURFACES
    }


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def on_background(panel: float, background: float) -> float:
    """CSS alpha for an element on the page background, so the two layers show ``panel``.

    An element cannot be clearer than the page under it, so below
    ``background`` it adds nothing at all.
    """
    if panel <= background:
        return 0.0
    return (panel - background) / (1.0 - background) if background < 1.0 else 1.0


def _rgba(hex_colour: str, alpha: float) -> str:
    value = hex_colour.lstrip("#")
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {alpha:.3f})"


def glass_layers(surfaces: Mapping[str, str], background: float, panel: float) -> str:
    """One painted layer per region of the new window, so each dial shows what it says.

    The window itself is clear. Regions straight on the desktop (or on the
    frosted backdrop) paint once: the sidebar, the content header and the
    player bar at ``panel``; the page under the content at ``background``.
    Elements on the page (cards, lists, the inspector) are solved with
    `on_background`, so the stack still shows ``panel``. Stacking layers at
    the same alpha is what makes 75% look solid: 0.75 over 0.75 is 0.94.

    ``surfaces`` maps every name in `GLASS_SURFACES` to an opaque ``#rrggbb``.
    Every rule is scoped to ``window.wio-glass``, which only the new window
    sets, and only in its translucent and frosted styles. Dialogs and
    popovers float above everything and go back to solid surfaces.
    """
    background, panel = _unit(background), _unit(panel)
    on_page = on_background(panel, background)
    sidebar = _rgba(surfaces["sidebar"], panel)
    header = _rgba(surfaces["headerbar"], panel)
    page = _rgba(surfaces["window"], background)
    values = [
        f"--window-bg-color: {page}",
        f"--view-bg-color: {_rgba(surfaces['view'], on_page)}",
        f"--headerbar-bg-color: {header}",
        f"--headerbar-backdrop-color: {header}",
        f"--sidebar-bg-color: {sidebar}",
        f"--sidebar-backdrop-color: {sidebar}",
        f"--secondary-sidebar-bg-color: {_rgba(surfaces['secondary-sidebar'], on_page)}",
        f"--card-bg-color: {_rgba(surfaces['card'], on_page)}",
    ]
    solid = [
        f"--{name}-bg-color: {surfaces[name]}"
        for name in ("window", "view", "card", "headerbar", "sidebar", "secondary-sidebar")
    ]
    solid += [
        f"--headerbar-backdrop-color: {surfaces['headerbar']}",
        f"--sidebar-backdrop-color: {surfaces['sidebar']}",
    ]
    content = "window.wio-glass navigation-view-page.wio-content-page > toolbarview"
    inspector = _rgba(surfaces["secondary-sidebar"], on_page)
    return f"""
window.wio-glass,
window.wio-glass.background,
window.wio-glass.background.csd {{
    background-color: transparent;
}}

window.wio-glass {{
    {"; ".join(values)};
}}

/* Straight on the desktop: one layer each. Both shell pages are tagged, so
   this holds side by side and when the split view collapses into a
   navigation view, which would otherwise paint its own page backgrounds. */
window.wio-glass navigation-split-view > widget.sidebar-pane,
window.wio-glass navigation-split-view > widget.content-pane,
window.wio-glass navigation-split-view navigation-view-page {{
    background-color: transparent;
}}

window.wio-glass navigation-view-page.wio-sidebar-page {{
    background-color: {sidebar};
}}

{content} > revealer.top-bar {{
    background-color: {header};
}}

{content} > stack {{
    background-color: {page};
}}

/* On the page: the inspector pane paints once and its contents stay clear.
   Floating over the grid in a narrow window it is solid, like a popover. */
window.wio-glass overlay-split-view > widget.sidebar-pane {{
    background-color: {inspector};
}}

window.wio-glass overlay-split-view > widget.background {{
    background-color: {surfaces["secondary-sidebar"]};
}}

window.wio-glass .inspector {{
    background-color: transparent;
}}

window.wio-glass banner > revealer > widget {{
    background-color: alpha(currentColor, 0.07);
}}

window.wio-glass dialog,
window.wio-glass popover {{
    {"; ".join(solid)};
}}
""".strip()


def render(palette: Palette, *, opacity: float = 1.0, glass: Glass | None = None) -> str:
    """Build the full stylesheet for ``palette`` at the given window opacity.

    ``glass`` adds the new window's glass layers (see `glass_layers`). Without
    it the stylesheet is exactly what the classic window has always had.
    """
    clamped = min(1.0, max(0.0, opacity))
    sections = [
        "/* generated by wall-in-one -- do not edit */",
        f"/* mode: {palette.mode}  tokens: {len(palette.colours)}  opacity: {clamped:.2f} */",
        "\n".join(token_definitions(palette)),
        "\n".join(adwaita_definitions(palette, clamped)),
        "\n".join(adwaita_variables()),
        _structural_rules(palette, clamped),
    ]
    if glass is not None:
        sections.append(glass_layers(glass_surfaces(palette), glass.background, glass.panel))
    return "\n\n".join(sections) + "\n"


def contrasting_foreground(background: Colour, palette: Palette) -> Colour:
    """Pick the palette's light or dark 'on' colour for an arbitrary swatch.

    Used for overlay text on wallpaper thumbnails, where the backdrop is an
    image rather than a themed surface.
    """
    if background.is_dark:
        return palette.get("inverse_on_surface", "on_surface")
    return palette["on_surface"]
