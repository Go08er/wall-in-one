"""Every built-in palette shows a preview, from a copy of Noctalia's own table.

The ten built-in palettes are compiled into the Noctalia binary, so no file
holds their colours. `theme/builtin_schemes.py` carries them (generated from
Noctalia's `src/theme/builtin_palettes.cpp` by `tools/gen_builtin_schemes.py`);
`palettes.discover` fills `PaletteEntry.colours` from it, and both windows draw
their swatches from that field. The wallpaper-generated schemes
(`noctalia.ALL_SCHEMES`) are not palettes: their colours depend on the still
and are always generated live, so they are not in the table.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from types import ModuleType

import pytest

from wall_in_one.theme import builtin_schemes, noctalia, palettes
from wall_in_one.theme.palettes import Origin

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "gen_builtin_schemes.py"
#: A checkout of the pinned Noctalia release, e.g. its read-only Nix store path.
_SOURCE_ENV = "WIO_NOCTALIA_SOURCE"


def _discovered() -> palettes.Discovery:
    return palettes.discover(custom=Path("/nonexistent"), community=Path("/nonexistent"))


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_builtin_schemes", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_table_covers_exactly_the_listed_builtin_names() -> None:
    assert [s.name for s in builtin_schemes.BUILTIN_SCHEMES] == sorted(palettes.BUILTIN_NAMES)


def test_every_builtin_palette_has_a_preview_in_both_variants() -> None:
    found = _discovered().of_origin(Origin.BUILTIN)
    assert {entry.name for entry in found} == set(palettes.BUILTIN_NAMES)
    for entry in found:
        for mode in ("dark", "light"):
            variant = entry.for_mode(mode)
            assert variant is not None, (entry.name, mode)
            for token in ("primary", "secondary", "tertiary", "error", "surface", "outline"):
                assert token in variant.colours, (entry.name, mode, token)
            assert "terminal_normal_red" in variant.colours


def test_generated_schemes_are_not_builtin_palettes() -> None:
    """Generation strategies need a still; they never have a table entry."""
    assert not set(noctalia.ALL_SCHEMES) & set(palettes.BUILTIN_NAMES)
    assert palettes.builtin_colours("m3-tonal-spot") is None


@pytest.mark.parametrize(
    ("name", "mode", "token", "expected"),
    [
        # builtin_palettes.cpp (Noctalia v5.2.0): Ayu line 20, 73; Nord lines 784-799.
        ("Ayu", "dark", "primary", "#e6b450"),
        ("Ayu", "dark", "surface", "#0b0e14"),
        ("Ayu", "light", "primary", "#ff8f40"),
        ("Ayu", "dark", "terminal_normal_red", "#ea6c73"),
        ("Nord", "dark", "primary", "#8fbcbb"),
        ("Nord", "dark", "surface", "#2e3440"),
        ("Nord", "dark", "outline", "#505a70"),
        ("Nord", "light", "primary", "#5e81ac"),
    ],
)
def test_colours_are_pinned_to_noctalias_source(
    name: str, mode: str, token: str, expected: str
) -> None:
    pair = palettes.builtin_colours(name)
    assert pair is not None
    assert pair.for_mode(mode).colours[token].hex == expected  # type: ignore[arg-type]


def test_a_name_not_in_the_table_has_no_preview() -> None:
    found = palettes.discover(
        custom=Path("/nonexistent"), community=Path("/nonexistent"), builtins=("Nord", "Unknown")
    )
    assert found.find(Origin.BUILTIN, "Nord") is not None
    unknown = found.find(Origin.BUILTIN, "Unknown")
    assert unknown is not None and unknown.colours is None
    assert unknown.for_mode("dark") is None


def test_the_generator_refuses_a_table_it_cannot_read_in_full() -> None:
    tool = _tool()
    with pytest.raises(tool.GenerationError):
        tool.parse('{ .name = "Half", .dark = FixedPaletteMode{ .primary = hex("#112233"), } }')
    with pytest.raises(tool.GenerationError):
        tool.parse("nothing here")


def test_the_checked_in_table_matches_the_pinned_noctalia_source() -> None:
    root = os.environ.get(_SOURCE_ENV)
    if not root:
        pytest.skip(f"set {_SOURCE_ENV} to a checkout of the pinned Noctalia release")
    source = Path(root)
    tool = _tool()
    version = (source / "VERSION").read_text(encoding="utf-8").strip()
    assert version == builtin_schemes.NOCTALIA_VERSION
    generated = tool.render(
        (source / tool.SOURCE_FILE).read_text(encoding="utf-8"),
        version,
        (source / "LICENSE").read_text(encoding="utf-8"),
    )
    assert tool.TARGET.read_text(encoding="utf-8") == generated, (
        "theme/builtin_schemes.py is out of date with the pinned Noctalia source; run "
        f"tools/gen_builtin_schemes.py {source} --write"
    )


def _table(*, names: list[str] | None = None, extra: str = "") -> str:
    """A complete synthetic table in Noctalia's layout, `extra` added to the first variant."""
    tool = _tool()
    colours = "\n".join(
        f'.{key} = hex("#102030"),'
        for key in [r for r, _ in tool.ROLES]
        + ["foreground"]
        + [f for f, _ in tool.TERMINAL_SCALARS if f != "foreground"]
    )
    ansi = "\n".join(f'.{key} = hex("#102030"),' for key in tool.ANSI)

    def variant(mode: str, more: str) -> str:
        return (
            f".{mode} = FixedPaletteMode{{\n{colours}\n{more}\n"
            f".normal = TerminalAnsiColors{{\n{ansi} }},\n"
            f".bright = TerminalAnsiColors{{\n{ansi} }}, }},\n"
        )

    return "".join(
        f'.name = "{name}",\n{variant("dark", extra if index == 0 else "")}{variant("light", "")}'
        for index, name in enumerate(names or [f"P{n}" for n in range(tool.EXPECTED_PALETTES)])
    )


def test_a_complete_synthetic_table_parses() -> None:
    tool = _tool()
    assert len(tool.parse(_table())) == tool.EXPECTED_PALETTES


def test_the_generator_refuses_a_repeated_palette_name() -> None:
    tool = _tool()
    names = [f"P{n}" for n in range(tool.EXPECTED_PALETTES - 1)] + ["P0"]
    with pytest.raises(tool.GenerationError, match="twice"):
        tool.parse(_table(names=names))


def test_the_generator_refuses_a_repeated_key_in_a_variant() -> None:
    tool = _tool()
    with pytest.raises(tool.GenerationError, match="twice"):
        tool.parse(_table(extra='.primary = hex("#445566"),'))
    with pytest.raises(tool.GenerationError, match="twice"):
        tool.parse(_table(extra='.dark = FixedPaletteMode{\n.primary = hex("#445566"),'))


def test_the_generator_refuses_an_unexpected_palette_count() -> None:
    tool = _tool()
    with pytest.raises(tool.GenerationError, match="expected"):
        tool.parse(_table(names=["A", "B"]))


def test_the_pinned_tag_in_the_flake_is_the_table_version() -> None:
    flake = Path(__file__).resolve().parent.parent / "flake.nix"
    if not flake.is_file():
        pytest.skip("no flake.nix beside the tests (a packaged build)")
    tags = re.findall(r"github:noctalia-dev/noctalia/v(\d+\.\d+\.\d+)", flake.read_text("utf-8"))
    assert len(set(tags)) == 1, tags
    assert tags[0] == builtin_schemes.NOCTALIA_VERSION, (
        "flake.nix pins a different Noctalia than theme/builtin_schemes.py was made from; run "
        "tools/gen_builtin_schemes.py <source of the new pin> --write"
    )


def test_the_module_carries_noctalias_full_licence() -> None:
    doc = builtin_schemes.__doc__ or ""
    assert "Copyright (c) 2026 noctalia-dev" in doc
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in doc
    assert "Permission is hereby granted" in doc
