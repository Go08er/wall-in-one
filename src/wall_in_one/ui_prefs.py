"""The new UI's window preferences, in a versioned ``ui.toml``.

``settings.toml`` is frozen (see :mod:`wall_in_one.config`): it has no version,
so a new key there would lock older builds out of settings edits. Preferences
that only the window cares about live here instead, in
``$XDG_CONFIG_HOME/wall-in-one/ui.toml``. The classic UI only reads it, and
writes it for one reason: the user dismissing the one-time Tidy up card.
Opening or idling never creates it, and nothing in it may ever affect the
wallpaper service.

The contract every build keeps:

* ``version = 1``. A file with a newer version is read for the keys this build
  knows and never written; saving raises :class:`UiPrefsReadOnlyError`. A
  version this build cannot interpret at all is treated the same way. A file
  without a version is taken as version 1.
* Unknown keys, at the top level or inside the per-style opacity tables, are
  carried through every save unchanged.
* An invalid value falls back to the default for that field only. The next
  save writes that default.
* A missing file means defaults. An unreadable or malformed file also means
  defaults, but it is left alone: saving refuses until somebody fixes or
  removes it, so its bytes are never lost.
* The file is TOML 1.0, written atomically with mode 0644. Later versions must
  keep writing TOML 1.0 so an older build can still parse the file and see
  that its version is newer.
* Any new field, or new value for an existing field, bumps ``version``.
  ``tidy_offer_dismissed`` joined version 1 before any release shipped
  ui.toml (0.2.0 is the first), so no build knows a version 1 without it.
"""

from __future__ import annotations

import datetime
import math
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from wall_in_one import file_io, paths
from wall_in_one.library import state_file

VERSION: Final = 1
MAX_BYTES: Final = 64 * 1024
FILE_MODE: Final = 0o644

WINDOW_STYLES: Final[tuple[str, ...]] = ("solid", "translucent", "frosted")
#: The styles that see through the window and so have opacity dials.
GLASS_STYLES: Final[tuple[str, ...]] = ("translucent", "frosted")
THUMBNAIL_SIZES: Final[tuple[str, ...]] = ("small", "large")
MAX_PAGE_CHARS: Final = 256

_GLASS_TABLES: Final[tuple[str, ...]] = ("background_opacity", "panel_opacity")
_SCALAR_KEYS: Final[tuple[str, ...]] = (
    "version",
    "window_style",
    "frost",
    "thumbnail_size",
    "last_page",
    "tidy_offer_dismissed",
)
KNOWN_KEYS: Final = frozenset((*_SCALAR_KEYS, *_GLASS_TABLES))
_BARE_KEY: Final = re.compile(r"[A-Za-z0-9_-]+")
_SHORT_ESCAPES: Final = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class UiPrefsError(Exception):
    """ui.toml could not be saved."""


class UiPrefsReadOnlyError(UiPrefsError):
    """This build must not write ui.toml; the message says why."""


@dataclass(frozen=True, slots=True)
class GlassOpacity:
    """One opacity dial, remembered separately for each glass style."""

    translucent: float
    frosted: float


DEFAULT_BACKGROUND_OPACITY: Final = GlassOpacity(translucent=0.55, frosted=0.30)
DEFAULT_PANEL_OPACITY: Final = GlassOpacity(translucent=0.80, frosted=0.60)


def _unit(value: object, default: float) -> float:
    """A finite number from 0 to 1, or ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        # Checked before float(): TOML integers can exceed a float's range.
        return float(value) if 0 <= value <= 1 else default
    if isinstance(value, float) and math.isfinite(value) and 0.0 <= value <= 1.0:
        return value
    return default


def _flag(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _choice(value: object, choices: tuple[str, ...], default: str) -> str:
    return value if isinstance(value, str) and value in choices else default


def _page(value: object, default: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_PAGE_CHARS
        or not value.isprintable()
    ):
        return default
    return value


def _glass(value: object, default: GlassOpacity) -> GlassOpacity:
    table = value if isinstance(value, Mapping) else {}
    return GlassOpacity(
        translucent=_unit(table.get("translucent", default.translucent), default.translucent),
        frosted=_unit(table.get("frosted", default.frosted), default.frosted),
    )


@dataclass(frozen=True, slots=True)
class UiPrefs:
    """Typed window preferences. Every field has a usable default."""

    window_style: str = "solid"
    #: The page background behind lists and grids, per glass style.
    background_opacity: GlassOpacity = DEFAULT_BACKGROUND_OPACITY
    #: Panels and elements: sidebar, header, player bar, inspector, cards.
    panel_opacity: GlassOpacity = DEFAULT_PANEL_OPACITY
    #: In-app blur strength for the frosted style, 0 (clear) to 1.
    frost: float = 0.5
    thumbnail_size: str = "large"
    #: The page to reopen, as the new UI names it.
    last_page: str = "library"
    #: The user dismissed the one-time Tidy up card shown after an update.
    tidy_offer_dismissed: bool = False

    def validated(self) -> UiPrefs:
        """Replace each invalid field with its own default."""
        return UiPrefs.from_mapping(
            {
                "window_style": self.window_style,
                "background_opacity": {
                    "translucent": self.background_opacity.translucent,
                    "frosted": self.background_opacity.frosted,
                },
                "panel_opacity": {
                    "translucent": self.panel_opacity.translucent,
                    "frosted": self.panel_opacity.frosted,
                },
                "frost": self.frost,
                "thumbnail_size": self.thumbnail_size,
                "last_page": self.last_page,
                "tidy_offer_dismissed": self.tidy_offer_dismissed,
            }
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> UiPrefs:
        defaults = cls()
        return cls(
            window_style=_choice(raw.get("window_style"), WINDOW_STYLES, defaults.window_style),
            background_opacity=_glass(raw.get("background_opacity"), defaults.background_opacity),
            panel_opacity=_glass(raw.get("panel_opacity"), defaults.panel_opacity),
            frost=_unit(raw.get("frost", defaults.frost), defaults.frost),
            thumbnail_size=_choice(
                raw.get("thumbnail_size"), THUMBNAIL_SIZES, defaults.thumbnail_size
            ),
            last_page=_page(raw.get("last_page"), defaults.last_page),
            tidy_offer_dismissed=_flag(
                raw.get("tidy_offer_dismissed"), defaults.tidy_offer_dismissed
            ),
        )


@dataclass(frozen=True, slots=True)
class UiPrefsDocument:
    """What was read from ui.toml and whether this build may write it."""

    prefs: UiPrefs
    #: Everything parsed from the file, unknown keys included. Empty when the
    #: file is missing or could not be parsed. Saving starts from this.
    raw: Mapping[str, Any] = field(default_factory=dict)
    #: Non-empty when this build must not write the file, saying why.
    read_only: str = ""

    @property
    def writable(self) -> bool:
        return not self.read_only

    @property
    def unknown_keys(self) -> tuple[str, ...]:
        """Keys this build carries without understanding, dotted when nested."""
        found = [key for key in self.raw if key not in KNOWN_KEYS]
        for table in _GLASS_TABLES:
            nested = self.raw.get(table)
            if isinstance(nested, Mapping):
                found.extend(f"{table}.{key}" for key in nested if key not in GLASS_STYLES)
        return tuple(found)


def _clipped(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:39] + "…"


def _version_problem(raw: Mapping[str, Any]) -> str:
    version = raw.get("version", VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return (
            f"ui.toml has a version this version of Wall-in-One doesn't recognize "
            f"({_clipped(version)}), so it won't change the file"
        )
    if version > VERSION:
        return (
            f"ui.toml is from a newer version of Wall-in-One (version {version}); this "
            "version uses the preferences it understands but won't change the file"
        )
    return ""


def _read(target: Path) -> tuple[UiPrefsDocument, bytes | None]:
    try:
        document = file_io.read_regular_bytes(target, MAX_BYTES)
    except OSError as error:
        return (
            UiPrefsDocument(
                UiPrefs(),
                read_only=(
                    f"ui.toml can't be read ({error}); defaults are in use and the "
                    "file won't be changed until it's fixed or removed"
                ),
            ),
            None,
        )
    if document is None:
        return UiPrefsDocument(UiPrefs()), None
    try:
        raw = tomllib.loads(document.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
        return (
            UiPrefsDocument(
                UiPrefs(),
                read_only=(
                    f"ui.toml isn't valid TOML ({error}); defaults are in use and the "
                    "file won't be changed until it's fixed or removed"
                ),
            ),
            document,
        )
    return UiPrefsDocument(UiPrefs.from_mapping(raw), raw, _version_problem(raw)), document


def load(path: Path | None = None) -> UiPrefsDocument:
    """Read ui.toml without ever failing; a problem is reported in the result."""
    return _read(path if path is not None else paths.ui_prefs_path())[0]


# -- writing TOML 1.0 --------------------------------------------------------


def _string(value: str) -> str:
    """A TOML basic string. Unlike JSON, TOML also requires DEL to be escaped."""
    escaped: list[str] = []
    for character in value:
        short = _SHORT_ESCAPES.get(character)
        if short is not None:
            escaped.append(short)
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            escaped.append(f"\\u{ord(character):04X}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def _key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _string(key)


def _value(value: object) -> str:
    """Any value tomllib produces, as TOML; tables become inline tables."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    raise UiPrefsError(f"ui.toml cannot hold a {type(value).__name__} value")


def render(prefs: UiPrefs, raw: Mapping[str, Any] | None = None) -> str:
    """The document for ``prefs``, carrying every unknown key in ``raw``."""
    prefs = prefs.validated()
    carried = dict(raw or {})
    scalars: dict[str, object] = {
        "version": VERSION,
        "window_style": prefs.window_style,
        "frost": prefs.frost,
        "thumbnail_size": prefs.thumbnail_size,
        "last_page": prefs.last_page,
    }
    if prefs.tidy_offer_dismissed:
        # Written only once true, like settings.toml's battery key: a window
        # preference save before any dismissal keeps its familiar shape.
        scalars["tidy_offer_dismissed"] = True
    tables: dict[str, Mapping[str, Any]] = {}
    for name, glass in (
        ("background_opacity", prefs.background_opacity),
        ("panel_opacity", prefs.panel_opacity),
    ):
        existing = carried.get(name)
        extra = (
            {key: value for key, value in existing.items() if key not in GLASS_STYLES}
            if isinstance(existing, Mapping)
            else {}
        )
        tables[name] = {"translucent": glass.translucent, "frosted": glass.frosted, **extra}
    for key, value in carried.items():
        if key in KNOWN_KEYS:
            continue
        if isinstance(value, Mapping):
            tables[key] = value
        else:
            scalars[key] = value

    lines = ["# Wall-in-One window preferences. See docs/settings.md.", ""]
    try:
        lines.extend(f"{_key(key)} = {_value(value)}" for key, value in scalars.items())
        for name, table in tables.items():
            lines.extend(("", f"[{_key(name)}]"))
            lines.extend(f"{_key(key)} = {_value(value)}" for key, value in table.items())
    except RecursionError as error:
        raise UiPrefsError("ui.toml is nested too deeply to write") from error
    return "\n".join(lines) + "\n"


# -- saving ------------------------------------------------------------------


def mutate(
    change: Callable[[UiPrefs], UiPrefs],
    path: Path | None = None,
) -> UiPrefsDocument:
    """Apply one change to the latest file under its lock, then save it.

    Re-reading under the lock keeps another window's edit to a different
    field, and the latest unknown keys, instead of a stale snapshot. A file
    that has not changed is not rewritten.
    """
    target = path if path is not None else paths.ui_prefs_path()
    try:
        with state_file.mutation_lock(target, description="UI preferences"):
            current, existing = _read(target)
            if current.read_only:
                raise UiPrefsReadOnlyError(current.read_only)
            prefs = change(current.prefs).validated()
            text = render(prefs, current.raw)
            encoded = text.encode("utf-8")
            if len(encoded) > MAX_BYTES:
                raise UiPrefsError(
                    f"ui.toml would exceed its {MAX_BYTES}-byte limit; nothing was saved"
                )
            if encoded != existing:
                paths.ensure_directory(target.parent)
                state_file.write_atomic_text(target, text, mode=FILE_MODE)
            return UiPrefsDocument(prefs, tomllib.loads(text))
    except UiPrefsError:
        raise
    except (OSError, TimeoutError, UnicodeError) as error:
        raise UiPrefsError(f"cannot save {target}: {error}") from error


def save(prefs: UiPrefs, path: Path | None = None) -> UiPrefsDocument:
    """Save every field of ``prefs``, keeping the file's unknown keys."""
    return mutate(lambda _current: prefs, path)


def update(changes: Mapping[str, Any], path: Path | None = None) -> UiPrefsDocument:
    """Save only the named fields, rebased on the latest file."""
    unknown = sorted(set(changes) - set(UiPrefs.__dataclass_fields__))
    if unknown:
        raise UiPrefsError(f"unknown UI preference(s): {', '.join(unknown)}")
    return mutate(lambda current: replace(current, **changes), path)
