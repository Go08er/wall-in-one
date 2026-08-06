from __future__ import annotations

import json

import pytest

from wall_in_one.theme.palette import Colour, Palette, PaletteError, PalettePair
from wall_in_one.theme.tokens import ALL_TOKENS, TOKEN_COUNT


def test_token_set_matches_noctalia() -> None:
    # Guards against a token being dropped or duplicated in tokens.py. The
    # count is from noctalia/src/theme/tokens.h.
    assert TOKEN_COUNT == 72
    assert len(set(ALL_TOKENS)) == TOKEN_COUNT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("#abc", (0xAA, 0xBB, 0xCC, 1.0)),
        ("#a5c8ff", (0xA5, 0xC8, 0xFF, 1.0)),
        ("#A5C8FF", (0xA5, 0xC8, 0xFF, 1.0)),
        ("  #a5c8ff  ", (0xA5, 0xC8, 0xFF, 1.0)),
        ("#a5c8ff80", (0xA5, 0xC8, 0xFF, 128 / 255)),
    ],
)
def test_colour_parse(text: str, expected: tuple[int, int, int, float]) -> None:
    colour = Colour.parse(text)
    assert (colour.red, colour.green, colour.blue) == expected[:3]
    assert colour.alpha == pytest.approx(expected[3])


@pytest.mark.parametrize("text", ["a5c8ff", "#gggggg", "#12345", "", "#"])
def test_colour_parse_rejects_junk(text: str) -> None:
    with pytest.raises(PaletteError):
        Colour.parse(text)


def test_colour_css_is_opaque_hex_until_alpha_applies() -> None:
    colour = Colour.parse("#102030")
    assert colour.css() == "#102030"
    assert colour.css(1.0) == "#102030"
    assert colour.css(0.5) == "rgba(16, 32, 48, 0.500)"


def test_luminance_orders_black_below_white() -> None:
    assert Colour.parse("#000000").relative_luminance == pytest.approx(0.0)
    assert Colour.parse("#ffffff").relative_luminance == pytest.approx(1.0)
    assert Colour.parse("#000000").is_dark
    assert not Colour.parse("#ffffff").is_dark


def test_pair_from_generator_output() -> None:
    document = json.dumps({"dark": {"primary": "#a5c8ff"}, "light": {"primary": "#3c6090"}})
    pair = PalettePair.from_json(document)
    assert pair.for_mode("dark")["primary"].hex == "#a5c8ff"
    assert pair.for_mode("light")["primary"].hex == "#3c6090"


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        json.dumps({"dark": {"primary": "#a5c8ff"}}),
        json.dumps({"dark": {}, "light": {"primary": "#fff"}}),
        json.dumps({"dark": {"primary": 12}, "light": {"primary": "#fff"}}),
    ],
)
def test_pair_rejects_malformed(document: str) -> None:
    with pytest.raises(PaletteError):
        PalettePair.from_json(document)


def test_palette_from_template_document() -> None:
    # The shape our Noctalia user template renders: one mode, tagged.
    document = json.dumps({"mode": "dark", "colors": {"primary": "#a5c8ff"}})
    palette = Palette.from_template_document(document)
    assert palette.mode == "dark"
    assert palette["primary"].hex == "#a5c8ff"


@pytest.mark.parametrize(
    "document",
    [
        json.dumps({"colors": {"primary": "#fff"}}),
        json.dumps({"mode": "sepia", "colors": {"primary": "#fff"}}),
        json.dumps({"mode": "dark"}),
    ],
)
def test_template_document_rejects_malformed(document: str) -> None:
    with pytest.raises(PaletteError):
        Palette.from_template_document(document)


def test_get_falls_back_for_tokens_an_older_noctalia_lacks() -> None:
    palette = Palette.from_mapping("dark", {"outline": "#8f909a"})
    assert palette.get("outline_variant", "outline").hex == "#8f909a"
    with pytest.raises(PaletteError):
        palette["outline_variant"]


def test_missing_tokens_reports_the_gap() -> None:
    palette = Palette.from_mapping("dark", {"primary": "#a5c8ff"})
    missing = palette.missing_tokens
    assert "primary" not in missing
    assert "surface" in missing
    assert len(missing) == TOKEN_COUNT - 1
