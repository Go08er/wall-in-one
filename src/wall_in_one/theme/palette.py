"""Palette model: a token name -> colour mapping, in dark and light variants."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self

from wall_in_one.theme.tokens import ALL_TOKENS

Mode = Literal["dark", "light"]

_HEX_RE: Final = re.compile(r"\A#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")

#: Ceiling on a palette document we will parse. Noctalia's own output is ~5 KiB;
#: this is only here so a corrupt or hostile file cannot exhaust memory.
MAX_PALETTE_BYTES: Final = 256 * 1024


class PaletteError(Exception):
    """A palette document was missing, malformed, or incomplete."""


@dataclass(frozen=True, slots=True)
class Colour:
    """An sRGB colour with 8-bit channels and an alpha."""

    red: int
    green: int
    blue: int
    alpha: float = 1.0

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse ``#rgb``, ``#rrggbb`` or ``#rrggbbaa``."""
        text = value.strip()
        if not _HEX_RE.match(text):
            raise PaletteError(f"not a hex colour: {value!r}")
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(char * 2 for char in digits)
        red, green, blue = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
        alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
        return cls(red, green, blue, alpha)

    @property
    def hex(self) -> str:
        return f"#{self.red:02x}{self.green:02x}{self.blue:02x}"

    def css(self, alpha: float | None = None) -> str:
        """CSS colour literal, as ``rgba()`` when translucent."""
        effective = self.alpha if alpha is None else alpha
        if effective >= 1.0:
            return self.hex
        return f"rgba({self.red}, {self.green}, {self.blue}, {effective:.3f})"

    @property
    def relative_luminance(self) -> float:
        """WCAG relative luminance, for picking readable foregrounds."""

        def channel(raw: int) -> float:
            value = raw / 255
            return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

        return (
            0.2126 * channel(self.red) + 0.7152 * channel(self.green) + 0.0722 * channel(self.blue)
        )

    @property
    def is_dark(self) -> bool:
        return self.relative_luminance < 0.5


@dataclass(frozen=True, slots=True)
class Palette:
    """One variant (dark or light) of a generated palette."""

    mode: Mode
    colours: Mapping[str, Colour]

    def __getitem__(self, token: str) -> Colour:
        try:
            return self.colours[token]
        except KeyError:
            raise PaletteError(f"palette has no token {token!r}") from None

    def get(self, token: str, fallback: str) -> Colour:
        """Look up ``token``, falling back to another token if it is absent.

        Noctalia has added tokens over time, so a palette written by an older
        version can be short a few. Callers that can degrade gracefully use
        this rather than failing the whole load.
        """
        found = self.colours.get(token)
        if found is not None:
            return found
        return self[fallback]

    @classmethod
    def from_mapping(cls, mode: Mode, raw: Mapping[str, object]) -> Self:
        colours: dict[str, Colour] = {}
        for token, value in raw.items():
            if not isinstance(value, str):
                raise PaletteError(f"token {token!r} is not a string")
            colours[token] = Colour.parse(value)
        if not colours:
            raise PaletteError(f"{mode} palette is empty")
        return cls(mode=mode, colours=colours)

    @property
    def missing_tokens(self) -> tuple[str, ...]:
        """Canonical tokens this palette does not define."""
        return tuple(token for token in ALL_TOKENS if token not in self.colours)

    @classmethod
    def from_template_document(cls, document: str | bytes) -> Self:
        """Parse what our Noctalia user template renders.

        A different shape from :meth:`PalettePair.from_json`: the template runs
        once for whichever mode is active, so it emits a single variant tagged
        with its mode rather than a dark/light pair.
        """
        try:
            decoded = json.loads(document)
        except ValueError as error:
            raise PaletteError(f"palette is not valid JSON: {error}") from error
        if not isinstance(decoded, dict):
            raise PaletteError("palette document must be an object")

        mode = decoded.get("mode")
        if mode not in ("dark", "light"):
            raise PaletteError(f"palette has an unusable mode: {mode!r}")
        raw = decoded.get("colors")
        if not isinstance(raw, dict):
            raise PaletteError("palette document is missing a 'colors' object")
        return cls.from_mapping(mode, raw)

    @classmethod
    def load_template_output(cls, path: Path) -> Self:
        try:
            size = path.stat().st_size
        except OSError as error:
            raise PaletteError(f"cannot stat palette {path}: {error}") from error
        if size > MAX_PALETTE_BYTES:
            raise PaletteError(
                f"palette {path} is {size} bytes, over the {MAX_PALETTE_BYTES} limit"
            )
        try:
            return cls.from_template_document(path.read_bytes())
        except OSError as error:
            raise PaletteError(f"cannot read palette {path}: {error}") from error


@dataclass(frozen=True, slots=True)
class PalettePair:
    """The dark and light variants Noctalia emits together."""

    dark: Palette
    light: Palette

    def for_mode(self, mode: Mode) -> Palette:
        return self.dark if mode == "dark" else self.light

    @classmethod
    def from_json(cls, document: str | bytes) -> Self:
        """Parse the ``--both`` form: ``{"dark": {...}, "light": {...}}``."""
        try:
            decoded = json.loads(document)
        except ValueError as error:
            raise PaletteError(f"palette is not valid JSON: {error}") from error
        if not isinstance(decoded, dict):
            raise PaletteError("palette document must be an object")

        variants: dict[str, Palette] = {}
        for mode in ("dark", "light"):
            raw = decoded.get(mode)
            if not isinstance(raw, dict):
                raise PaletteError(f"palette document is missing a {mode!r} object")
            variants[mode] = Palette.from_mapping(mode, raw)
        return cls(dark=variants["dark"], light=variants["light"])

    @classmethod
    def load(cls, path: Path) -> Self:
        try:
            size = path.stat().st_size
        except OSError as error:
            raise PaletteError(f"cannot stat palette {path}: {error}") from error
        if size > MAX_PALETTE_BYTES:
            raise PaletteError(
                f"palette {path} is {size} bytes, over the {MAX_PALETTE_BYTES} limit"
            )
        try:
            return cls.from_json(path.read_bytes())
        except OSError as error:
            raise PaletteError(f"cannot read palette {path}: {error}") from error
