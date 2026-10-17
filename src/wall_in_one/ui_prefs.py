"""The new UI's window preferences, in a versioned ``ui.toml``.

Preferences that only the window cares about live here, not in
``settings.toml`` (see :mod:`wall_in_one.config`), in
``$XDG_CONFIG_HOME/wall-in-one/ui.toml``. The classic UI only reads it, and
writes it for one reason: the user dismissing the one-time Tidy up card.
Opening or idling never creates it, and nothing in it may ever affect the
wallpaper service.

The contract every build keeps:

* ``version`` 1 or 2 (:data:`FORMATS`). A file with a newer version is read for
  the keys this build knows and never written; saving raises
  :class:`UiPrefsReadOnlyError`. A version this build cannot interpret at all
  is treated the same way. A file without a version is taken as version 1.
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
* Version 2 adds ``interface``, the window the app builds when it starts
  without ``--ui``, and ``gpu_acceleration``, whether that window may render
  on the GPU (``false`` makes the next start use GTK's software renderer).
  ``gpu_acceleration`` joined version 2 before any release shipped it, so no
  released build knows a version 2 without it. Every save writes version 2,
  chosen or not (see :mod:`wall_in_one.library.state_file`); a version 1
  file is read as it is and written as version 2 by the next save.
* ``interface`` is absent until the user chooses one, and absent means the
  default (`DEFAULT_INTERFACE`): the new interface since 0.3.0, classic in
  0.2.2. So 0.3.0 writes either choice, ``"classic"`` included, while 0.2.2
  wrote only ``"next"`` and left the key out with classic chosen. In a file
  0.2.2 saved, absence can't tell "never chose" from "chose New, then went
  back to Classic", and the version can't either (turning GPU acceleration
  off also makes it 2), so neither is inferred: such a profile starts the
  new interface once.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

from wall_in_one import file_io, paths
from wall_in_one.library import state_file

#: Version 1 is what 0.2.0 and 0.2.1 wrote and is still read; version 2 adds
#: ``interface`` and ``gpu_acceleration`` (0.2.2). Every save is version 2.
FORMATS: Final = state_file.FormatVersions(oldest=1, current=2)
VERSION: Final = FORMATS.current
LOGGER: Final = logging.getLogger(__name__)
MAX_BYTES: Final = 64 * 1024
FILE_MODE: Final = 0o644

WINDOW_STYLES: Final[tuple[str, ...]] = ("solid", "translucent", "frosted")
#: The styles that see through the window and so have opacity dials.
GLASS_STYLES: Final[tuple[str, ...]] = ("translucent", "frosted")
THUMBNAIL_SIZES: Final[tuple[str, ...]] = ("small", "large")
#: An interface a start without ``--ui`` can build (``ui.window_services.UiKind``).
Interface = Literal["classic", "next"]
#: The windows a start without ``--ui`` can build (``ui.window_services.UI_KINDS``).
INTERFACES: Final[tuple[Interface, ...]] = ("classic", "next")
#: What a start builds when no interface was chosen (``ui.window_services.DEFAULT_UI``):
#: the new interface since 0.3.0. 0.2.2 started classic.
DEFAULT_INTERFACE: Final[Interface] = "next"
MAX_PAGE_CHARS: Final = 256

_GLASS_TABLES: Final[tuple[str, ...]] = ("background_opacity", "panel_opacity")
_SCALAR_KEYS: Final[tuple[str, ...]] = (
    "version",
    "window_style",
    "frost",
    "thumbnail_size",
    "last_page",
    "tidy_offer_dismissed",
    "interface",
    "gpu_acceleration",
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

    def __init__(self, *args: object) -> None:
        # For a person: no descriptor spelling of a path (`file_io.public_text`).
        super().__init__(
            *(
                file_io.public_text(argument) if isinstance(argument, str) else argument
                for argument in args
            )
        )


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


def _interface(value: object) -> Interface | None:
    """The interface ``value`` names, or None: nothing (valid) was chosen."""
    for choice in INTERFACES:
        if isinstance(value, str) and value == choice:
            return choice
    return None


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
    #: The window the next start builds when no ``--ui`` is given (version 2),
    #: once chosen. None until then: the start builds `DEFAULT_INTERFACE`.
    interface: Interface | None = None
    #: Whether the next start's window may render on the GPU (version 2). Off
    #: starts it with GTK's software renderer.
    gpu_acceleration: bool = True

    @property
    def interface_to_start(self) -> Interface:
        """The window a start without ``--ui`` builds: the one chosen, else the default."""
        return _interface(self.interface) or DEFAULT_INTERFACE

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
                "interface": self.interface,
                "gpu_acceleration": self.gpu_acceleration,
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
            interface=_interface(raw.get("interface")),
            gpu_acceleration=_flag(raw.get("gpu_acceleration"), defaults.gpu_acceleration),
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
    if FORMATS.declared(raw) is not None:
        return ""
    version = raw.get("version")
    if type(version) is int and version > FORMATS.current:
        return (
            f"ui.toml is from a newer version of Wall-in-One (version {version}); this "
            "version uses the preferences it understands but won't change the file"
        )
    return (
        f"ui.toml has a version this version of Wall-in-One doesn't recognize "
        f"({_clipped(version)}), so it won't change the file"
    )


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


def reads_cleanly(path: Path | None = None) -> bool:
    """Whether ui.toml is there and reads without a problem, in a version this build knows."""
    document, existing = _read(path if path is not None else paths.ui_prefs_path())
    return existing is not None and document.writable


#: The copy 0.2.2 kept of a version 1 ui.toml before its first version 2
#: save, beside it. Nothing reads it.
RETIRED_BACKUP_NAME: Final = "ui.toml.v1-backup"


def _declared_version(contents: bytes) -> int | None:
    """The version a ui.toml document declares, or None when it is not one this build reads."""
    try:
        raw = tomllib.loads(contents.decode("utf-8"))
    except UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError:
        return None
    return FORMATS.declared(raw)


def remove_retired_backup() -> bool:
    """Remove `RETIRED_BACKUP_NAME`, the copy kept for going back; whether it went.

    Only that one name, beside ui.toml in the app's config folder, and only
    under the checks of `wall_in_one.library.retired_backups`: a regular
    version 1 ui.toml of this user's, while ui.toml itself reads without a
    problem. Never raises. For a start that opens a window: no other start
    reads ui.toml.
    """
    from wall_in_one.library import retired_backups

    candidate = retired_backups.Candidate(
        RETIRED_BACKUP_NAME,
        paths.ui_prefs_path,
        1,
        "UI preferences",
        MAX_BYTES,
        _declared_version,
        reads_cleanly,
    )
    return bool(retired_backups.remove((candidate,)))


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
    """The document for ``prefs`` at the current version, carrying every unknown key in ``raw``."""
    prefs = prefs.validated()
    carried = dict(raw or {})
    scalars: dict[str, object] = {
        "version": FORMATS.current,
        "window_style": prefs.window_style,
        "frost": prefs.frost,
        "thumbnail_size": prefs.thumbnail_size,
        "last_page": prefs.last_page,
    }
    if prefs.tidy_offer_dismissed:
        # Written only once true, like settings.toml's battery key: a window
        # preference save before any dismissal keeps its familiar shape.
        scalars["tidy_offer_dismissed"] = True
    if prefs.interface is not None:
        # Written once chosen, classic too: absent means the default, which
        # 0.3.0 made the new interface.
        scalars["interface"] = prefs.interface
    if not prefs.gpu_acceleration:
        # Omitted while on (the default).
        scalars["gpu_acceleration"] = False
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

    A save writes the current version, whatever version the file was in.
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
                # Published but not confirmed durable is saved: say so, never
                # "not saved" (`state_file.published_counts`).
                with state_file.published_counts():
                    state_file.write_atomic_text(target, text, mode=FILE_MODE)
            return UiPrefsDocument(prefs, tomllib.loads(text))
    except UiPrefsError:
        raise
    except (OSError, TimeoutError, UnicodeError) as error:
        raise UiPrefsError(f"cannot save {target}: {error}") from error


def launch_interface(path: Path | None = None) -> Interface:
    """The window a start without ``--ui`` builds: the one chosen in Settings, else the default.

    Read-only: it never writes and never raises. A missing file, or one that
    chose nothing, means `DEFAULT_INTERFACE`, the new interface. So do an
    unreadable or malformed file and an unknown value; those two are logged
    in one line. A file from a newer version is read for this key like every
    key this build knows.
    """
    document = load(path)
    if document.read_only and not document.raw:
        LOGGER.info("%s; starting the new interface, the default", document.read_only)
        return DEFAULT_INTERFACE
    saved = document.raw.get("interface", DEFAULT_INTERFACE)
    chosen = _interface(saved)
    if chosen is None:
        LOGGER.info(
            "ui.toml names an interface this version doesn't know (%s); "
            "starting the new interface, the default",
            _clipped(saved),
        )
        return DEFAULT_INTERFACE
    return chosen


def launch_gpu_acceleration(path: Path | None = None) -> bool:
    """Whether a start's window may render on the GPU: False only when turned off.

    Read-only: it never writes and never raises. Unlike `launch_interface`, a
    file this build must not write -- missing is fine, but unreadable,
    malformed or from a newer version -- means on: the safe, unchanged default.
    """
    document = load(path)
    return document.prefs.gpu_acceleration if document.writable else True


def save(prefs: UiPrefs, path: Path | None = None) -> UiPrefsDocument:
    """Save every field of ``prefs``, keeping the file's unknown keys."""
    return mutate(lambda _current: prefs, path)


def update(changes: Mapping[str, Any], path: Path | None = None) -> UiPrefsDocument:
    """Save only the named fields, rebased on the latest file."""
    unknown = sorted(set(changes) - set(UiPrefs.__dataclass_fields__))
    if unknown:
        raise UiPrefsError(f"unknown UI preference(s): {', '.join(unknown)}")
    return mutate(lambda current: replace(current, **changes), path)
