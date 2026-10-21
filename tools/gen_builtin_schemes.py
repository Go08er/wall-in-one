#!/usr/bin/env python3
"""Regenerate ``src/wall_in_one/theme/builtin_schemes.py`` from Noctalia's source.

    tools/gen_builtin_schemes.py NOCTALIA_SOURCE_DIR [--check | --write]

``NOCTALIA_SOURCE_DIR`` is a checkout of the pinned Noctalia release (the
``noctalia`` input of ``flake.nix``; read-only is fine, for example its Nix
store path). The built-in palettes are read from ``src/theme/builtin_palettes.cpp``
and the release from ``VERSION``. With no flag the module is printed; ``--write``
replaces it; ``--check`` exits 1 when the checked-in file differs, which is how
``tests/test_builtin_schemes.py`` keeps it current.

Only the hex literals of that table are read. Nothing is invented: a table this
script cannot read in full is an error, not a guess.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

TARGET: Final = Path(__file__).resolve().parent.parent / "src/wall_in_one/theme/builtin_schemes.py"
SOURCE_FILE: Final = "src/theme/builtin_palettes.cpp"

#: C++ designated-initialiser name -> data module field, in the order they are emitted.
ROLES: Final[tuple[tuple[str, str], ...]] = (
    ("primary", "primary"),
    ("onPrimary", "on_primary"),
    ("secondary", "secondary"),
    ("onSecondary", "on_secondary"),
    ("tertiary", "tertiary"),
    ("onTertiary", "on_tertiary"),
    ("error", "error"),
    ("onError", "on_error"),
    ("surface", "surface"),
    ("onSurface", "on_surface"),
    ("surfaceVariant", "surface_variant"),
    ("onSurfaceVariant", "on_surface_variant"),
    ("outline", "outline"),
    ("shadow", "shadow"),
    ("hover", "hover"),
    ("onHover", "on_hover"),
)
ANSI: Final = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
TERMINAL_SCALARS: Final[tuple[tuple[str, str], ...]] = (
    ("foreground", "foreground"),
    ("background", "background"),
    ("cursor", "cursor"),
    ("cursorText", "cursor_text"),
    ("selectionFg", "selection_fg"),
    ("selectionBg", "selection_bg"),
)

_NAME = re.compile(r'\.name\s*=\s*"([^"]+)"')
_MODE = re.compile(r"\.(dark|light)\s*=")
_GROUP = re.compile(r"\.(normal|bright)\s*=")
_COLOUR = re.compile(r'\.(\w+)\s*=\s*hex\("(#[0-9A-Fa-f]{6})"\)')

Variant = dict[str, object]


class GenerationError(Exception):
    """The source is not shaped the way this script reads it."""


def parse(source: str) -> list[tuple[str, dict[str, Variant]]]:
    """Every built-in palette in ``source``: its name and its dark and light variants."""
    palettes: list[tuple[str, dict[str, Variant]]] = []
    name: str | None = None
    variants: dict[str, Variant] = {}
    variant: Variant | None = None
    group: dict[str, str] | None = None
    for line in source.splitlines():
        found = _NAME.search(line)
        if found:
            if name is not None:
                palettes.append((name, variants))
            name, variants, variant, group = found.group(1), {}, None, None
            continue
        found = _MODE.search(line)
        if found and name is not None:
            variant = {"normal": {}, "bright": {}}
            variants[found.group(1)] = variant
            group = None
            continue
        found = _GROUP.search(line)
        if found and variant is not None:
            group = variant[found.group(1)]  # type: ignore[assignment]
            continue
        found = _COLOUR.search(line)
        if found and variant is not None:
            key, value = found.group(1), found.group(2).lower()
            if key in ANSI:
                if group is None:
                    raise GenerationError(f"{name}: ansi colour {key} outside normal/bright")
                group[key] = value
            else:
                variant[key] = value
    if name is not None:
        palettes.append((name, variants))
    for palette, modes in palettes:
        if sorted(modes) != ["dark", "light"]:
            raise GenerationError(f"{palette}: expected a dark and a light variant")
        for mode, parsed in modes.items():
            wanted = [r for r, _ in ROLES] + [r for r, _ in TERMINAL_SCALARS]
            missing = [key for key in wanted if key not in parsed]
            for kind in ("normal", "bright"):
                missing += [f"{kind}.{c}" for c in ANSI if c not in parsed[kind]]  # type: ignore[operator]
            if missing:
                raise GenerationError(f"{palette} {mode}: missing {', '.join(missing)}")
    if not palettes:
        raise GenerationError("no built-in palettes found")
    return palettes


def _terminal(variant: Variant, indent: str) -> list[str]:
    lines = [f"{indent}terminal=BuiltinTerminal("]
    inner = indent + "    "
    for kind in ("normal", "bright"):
        colours: dict[str, str] = variant[kind]  # type: ignore[assignment]
        lines.append(f"{inner}{kind}=(")
        lines += [f'{inner}    "{colours[c]}",' for c in ANSI]
        lines.append(f"{inner}),")
    for source, field in TERMINAL_SCALARS:
        lines.append(f'{inner}{field}="{variant[source]}",')
    lines.append(f"{indent}),")
    return lines


def render(source: str, version: str) -> str:
    """The text of the data module for the palettes in ``source``."""
    out = [_HEADER.format(version=version, source=SOURCE_FILE).rstrip("\n")]
    for name, modes in parse(source):
        out.append(f'    BuiltinScheme(\n        name="{name}",')
        for mode in ("dark", "light"):
            out.append(f"        {mode}=BuiltinVariant(")
            for source_key, field in ROLES:
                out.append(f'            {field}="{modes[mode][source_key]}",')
            out += _terminal(modes[mode], "            ")
            out.append("        ),")
        out.append("    ),")
    out.append(")")
    return "\n".join(out) + "\n"


_HEADER = '''\
"""Noctalia's built-in palettes, as data, so each one can show a preview.

Generated by ``tools/gen_builtin_schemes.py``; do not edit by hand. Regenerate
it from Noctalia's source whenever the pinned version (``flake.nix``) changes.

Source: Noctalia v{version}, ``{source}`` (the ``kPalettes`` table). Those
palettes are compiled into the binary and no CLI surface prints their colours,
so the app carries them. Colours are copied verbatim (lower-cased); nothing here
is derived or invented. Noctalia 5.3.0's table is identical to 5.2.0's.

Noctalia is MIT licensed, Copyright (c) 2026 noctalia-dev. The palettes named
here are Noctalia's selections of those colour themes; this notice and the
licence at https://github.com/noctalia-dev/noctalia/blob/v{version}/LICENSE
apply to the copied table.

Only a *preview*: Noctalia's live files are never overridden by this table
(see ``palettes.builtin_colours``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class BuiltinTerminal:
    """The ``terminal`` block of one variant: 8 normal and 8 bright ansi colours."""

    #: black, red, green, yellow, blue, magenta, cyan, white
    normal: tuple[str, ...]
    bright: tuple[str, ...]
    foreground: str
    background: str
    cursor: str
    cursor_text: str
    selection_fg: str
    selection_bg: str


@dataclass(frozen=True, slots=True)
class BuiltinVariant:
    """One variant (dark or light): Noctalia's ``Palette`` roles and its terminal colours."""

    primary: str
    on_primary: str
    secondary: str
    on_secondary: str
    tertiary: str
    on_tertiary: str
    error: str
    on_error: str
    surface: str
    on_surface: str
    surface_variant: str
    on_surface_variant: str
    outline: str
    shadow: str
    hover: str
    on_hover: str
    terminal: BuiltinTerminal


@dataclass(frozen=True, slots=True)
class BuiltinScheme:
    name: str
    dark: BuiltinVariant
    light: BuiltinVariant


NOCTALIA_VERSION: Final = "{version}"

BUILTIN_SCHEMES: Final[tuple[BuiltinScheme, ...]] = (
'''


def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    positional = [a for a in argv if not a.startswith("--")]
    if len(positional) != 1 or flags - {"--check", "--write"}:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(positional[0])
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    text = render((root / SOURCE_FILE).read_text(encoding="utf-8"), version)
    if "--check" in flags:
        if TARGET.read_text(encoding="utf-8") != text:
            print(f"{TARGET} is out of date; run with --write", file=sys.stderr)
            return 1
        return 0
    if "--write" in flags:
        TARGET.write_text(text, encoding="utf-8")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
