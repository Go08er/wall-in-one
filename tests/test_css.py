from __future__ import annotations

import re

from wall_in_one.theme import css
from wall_in_one.theme.palette import Palette
from wall_in_one.theme.source import fallback_palette


def test_every_token_becomes_a_named_colour() -> None:
    palette = fallback_palette()
    stylesheet = css.render(palette)
    for token in palette.colours:
        assert f"@define-color wio_{token} " in stylesheet


def test_adwaita_names_are_all_defined() -> None:
    stylesheet = css.render(fallback_palette())
    for name, _token, _fallback in css._ADWAITA_MAPPING:
        assert f"@define-color {name} " in stylesheet


def test_adwaita_variables_use_the_same_colours_as_legacy_widgets() -> None:
    stylesheet = css.render(fallback_palette(), opacity=0.4)
    # Share the named definitions, including their fallback tokens and alpha,
    # instead of maintaining a second palette mapping for modern libadwaita.
    for name, _token, _fallback in css._ADWAITA_MAPPING:
        variable = "border-color" if name == "borders" else name.replace("_", "-")
        assert f"--{variable}: @{name};" in stylesheet
    assert "--borders:" not in stylesheet


def test_opaque_render_has_no_alpha_surfaces() -> None:
    stylesheet = css.render(fallback_palette(), opacity=1.0)
    assert not _alpha_definitions(stylesheet)
    assert _named_colour(stylesheet, "window_bg_color") == fallback_palette()["surface"].hex
    # The one exception is the badge scrim, which sits over a wallpaper image
    # rather than a themed surface and is translucent at every opacity.
    assert "rgba(" in stylesheet


def test_translucency_reaches_the_window_but_not_the_foreground() -> None:
    stylesheet = css.render(fallback_palette(), opacity=0.8)
    window = _named_colour(stylesheet, "window_bg_color")
    foreground = _named_colour(stylesheet, "window_fg_color")
    assert window.startswith("rgba(")
    assert not foreground.startswith("rgba(")
    # The literal window rule must carry it too -- GTK paints its own opaque
    # background underneath the themed one otherwise.
    assert "window.background" in stylesheet
    assert stylesheet.count("rgba(") >= 2


def test_opacity_is_clamped_into_range() -> None:
    assert not _alpha_definitions(css.render(fallback_palette(), opacity=5.0))
    low = css.render(fallback_palette(), opacity=-1.0)
    assert "0.000" in low


def test_missing_token_uses_its_fallback() -> None:
    # A palette from an older Noctalia without outline_variant should still
    # produce a complete stylesheet.
    palette = Palette.from_mapping(
        "dark",
        {
            "primary": "#a5c8ff",
            "on_primary": "#00315e",
            "secondary": "#bcc7dc",
            "on_secondary": "#263141",
            "error": "#ffb4ab",
            "on_error": "#690005",
            "surface": "#131318",
            "on_surface": "#e4e1e9",
            "surface_variant": "#44464f",
            "outline": "#8f909a",
            "shadow": "#000000",
        },
    )
    stylesheet = css.render(palette)
    assert _named_colour(stylesheet, "borders") == "#8f909a"
    assert _named_colour(stylesheet, "success_bg_color") == "#a5c8ff"


def _alpha_definitions(stylesheet: str) -> list[str]:
    """Named colours carrying alpha -- the themed surfaces, not the overlays."""
    return [
        line
        for line in stylesheet.splitlines()
        if line.startswith("@define-color") and "rgba(" in line
    ]


def _named_colour(stylesheet: str, name: str) -> str:
    match = re.search(rf"@define-color {re.escape(name)} ([^;]+);", stylesheet)
    assert match is not None, f"{name} is not defined"
    return match.group(1).strip()


# -- the new window's glass layers ------------------------------------------------


def test_without_glass_the_stylesheet_is_the_classic_one() -> None:
    palette = fallback_palette()
    classic = css.render(palette, opacity=0.4)
    assert css.render(palette, opacity=0.4, glass=None) == classic
    assert "wio-glass" not in classic


def test_glass_is_scoped_to_the_new_window_and_cut_from_the_palette() -> None:
    palette = fallback_palette()
    stylesheet = css.render(palette, glass=css.Glass(background=0.3, panel=0.6))
    glass = stylesheet.split("window.wio-glass,", 1)[1]
    # Everything after the classic rules is scoped to the new window's class.
    for line in glass.splitlines():
        if line.rstrip().endswith(("{", ",")) and not line.startswith(" "):
            assert line.startswith("window.wio-glass"), line
    surface = palette["surface"]
    assert f"rgba({surface.red}, {surface.green}, {surface.blue}, 0.300)" in glass
    sidebar = palette.get("surface_container_low", "surface")
    assert (
        f"--sidebar-bg-color: rgba({sidebar.red}, {sidebar.green}, {sidebar.blue}, 0.600)" in glass
    )
    # Cards sit on the page: 0.3 under (0.6 - 0.3) / 0.7 shows 0.6 in total.
    card = palette.get("surface_container", "surface_variant")
    assert f"--card-bg-color: rgba({card.red}, {card.green}, {card.blue}, 0.429)" in glass
    # Dialogs and popovers float above the glass and stay solid.
    assert f"--card-bg-color: {card.hex}" in glass


def test_an_element_never_adds_paint_below_the_page_it_sits_on() -> None:
    assert css.on_background(0.2, 0.8) == 0.0
    assert css.on_background(0.8, 0.8) == 0.0
    assert css.on_background(1.0, 0.5) == 1.0
    stacked = 0.3 + (1 - 0.3) * css.on_background(0.75, 0.3)
    assert abs(stacked - 0.75) < 1e-9


def test_glass_dials_are_clamped() -> None:
    surfaces = css.glass_surfaces(fallback_palette())
    assert set(surfaces) == set(css.GLASS_SURFACES)
    layers = css.glass_layers(surfaces, background=-1.0, panel=7.0)
    assert ", 0.000)" in layers and ", 1.000)" in layers
    assert "nan" not in layers
