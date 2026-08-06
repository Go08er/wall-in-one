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


def test_opaque_render_has_no_alpha_backgrounds() -> None:
    stylesheet = css.render(fallback_palette(), opacity=1.0)
    assert "rgba(" not in stylesheet


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
    assert "rgba(" not in css.render(fallback_palette(), opacity=5.0)
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


def _named_colour(stylesheet: str, name: str) -> str:
    match = re.search(rf"@define-color {re.escape(name)} ([^;]+);", stylesheet)
    assert match is not None, f"{name} is not defined"
    return match.group(1).strip()
