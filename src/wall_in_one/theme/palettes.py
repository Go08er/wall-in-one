"""Noctalia's palette files: what is on disk, and how to add to it.

Three sources, and they are not symmetrical:

* **built-in** palettes are compiled into the Noctalia binary. Confirmed by
  reading it: the ten names are in there as plain strings, their colours are
  not. So they can be listed and selected but never previewed from disk.
* **community** palettes are cached one file per palette, named after the
  palette with the name percent-encoded (`Osaka%20jade.json`), next to a
  `.catalog` directory the daemon owns. The id Noctalia wants back -- the one
  that appears as `[theme] community_palette` -- is the decoded name.
* **custom** palettes are plain `<name>.json` files the user owns, and the only
  ones this module will ever write.

The file format is *not* the 72-token document `noctalia theme` emits. It is
Noctalia's `mPrimary` shape with a nested `terminal` block, so parsing maps it
onto our canonical token names. Fourteen core tokens and all twenty-two
terminal ones survive that map exactly. The remaining core tokens are tonal
derivations Noctalia computes at apply time; they are left missing rather than
guessed, because a wrong `surface_container` looks like a bug and `Palette.get`
already degrades through them. The true 72 arrive by the ordinary route the
moment the palette is actually applied -- Noctalia renders our template.

Bounded like `library.scan`: a ceiling on entries and on file size, and
anything unreadable or malformed is reported in `Discovery.skipped` rather than
raised. A directory that does not exist is simply an empty source.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final
from urllib.parse import unquote

from wall_in_one import paths
from wall_in_one.theme.palette import (
    MAX_PALETTE_BYTES,
    Colour,
    Mode,
    Palette,
    PaletteError,
    PalettePair,
)


class PaletteWriteError(PaletteError):
    """A palette could not be written, or must never be."""


class Origin(Enum):
    """Where a palette comes from.

    The values are exactly the source names `noctalia msg color-scheme-set`
    takes, so applying an entry is `color-scheme-set <origin.value> <name>`.
    """

    BUILTIN = "builtin"
    COMMUNITY = "community"
    CUSTOM = "custom"

    @property
    def is_writable(self) -> bool:
        """Only the user's own directory is ours to write into."""
        return self is Origin.CUSTOM

    @property
    def label(self) -> str:
        return {"builtin": "Built-in", "community": "Community", "custom": "Custom"}[self.value]


#: The palettes compiled into Noctalia, from `src/theme/builtin_palettes.cpp`
#: and confirmed against the 5.0.0-beta.7 binary. Names only: the colours are
#: not exposed by any CLI surface, so these entries carry no `colours`.
BUILTIN_NAMES: Final[tuple[str, ...]] = (
    "Ayu",
    "Catppuccin",
    "Dracula",
    "Eldritch",
    "Gruvbox",
    "Kanagawa",
    "Noctalia",
    "Nord",
    "Rosé Pine",
    "Tokyo-Night",
)

#: Ceiling on one discovery pass. A palette directory is small by nature; this
#: only stops a directory that has become something else from stalling the UI.
MAX_ENTRIES: Final = 512

#: The core keys a palette file carries, mapped onto the canonical tokens each
#: one drives. Derived by diffing a real community palette against what
#: `noctalia theme --theme-json <file> --both` expands it into, so these are
#: measured equalities rather than guesses at intent.
_CORE_MAPPING: Final[Mapping[str, tuple[str, ...]]] = {
    "mPrimary": ("source_color", "primary", "surface_tint"),
    "mOnPrimary": ("on_primary",),
    "mSecondary": ("secondary",),
    "mOnSecondary": ("on_secondary",),
    "mTertiary": ("tertiary",),
    "mOnTertiary": ("on_tertiary",),
    "mError": ("error",),
    "mOnError": ("on_error",),
    "mSurface": ("surface", "background"),
    "mOnSurface": ("on_surface", "on_background"),
    "mSurfaceVariant": ("surface_variant", "surface_container"),
    "mOnSurfaceVariant": ("on_surface_variant",),
    "mOutline": ("outline",),
    "mShadow": ("shadow", "scrim"),
}

#: The token each core key is written back *from*. Not the inverse of
#: `_CORE_MAPPING`: `mPrimary` reads as three tokens but is written from
#: `primary`, since `source_color` is the seed the scheme was built from rather
#: than a surface colour.
_CORE_REVERSE: Final[Mapping[str, str]] = {
    "mPrimary": "primary",
    "mOnPrimary": "on_primary",
    "mSecondary": "secondary",
    "mOnSecondary": "on_secondary",
    "mTertiary": "tertiary",
    "mOnTertiary": "on_tertiary",
    "mError": "error",
    "mOnError": "on_error",
    "mSurface": "surface",
    "mOnSurface": "on_surface",
    "mSurfaceVariant": "surface_variant",
    "mOnSurfaceVariant": "on_surface_variant",
    "mOutline": "outline",
    "mShadow": "shadow",
}

#: `mHover` and `mOnHover` appear in older files and have no counterpart in the
#: 72-token set, so they are carried through a rewrite untouched but never
#: mapped onto a token.
_TERMINAL_SCALARS: Final[Mapping[str, str]] = {
    "foreground": "terminal_foreground",
    "background": "terminal_background",
    "cursor": "terminal_cursor",
    "cursorText": "terminal_cursor_text",
    "selectionFg": "terminal_selection_fg",
    "selectionBg": "terminal_selection_bg",
}

_TERMINAL_GROUPS: Final[tuple[str, ...]] = ("normal", "bright")

#: What the editor offers, in the order it shows them. The whole core set of
#: the file format -- fourteen well-understood keys beats a partial pass at
#: seventy-two derived ones.
EDITABLE_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("mPrimary", "Primary"),
    ("mOnPrimary", "On primary"),
    ("mSecondary", "Secondary"),
    ("mOnSecondary", "On secondary"),
    ("mTertiary", "Tertiary"),
    ("mOnTertiary", "On tertiary"),
    ("mError", "Error"),
    ("mOnError", "On error"),
    ("mSurface", "Surface"),
    ("mOnSurface", "On surface"),
    ("mSurfaceVariant", "Surface variant"),
    ("mOnSurfaceVariant", "On surface variant"),
    ("mOutline", "Outline"),
    ("mShadow", "Shadow"),
)

#: Names we will create files for. Deliberately narrower than what a filesystem
#: accepts: no separators, no leading dot, nothing that needs escaping in a
#: file name, and short enough to stay well inside NAME_MAX.
_MAX_NAME_LENGTH: Final = 64
_ALLOWED_EXTRA: Final = frozenset(" ._+-()")


def custom_directory() -> Path:
    return paths.noctalia_custom_palettes_dir()


def community_directory() -> Path:
    return paths.noctalia_community_palettes_dir()


# -- parsing -------------------------------------------------------------


def _colour_of(raw: object, where: str) -> Colour:
    if not isinstance(raw, str):
        raise PaletteError(f"{where} is not a colour string")
    return Colour.parse(raw)


def _terminal_colours(raw: object, where: str) -> Iterator[tuple[str, Colour]]:
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise PaletteError(f"{where}.terminal is not an object")
    for key, token in _TERMINAL_SCALARS.items():
        if key in raw:
            yield token, _colour_of(raw[key], f"{where}.terminal.{key}")
    for group in _TERMINAL_GROUPS:
        entries = raw.get(group)
        if entries is None:
            continue
        if not isinstance(entries, dict):
            raise PaletteError(f"{where}.terminal.{group} is not an object")
        for name, value in entries.items():
            yield f"terminal_{group}_{name}", _colour_of(value, f"{where}.terminal.{group}.{name}")


def variant_from_mapping(mode: Mode, raw: Mapping[str, object]) -> Palette:
    """Map one `dark` or `light` object onto canonical tokens."""
    colours: dict[str, Colour] = {}
    for key, tokens in _CORE_MAPPING.items():
        value = raw.get(key)
        if value is None:
            continue
        colour = _colour_of(value, f"{mode}.{key}")
        for token in tokens:
            colours[token] = colour
    colours.update(_terminal_colours(raw.get("terminal"), mode))
    if not colours:
        raise PaletteError(f"{mode} variant defines no recognised colours")
    return Palette(mode=mode, colours=colours)


def pair_from_mapping(decoded: Mapping[str, object]) -> PalettePair:
    """Build both variants from an already-decoded palette document."""
    variants: dict[str, Palette] = {}
    for mode in ("dark", "light"):
        raw = decoded.get(mode)
        if not isinstance(raw, dict):
            raise PaletteError(f"palette document is missing a {mode!r} object")
        variants[mode] = variant_from_mapping(mode, raw)
    return PalettePair(dark=variants["dark"], light=variants["light"])


def parse_document(document: str | bytes) -> PalettePair:
    """Parse a palette file into the dark and light variants it describes."""
    try:
        decoded: object = json.loads(document)
    except ValueError as error:
        raise PaletteError(f"palette is not valid JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise PaletteError("palette document must be an object")
    return pair_from_mapping(decoded)


def read_document(path: Path) -> dict[str, object]:
    """Read a palette file as raw JSON, size-checked.

    Kept alongside the parsed form because an edit has to preserve the keys we
    do not model -- the terminal block, and `mHover` on older files.
    """
    try:
        size = path.stat().st_size
    except OSError as error:
        raise PaletteError(f"cannot stat palette {path}: {error}") from error
    if size > MAX_PALETTE_BYTES:
        raise PaletteError(f"palette {path} is {size} bytes, over the {MAX_PALETTE_BYTES} limit")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PaletteError(f"cannot read palette {path}: {error}") from error
    try:
        decoded: object = json.loads(raw)
    except ValueError as error:
        raise PaletteError(f"palette {path} is not valid JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise PaletteError(f"palette {path} is not an object")
    return decoded


# -- entries -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaletteEntry:
    """One selectable palette.

    `colours` is `None` only for built-ins, whose colours Noctalia keeps to
    itself. Everything else on an entry is present for all three origins, so
    the UI can list and apply a built-in without special-casing it.
    """

    name: str
    origin: Origin
    path: Path | None
    colours: PalettePair | None

    @property
    def is_editable(self) -> bool:
        """Whether this app may rewrite the file behind this entry."""
        return self.origin.is_writable and self.path is not None

    def for_mode(self, mode: Mode) -> Palette | None:
        return self.colours.for_mode(mode) if self.colours is not None else None

    def describe(self) -> str:
        if self.colours is None:
            return "compiled into Noctalia -- colours appear once it is applied"
        return f"{len(self.colours.dark.colours)} tokens"


@dataclass(frozen=True, slots=True)
class Discovery:
    """Everything found, and everything deliberately not found."""

    entries: tuple[PaletteEntry, ...]
    skipped: tuple[str, ...] = ()

    def of_origin(self, origin: Origin) -> tuple[PaletteEntry, ...]:
        return tuple(entry for entry in self.entries if entry.origin is origin)

    def find(self, origin: Origin, name: str) -> PaletteEntry | None:
        for entry in self.entries:
            if entry.origin is origin and entry.name == name:
                return entry
        return None


def load_entry(path: Path, origin: Origin) -> PaletteEntry:
    """Load one palette file. Raises `PaletteError` if it is not usable."""
    return PaletteEntry(
        name=entry_name(path, origin),
        origin=origin,
        path=path,
        colours=pair_from_mapping(read_document(path)),
    )


def entry_name(path: Path, origin: Origin) -> str:
    """The name Noctalia knows this palette by.

    Community files are cached under a percent-encoded name; the id that goes
    back over IPC, and that `[theme] community_palette` holds, is the decoded
    one. Custom files are named by us, and we only allow names that need no
    encoding, so the stem is already the name.
    """
    if origin is Origin.COMMUNITY:
        return unquote(path.stem)
    return path.stem


def _candidates(directory: Path, skipped: list[str]) -> list[Path]:
    """Palette files directly inside ``directory``, in a stable order.

    `os.scandir` rather than `Path.glob` because glob swallows the permission
    error on an unreadable directory, and "your palettes are missing" with no
    explanation is the worst version of that.
    """
    try:
        with os.scandir(directory) as entries:
            found = [
                Path(entry.path)
                for entry in entries
                if not entry.name.startswith(".")
                and entry.name.endswith(".json")
                and entry.is_file()
            ]
    except FileNotFoundError:
        # A source nobody has used yet. Empty, not broken.
        return []
    except OSError as error:
        skipped.append(f"{directory}: {error.strerror or error}")
        return []
    return sorted(found)


def _scan_directory(directory: Path, origin: Origin, skipped: list[str]) -> list[PaletteEntry]:
    entries: list[PaletteEntry] = []
    for candidate in _candidates(directory, skipped):
        if len(entries) >= MAX_ENTRIES:
            skipped.append(f"{directory}: stopped at {MAX_ENTRIES} palettes")
            break
        try:
            entries.append(load_entry(candidate, origin))
        except PaletteError as error:
            skipped.append(f"{candidate.name}: {error}")
    return entries


def discover(
    *,
    custom: Path | None = None,
    community: Path | None = None,
    builtins: Sequence[str] | None = None,
) -> Discovery:
    """Every palette this machine can offer. Never raises."""
    skipped: list[str] = []
    entries: list[PaletteEntry] = [
        PaletteEntry(name=name, origin=Origin.BUILTIN, path=None, colours=None)
        for name in (BUILTIN_NAMES if builtins is None else builtins)
    ]
    entries += _scan_directory(
        community if community is not None else community_directory(),
        Origin.COMMUNITY,
        skipped,
    )
    entries += _scan_directory(
        custom if custom is not None else custom_directory(),
        Origin.CUSTOM,
        skipped,
    )
    order = {Origin.BUILTIN: 0, Origin.COMMUNITY: 1, Origin.CUSTOM: 2}
    entries.sort(key=lambda entry: (order[entry.origin], entry.name.casefold()))
    return Discovery(entries=tuple(entries), skipped=tuple(skipped))


# -- writing -------------------------------------------------------------


def validate_name(name: str) -> str:
    """Check a proposed custom palette name, returning it stripped.

    Restrictive on purpose. This name becomes a file name in a directory
    another program reads, so anything that could escape the directory or
    change which file is meant is refused rather than sanitised -- silently
    rewriting a name the user typed is how you end up overwriting a palette
    they did not mean.
    """
    candidate = name.strip()
    if not candidate:
        raise PaletteWriteError("a palette needs a name")
    if len(candidate) > _MAX_NAME_LENGTH:
        raise PaletteWriteError(f"name is longer than {_MAX_NAME_LENGTH} characters")
    if candidate.startswith("."):
        raise PaletteWriteError("a palette name cannot start with a dot")
    for character in candidate:
        if character.isalnum() or character in _ALLOWED_EXTRA:
            continue
        raise PaletteWriteError(f"a palette name cannot contain {character!r}")
    return candidate


def custom_path(name: str, directory: Path | None = None) -> Path:
    """Where a custom palette called ``name`` belongs.

    Validated *and* checked after the fact: `validate_name` already rules out
    anything with a separator in it, and the containment check then proves the
    result rather than trusting the rule.
    """
    target_directory = directory if directory is not None else custom_directory()
    target = target_directory / f"{validate_name(name)}.json"
    if target.parent != target_directory:
        raise PaletteWriteError(f"{name!r} does not name a file in {target_directory}")
    return target


def target_for(entry: PaletteEntry) -> Path:
    """The file an edit to ``entry`` may be written to.

    The enforcement point for read-only sources: built-in and community
    palettes have no writable target, and saying so here means every write path
    goes through the same check instead of each caller remembering.
    """
    if not entry.origin.is_writable:
        raise PaletteWriteError(f"{entry.origin.label.lower()} palettes are read-only")
    if entry.path is None:
        raise PaletteWriteError(f"{entry.name} has no file behind it")
    return entry.path


def with_overrides(
    document: Mapping[str, object],
    overrides: Mapping[Mode, Mapping[str, str]],
) -> dict[str, object]:
    """A copy of ``document`` with some core keys replaced.

    Copied rather than mutated, and only the named keys are touched, so the
    terminal block and anything else Noctalia writes survives an edit
    untouched.
    """
    result: dict[str, object] = dict(document)
    for mode, changes in overrides.items():
        existing = result.get(mode)
        variant: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
        for key, value in changes.items():
            if key not in _CORE_MAPPING:
                raise PaletteWriteError(f"{key} is not an editable palette key")
            # Parse to reject anything that is not a colour before it reaches
            # the file; Noctalia validates on load and would drop the palette.
            variant[key] = Colour.parse(value).hex
        result[mode] = variant
    return result


def variant_document(palette: Palette) -> dict[str, object]:
    """The file-format object for one variant of a generated palette.

    The way back out of the canonical token set, so a scheme preview can be
    kept as a named palette instead of only ever being looked at.
    """
    variant: dict[str, object] = {}
    for key, token in _CORE_REVERSE.items():
        colour = palette.colours.get(token)
        if colour is not None:
            variant[key] = colour.hex
    terminal: dict[str, object] = {}
    for key, token in _TERMINAL_SCALARS.items():
        colour = palette.colours.get(token)
        if colour is not None:
            terminal[key] = colour.hex
    for group in _TERMINAL_GROUPS:
        shades = {
            name.removeprefix(f"terminal_{group}_"): colour.hex
            for name, colour in palette.colours.items()
            if name.startswith(f"terminal_{group}_")
        }
        if shades:
            terminal[group] = shades
    if terminal:
        variant["terminal"] = terminal
    return variant


def document_from_pair(pair: PalettePair) -> dict[str, object]:
    """Turn a generated dark/light pair back into a palette file document."""
    return {"dark": variant_document(pair.dark), "light": variant_document(pair.light)}


def _write_atomic(target: Path, serialised: str) -> None:
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise PaletteWriteError(f"cannot create {target.parent}: {error}") from error

    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(serialised)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise PaletteWriteError(f"cannot write {target}: {error}") from error


def _serialise(document: Mapping[str, object]) -> tuple[str, PalettePair]:
    """Render a document, and prove it parses before anyone writes it.

    A file Noctalia would reject is worse than no file, because the failure
    surfaces somewhere else entirely.
    """
    serialised = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    return serialised, parse_document(serialised)


def write_custom(
    name: str,
    document: Mapping[str, object],
    directory: Path | None = None,
) -> PaletteEntry:
    """Write a custom palette, atomically, and return the entry for it."""
    target = custom_path(name, directory if directory is not None else custom_directory())
    serialised, colours = _serialise(document)
    _write_atomic(target, serialised)
    return PaletteEntry(
        name=validate_name(name), origin=Origin.CUSTOM, path=target, colours=colours
    )


def save_edits(entry: PaletteEntry, document: Mapping[str, object]) -> PaletteEntry:
    """Rewrite the file behind ``entry`` in place.

    Goes through `target_for`, so a built-in or community entry raises here
    rather than reaching the filesystem.
    """
    target = target_for(entry)
    serialised, colours = _serialise(document)
    _write_atomic(target, serialised)
    return PaletteEntry(name=entry.name, origin=entry.origin, path=target, colours=colours)


def duplicate(
    entry: PaletteEntry,
    name: str,
    *,
    overrides: Mapping[Mode, Mapping[str, str]] | None = None,
    directory: Path | None = None,
) -> PaletteEntry:
    """Copy ``entry`` into the custom directory under a new name.

    The only way to get an editable copy of a read-only palette, and the reason
    read-only enforcement does not have to mean read-only in practice.
    """
    if entry.path is None:
        raise PaletteWriteError(f"{entry.name} has no file to copy")
    document = read_document(entry.path)
    if overrides:
        document = with_overrides(document, overrides)
    return write_custom(name, document, directory)
