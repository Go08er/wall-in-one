"""Wallpaper Engine content installed through Steam.

Surveyed against the 49 items on the development machine before any of this
was written, and the survey changed the design:

- **45 of 49 are `video`**, and their entry file is a real `.mp4` sitting in the
  Workshop directory. Those need nothing but mpvpaper, which this app already
  drives. They are ordinary video wallpapers that happen to live under Steam.
- **4 are `scene`**, and their `file` names a `scene.json` that *does not exist*
  -- the content is packed inside `scene.pkg`. Those genuinely require
  `linux-wallpaperengine`; the app uses it for both desktop playback and a
  full-resolution representative still without writing into Steam's tree.
- `type` is spelled both `video` and `Video`, so it is compared case-folded.
  Trusting the casing would have silently hidden eight wallpapers.
- The preview is a `.gif` for 46 of them. A gif is *not* a still -- this app
  plays gifs as video -- so a preview is a good source for a still rather than
  a still itself. It is also 900 KB against a 100 MB video, and it is the frame
  the author chose.

Later the owner added a `web` wallpaper and a preset, and both vanished: only
the two kinds above were kept. Every item is now listed. What the engine can
run, and how a preset becomes its base plus `--set-property` values, is in
docs/wallpaper-engine.md, read from linux-wallpaperengine's own source;
`playback` below is that decision.

Nothing here writes anything. Steam's directories belong to Steam: items are
`Ownership.USER` so `library.manage` refuses to delete them, and the entry
below is a read-only view of a directory this app does not own.
"""

from __future__ import annotations

import json
import math
import re
import stat
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import file_io
from wall_in_one.library.model import MediaItem, Unplayable, UnseenKind, is_absent

#: Where the scan is told about a place it could not look into: the folder,
#: why (`UnseenKind`), and the system's words. Optional everywhere below.
OnUnseen = Callable[[Path, UnseenKind, str], None]

#: Wallpaper Engine's Steam application id.
APP_ID: Final = "431960"

#: Where Steam keeps the file that lists every library folder.
_LIBRARY_INDEX: Final = Path("steamapps") / "libraryfolders.vdf"


def default_steam_roots() -> tuple[Path, ...]:
    """The usual roots, resolved against the caller's current home.

    This must stay lazy. Test and VM launchers deliberately replace ``HOME``
    after importing modules; capturing :func:`Path.home` at import time made a
    later sandboxed scan inspect the developer's real Steam installation.
    Flatpak's location is included because it lives in a separate tree.
    """
    home = Path.home()
    return (
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    )


#: Ceilings. A workshop directory is user-writable and can hold anything.
MAX_ITEMS: Final = 4096
MAX_PROJECT_BYTES: Final = 256 * 1024
MAX_INDEX_BYTES: Final = 1024 * 1024

#: `"path"  "/some/where"` out of the KeyValues index. Parsed by pattern
#: rather than properly: the file is Valve's own format, the only field wanted
#: is this one, and a real parser would be a dependency for one line.
_PATH_LINE: Final = re.compile(r'"path"\s*"([^"]+)"')

#: Directly usable as a still. Anything else -- a gif, above all -- is a source
#: for one instead.
_STILL_SUFFIXES: Final[frozenset[str]] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})

#: The item types linux-wallpaperengine runs: its project parser knows these
#: three and exits on any other (ProjectParser.cpp:49-63 in the version
#: nixpkgs builds; docs/wallpaper-engine.md). A playable `video` still goes to
#: mpvpaper; the rest of these go to the engine as `--bg <id>`.
SUPPORTED_TYPES: Final[frozenset[str]] = frozenset({"scene", "video", "web"})

#: Types that are never handed to any renderer, whatever `SUPPORTED_TYPES`
#: says now or later (owner decision, for security). An `application`
#: wallpaper is a program Wallpaper Engine executes: a Workshop item anyone
#: can publish that runs as the user is the easiest trojan there is. It is
#: listed and counted as unsupported, and `playback` checks this before the
#: supported set, so adding a type there can never re-enable one of these.
NEVER_RUN: Final[frozenset[str]] = frozenset({"application"})

#: What the entry file of each type the engine renders must look like. A
#: scene names its `scene.json` (inside `scene.pkg`), a web wallpaper its
#: page. Neither is ever executed (the engine parses one and opens the other
#: in a browser), so this is judged by name alone and no file is read or
#: run to decide: an item whose file is something else isn't what its type
#: says, and is not handed over at all. A `video` is checked by what plays
#: it (mpvpaper, or the engine's own decoder for a preset's base).
EXPECTED_FILE: Final[Mapping[str, tuple[str, ...]]] = {
    "scene": (".json",),
    "web": (".html", ".htm"),
}

#: What a preset is called where its type would be: it has none of its own.
PRESET: Final = "preset"

#: What an item with no usable `type` is called.
UNKNOWN_TYPE: Final = "unknown"

#: A type as it is shown. Anything else an item claims is `UNKNOWN_TYPE`:
#: these words end up in the interface, and they come from a file anyone
#: can edit.
_TYPE_WORD: Final = re.compile(r"\A[a-z0-9][a-z0-9 _.-]{0,31}\Z")

#: Bounds on a preset's properties, shared with the service (`config.rs`,
#: `MAX_ENTRY_PROPERTIES` and the two after it): each one becomes a
#: `--set-property NAME=VALUE` argument.
MAX_PROPERTIES: Final = 512
MAX_PROPERTY_VALUE_BYTES: Final = 256

#: A property name the service accepts: a TOML bare key that never starts
#: with `-` and has no `=`, so `NAME=VALUE` is one unambiguous argument.
PROPERTY_NAME: Final = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_-]{0,63}\Z")

#: How many declarations or preset values one `project.json` may give before
#: the rest are ignored. The file is already capped at `MAX_PROJECT_BYTES`.
_MAX_DECLARED: Final = 4096

#: A slider's value is read with `std::stof`; anything near float's range
#: would throw and stop the engine.
_MAX_SLIDER_MAGNITUDE: Final = 1e9

#: The smallest normal float. `std::stof` throws (out of range) on any
#: non-zero value smaller in magnitude, `1e-38` and `1.5e-39` included
#: (checked against libstdc++), so such a value is written as zero.
_FLT_MIN: Final = 1.1754943508222875e-38

#: A number the engine reads as an `int` (a combo's option value, through
#: nlohmann's ``get<int>``): anything outside this does not fit, and is left
#: out rather than handed over to be truncated.
_INT_MIN: Final = -(2**31)
_INT_MAX: Final = 2**31 - 1

#: The declared property types a preset value is passed on for. The engine
#: reads these from text (Property.h); the rest are labels, files and text
#: fields, which a preset can't usefully set and a path must not come from.
_PASSED_TYPES: Final[frozenset[str]] = frozenset({"bool", "slider", "color", "combo"})


@dataclass(frozen=True, slots=True)
class Declared:
    """One property a wallpaper declares in its ``general.properties``."""

    name: str
    #: `bool`, `slider`, `color` or `combo` (the only types kept).
    type: str
    #: A combo's option values, as the engine keys them (an integer as its
    #: decimal text). Empty for the other types.
    options: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class WorkshopItem:
    """One installed Wallpaper Engine wallpaper."""

    #: The Steam Workshop file id, and the directory name. Stable across
    #: reinstalls and across machines, which is why a pairing keys on it.
    id: str
    directory: Path
    title: str
    #: `video`, `scene`, `web`, folded. Not an enum: Wallpaper Engine may add
    #: one and an unknown type should be reported, not dropped.
    kind: str
    #: The playable file, when there is one on disk. `None` for a scene, whose
    #: content is inside `scene.pkg`.
    entry: Path | None
    preview: Path | None
    #: Whether `project.json` has the string `title` and the `file` the engine
    #: requires of every item it loads (ProjectParser.cpp:36, :44).
    complete: bool = True
    #: `file` as `project.json` names it, or empty.
    file: str = ""
    #: For a preset (`kind` is `PRESET`): the Workshop id of the wallpaper it
    #: is a preset of, or empty when it names none that can be trusted.
    dependency: str = ""
    #: A preset's own values, as JSON gave them, under names the service
    #: would accept. Translated against its base by `preset_properties`.
    preset: tuple[tuple[str, bool | int | float | str], ...] = ()
    #: The properties this wallpaper declares, for a preset of it.
    declared: tuple[Declared, ...] = ()

    @property
    def is_video(self) -> bool:
        return self.kind == "video" and self.entry is not None

    @property
    def still_preview(self) -> Path | None:
        """The preview when it is directly usable as a still, else ``None``.

        A `.gif` preview is not a still -- this app plays gifs -- so it comes
        back as ``None`` here and is used as a *source* for a generated still
        instead.
        """
        if self.preview is None:
            return None
        return self.preview if self.preview.suffix.lower() in _STILL_SUFFIXES else None


def _is_folder(path: Path, *, follow: bool, on_unseen: OnUnseen | None) -> bool:
    """Whether ``path`` is a folder; a stat that fails for another reason is said.

    ``Path.is_dir`` answers False to any OSError, which would make "couldn't
    look" read as "not there". Absent (or under something that is not a
    folder) is quiet; anything else (permissions, a dead mount) goes to
    ``on_unseen`` as unreadable.
    """
    try:
        info = path.stat() if follow else path.lstat()
    except OSError as error:
        if on_unseen is not None and not is_absent(error):
            on_unseen(path, UnseenKind.UNREADABLE, error.strerror or str(error))
        return False
    return stat.S_ISDIR(info.st_mode)


def steam_roots(extra: Sequence[Path] = (), *, include_defaults: bool = True) -> tuple[Path, ...]:
    """Steam installations to look in, in order, without duplicates.

    ``extra`` adds to the usual places rather than replacing them, because a
    user with Steam somewhere unusual almost certainly still has the ordinary
    one too. ``include_defaults=False`` is the escape hatch, and exists for
    tests: without it there is no way to ask this module about a directory
    without also being told about whatever Steam the developer happens to have
    installed, which is a test that passes for the wrong reason on one machine
    and fails on every other.
    """
    seen: dict[Path, None] = {}
    candidates = (*extra, *default_steam_roots()) if include_defaults else tuple(extra)
    for candidate in candidates:
        expanded = Path(candidate).expanduser()
        # A root that can't be looked at is kept: the scan that follows it
        # says so where it fails (`content_directories`), instead of this
        # dropping it as if it were not there.
        if _is_folder(expanded, follow=True, on_unseen=None) or _unlooked(expanded):
            seen.setdefault(expanded, None)
    return tuple(seen)


def _unlooked(path: Path) -> bool:
    """Whether a stat of ``path`` fails for a reason other than "not there"."""
    try:
        path.stat()
    except OSError as error:
        return not is_absent(error)
    return False


def library_folders(root: Path, on_unseen: OnUnseen | None = None) -> tuple[Path, ...]:
    """Every library folder Steam knows about, including ``root`` itself.

    Steam spreads installations across drives, and Workshop content follows the
    game rather than the main installation -- so looking only where Steam is
    installed finds nothing for anyone with a second disk.
    """
    found: dict[Path, None] = {root: None}
    index = root / _LIBRARY_INDEX
    try:
        raw = file_io.read_regular_text(index, MAX_INDEX_BYTES, encoding="utf-8", errors="replace")
        if raw is not None:
            for match in _PATH_LINE.finditer(raw):
                candidate = Path(match.group(1))
                if _is_folder(candidate, follow=False, on_unseen=on_unseen):
                    found.setdefault(candidate, None)
    except OSError as error:
        # An unreadable index is not a reason to find nothing: the root itself
        # is still a library folder. But which other folders it named is not
        # known, so it is said, at the folder that holds the index.
        if on_unseen is not None and getattr(error, "errno", None) is not None:
            on_unseen(index.parent, UnseenKind.UNREADABLE, error.strerror or str(error))
    return tuple(found)


def content_directories(
    extra_roots: Sequence[Path] = (),
    *,
    include_defaults: bool = True,
    on_unseen: OnUnseen | None = None,
) -> tuple[Path, ...]:
    """Every directory that could hold installed Wallpaper Engine content."""
    found: dict[Path, None] = {}
    for root in steam_roots(extra_roots, include_defaults=include_defaults):
        for folder in library_folders(root, on_unseen):
            content = folder / "steamapps" / "workshop" / "content" / APP_ID
            if _is_folder(content, follow=True, on_unseen=on_unseen):
                found.setdefault(content, None)
    return tuple(found)


def _read_project(directory: Path, on_unseen: OnUnseen | None = None) -> WorkshopItem | None:
    """One item from its `project.json`, or ``None`` if it is not one.

    Never raises: a Workshop directory is written by Steam and edited by
    whoever likes, and one unreadable item must not cost the other forty-eight.
    Whatever reading one raises (a value nothing here foresaw) is said for that
    folder, as one whose `project.json` could not be read, and the scan goes
    on. It is a "not a wallpaper" place, as a malformed file is: one item's
    surprise must not stop the compiler either.
    """
    try:
        return _parse_project(directory, on_unseen)
    except Exception as error:  # the promise above
        if on_unseen is not None:
            on_unseen(
                directory,
                UnseenKind.NOT_A_WALLPAPER,
                f"its project.json could not be read ({type(error).__name__})",
            )
        return None


def _parse_project(directory: Path, on_unseen: OnUnseen | None) -> WorkshopItem | None:
    """`_read_project`'s work, which may raise for what nothing here foresaw."""
    project = directory / "project.json"
    try:
        raw = file_io.read_regular_text(
            project, MAX_PROJECT_BYTES, encoding="utf-8", errors="replace"
        )
    except OSError as error:
        # With an errno: one that could not be opened, so it is not known
        # whether this is a wallpaper.
        if getattr(error, "errno", None) is not None:
            if on_unseen is not None:
                on_unseen(directory, UnseenKind.UNREADABLE, error.strerror or str(error))
        elif on_unseen is not None:
            # Opened, but not a file this can read: not a regular file, or
            # larger than any real one. Looked at, and not a wallpaper.
            on_unseen(directory, UnseenKind.NOT_A_WALLPAPER, f"its project.json {_why(error)}")
        return None
    if raw is None:
        # Steam makes the folder before the files; it may be on its way.
        if on_unseen is not None:
            on_unseen(directory, UnseenKind.NOT_A_WALLPAPER, NO_PROJECT)
        return None
    try:
        document = json.loads(raw)
    except ValueError, RecursionError:
        document = None
    if not isinstance(document, dict):
        if on_unseen is not None:
            on_unseen(directory, UnseenKind.NOT_A_WALLPAPER, NOT_A_PROJECT)
        return None

    kind = document.get("type")
    title = document.get("title")
    file = document.get("file")
    entry = _beside(directory, file, on_unseen)
    preview = _beside(directory, document.get("preview"), on_unseen)
    preset = document.get("preset")
    dependency = document.get("dependency")
    if isinstance(dependency, int) and not isinstance(dependency, bool):
        dependency = str(dependency)
    return WorkshopItem(
        id=directory.name,
        directory=directory,
        # A preset has no type of its own: it is a set of values for another
        # item, its `dependency`.
        kind=PRESET if isinstance(preset, dict) else _type_word(kind),
        title=title.strip() if isinstance(title, str) and title.strip() else directory.name,
        entry=entry,
        preview=preview,
        complete=isinstance(title, str) and isinstance(file, str) and bool(file.strip()),
        file=file.strip() if isinstance(file, str) else "",
        dependency=(
            dependency.strip()
            if isinstance(dependency, str) and is_workshop_id(dependency.strip())
            else ""
        ),
        preset=_preset_values(preset) if isinstance(preset, dict) else (),
        declared=_declarations(document.get("general")),
    )


#: What the scan says about a Workshop folder it can't list.
NO_PROJECT: Final = "a Wallpaper Engine folder with no project.json"
NOT_A_PROJECT: Final = "its project.json is not Wallpaper Engine's JSON"


def _why(error: OSError) -> str:
    """The end of `file_io.FileReadError`'s sentence, without the path it starts with."""
    text = str(error)
    if "exceeds" in text:
        return f"is larger than {MAX_PROJECT_BYTES // 1024} KiB"
    if "not a regular file" in text:
        return "is not a regular file"
    return "could not be read"


def _type_word(kind: object) -> str:
    """``type`` as it is compared and shown: case-folded, or `UNKNOWN_TYPE`.

    Case-folded because the real content spells it both `video` and `Video`,
    and trusting the casing hid eight wallpapers out of forty-nine.
    """
    if not isinstance(kind, str):
        return UNKNOWN_TYPE
    folded = kind.strip().casefold()
    return folded if _TYPE_WORD.fullmatch(folded) else UNKNOWN_TYPE


def _preset_values(
    preset: dict[object, object],
) -> tuple[tuple[str, bool | int | float | str], ...]:
    """A preset's values worth translating: a scalar under a name the service accepts."""
    kept: list[tuple[str, bool | int | float | str]] = []
    for name, value in preset.items():
        if len(kept) >= _MAX_DECLARED:
            break
        if (
            isinstance(name, str)
            and PROPERTY_NAME.fullmatch(name)
            and isinstance(value, bool | int | float | str)
        ):
            kept.append((name, value))
    return tuple(kept)


def _declarations(general: object) -> tuple[Declared, ...]:
    """The typed properties ``general.properties`` declares, of the types passed on."""
    properties = general.get("properties") if isinstance(general, dict) else None
    if not isinstance(properties, dict):
        return ()
    found: list[Declared] = []
    for name, spec in properties.items():
        if len(found) >= _MAX_DECLARED:
            break
        if not isinstance(name, str) or not PROPERTY_NAME.fullmatch(name):
            continue
        if not isinstance(spec, dict):
            continue
        kind = spec.get("type")
        if not isinstance(kind, str) or kind not in _PASSED_TYPES:
            continue
        options: frozenset[str] = frozenset()
        if kind == "combo":
            listed = spec.get("options")
            if not isinstance(listed, list):
                continue
            options = frozenset(
                value
                for value in (_combo_key(option) for option in listed[:_MAX_DECLARED])
                if value is not None
            )
        found.append(Declared(name=name, type=kind, options=options))
    return tuple(found)


def _combo_key(option: object) -> str | None:
    """A combo option's value as the engine keys it (PropertyParser.cpp:57-68)."""
    if not isinstance(option, dict):
        return None
    value = option.get("value")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _engine_int(value)
    if isinstance(value, str) and valid_property_value(value):
        return value
    return None


def _engine_int(value: int | float, *, integral: bool = False) -> str | None:
    """``value`` as the engine's ``int`` of it, in decimal, or ``None`` if it doesn't fit.

    An integer is judged as one: JSON allows any number of digits, and
    `math.isfinite` on a 400-digit integer raises `OverflowError` rather than
    answering. ``integral``: a float with a fraction is refused, not truncated.
    """
    if isinstance(value, int):
        return str(value) if _INT_MIN <= value <= _INT_MAX else None
    if not math.isfinite(value) or (integral and value != int(value)):
        return None
    return str(int(value)) if _INT_MIN <= value <= _INT_MAX else None


def valid_property_value(value: str) -> bool:
    """Whether the service accepts ``value`` (`config.rs`, `property_value_is_valid`).

    JSON can spell a lone surrogate, which is no UTF-8 at all.
    """
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= MAX_PROPERTY_VALUE_BYTES and not any(
        unicodedata.category(character) == "Cc" for character in value
    )


def property_value(declared: Declared, value: object) -> str | None:
    """``value`` as the engine reads the property ``declared``, or ``None`` to leave it out.

    Only what the engine's parser for that type accepts (Property.h): a
    slider is `std::stof`, a colour is `ColorBuilder::parse`, and both stop
    the engine on anything else. Every colour is written with a decimal
    point, because `ColorBuilder` reads `"1 1 0"` as 0-255 integers while
    Wallpaper Engine means 0-1.
    """
    if declared.type == "bool":
        return ("true" if value else "false") if isinstance(value, bool) else None
    if declared.type == "slider":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if isinstance(value, int):
            # Compared as an integer: a JSON integer can have any number of
            # digits, and turning one that size into a float raises.
            return str(value) if abs(value) <= _MAX_SLIDER_MAGNITUDE else None
        if not math.isfinite(value) or abs(value) > _MAX_SLIDER_MAGNITUDE:
            return None
        if 0.0 < abs(value) < _FLT_MIN:
            # What the engine could hold of it is zero; written so, it parses.
            return "0.0"
        return repr(float(value))
    if declared.type == "color":
        if not isinstance(value, str):
            return None
        parts = value.replace(",", " ").split()
        if len(parts) not in (3, 4):
            return None
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            return None
        if not all(math.isfinite(number) and 0.0 <= number <= 1.0 for number in numbers):
            return None
        return " ".join(f"{number:.6f}" for number in numbers)
    if declared.type == "combo":
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            number = _engine_int(value, integral=True)
            if number is None:
                return None
            text = number
        elif isinstance(value, str):
            text = value
        else:
            return None
        return text if text in declared.options and valid_property_value(text) else None
    return None


def preset_properties(preset: WorkshopItem, base: WorkshopItem) -> tuple[tuple[str, str], ...]:
    """``preset``'s values that ``base`` declares, as the engine reads them, in name order.

    A name the base does not declare is left out: the engine would ignore it
    anyway (it walks the base's declarations, WallpaperApplication.cpp:502-518).
    """
    declared = {item.name: item for item in base.declared}
    kept: dict[str, str] = {}
    for name, value in preset.preset:
        found = declared.get(name)
        if found is None:
            continue
        text = property_value(found, value)
        if text is not None:
            kept[name] = text
    return tuple(sorted(kept.items()))


@dataclass(frozen=True, slots=True)
class Playback:
    """How a Workshop item listed as a scene plays, or why it can't."""

    #: The Workshop id to render when it isn't the item's own: a preset's base.
    base: str = ""
    properties: tuple[tuple[str, str], ...] = ()
    unplayable: Unplayable | None = None
    reason: str = ""
    #: What it plays, when that isn't what it shows: a preset of a web
    #: wallpaper plays its base's look ("plays as Electric Eye; ...").
    note: str = ""
    #: The picture its still and colours come from, when not its own preview.
    preview: Path | None = None


def _type_phrase(kind: str) -> str:
    return "with no type it can read" if kind == UNKNOWN_TYPE else f"of the type \u201c{kind}\u201d"


def _refusal(item: WorkshopItem) -> Playback | None:
    """Why ``item`` itself must not be handed to the engine, or ``None``.

    `NEVER_RUN` first, so no change to `SUPPORTED_TYPES` can let one through.
    """
    if item.kind in NEVER_RUN:
        return Playback(
            unplayable=Unplayable.RUNTIME,
            reason=(
                f"it is a program (Wallpaper Engine\u2019s \u201c{item.kind}\u201d type), "
                "and Wall-in-One never runs programs from the Workshop"
            ),
        )
    if item.kind not in SUPPORTED_TYPES:
        return Playback(
            unplayable=Unplayable.RUNTIME,
            reason=(
                "linux-wallpaperengine can\u2019t play Wallpaper Engine wallpapers "
                f"{_type_phrase(item.kind)} yet"
            ),
        )
    if not item.complete:
        return Playback(
            unplayable=Unplayable.MALFORMED,
            reason="its project.json lacks the title or file linux-wallpaperengine needs",
        )
    expected = EXPECTED_FILE.get(item.kind)
    if expected is not None and not item.file.casefold().endswith(expected):
        return Playback(
            unplayable=Unplayable.RUNTIME,
            reason=(
                f"its file isn\u2019t what a \u201c{item.kind}\u201d wallpaper names "
                f"({' or '.join(expected)}), so it is never run"
            ),
        )
    return None


def playback(item: WorkshopItem, installed: Mapping[str, WorkshopItem]) -> Playback:
    """How ``item`` plays through linux-wallpaperengine, given everything ``installed`` by id.

    Its own type must be one the engine runs (`SUPPORTED_TYPES`) and never
    one of `NEVER_RUN`, with the file its type names; a preset plays its
    base, which must pass the same, with the values the base declares.
    Anything else is listed and never handed to the engine.
    """
    if item.kind != PRESET:
        return _refusal(item) or Playback()
    if not item.dependency:
        return Playback(
            unplayable=Unplayable.NEEDS_BASE,
            reason="it is a preset that doesn\u2019t say which wallpaper it is for",
        )
    base = installed.get(item.dependency)
    if base is None:
        return Playback(
            base=item.dependency,
            unplayable=Unplayable.NEEDS_BASE,
            reason=(
                f"it is a preset of Workshop item {item.dependency}, which isn\u2019t installed. "
                "Subscribe to it in Steam, then check Wallpaper Engine again"
            ),
        )
    if base.kind == PRESET:
        return Playback(
            base=base.id,
            unplayable=Unplayable.MALFORMED,
            reason=f"it is a preset of {base.title}, which is itself a preset",
        )
    refused = _refusal(base)
    if refused is not None:
        return Playback(
            base=base.id,
            unplayable=refused.unplayable,
            reason=f"it is a preset of {base.title}, which can\u2019t play: {refused.reason}",
        )
    if base.kind == "web":
        # linux-wallpaperengine hands a page no properties (nothing calls into
        # it: CWeb.cpp only loads the URL), so the screen shows the base as it
        # is. Say so, pass nothing, and take the still and colours from the
        # base's own preview so they match what is on screen.
        return Playback(
            base=base.id,
            note=f"plays as {base.title}; its settings aren\u2019t applied",
            preview=base.preview or item.preview,
        )
    properties = preset_properties(item, base)
    if len(properties) > MAX_PROPERTIES:
        return Playback(
            base=base.id,
            unplayable=Unplayable.MALFORMED,
            reason=(
                f"it sets {len(properties)} of {base.title}\u2019s properties, and Wall-in-One "
                f"passes at most {MAX_PROPERTIES}"
            ),
        )
    return Playback(base=base.id, properties=properties)


# -- what the Library says about them ----------------------------------------------


def unsupported_titles(items: Iterable[MediaItem]) -> tuple[str, ...]:
    """The titles of listed items no renderer here runs (`Unplayable.RUNTIME`), by name.

    A preset waiting for its base is not one of them: installing the base
    is all it needs, and its own card says so.
    """
    return tuple(
        sorted(
            (item.name for item in items if item.unplayable is Unplayable.RUNTIME),
            key=str.casefold,
        )
    )


def _beside(directory: Path, name: object, on_unseen: OnUnseen | None = None) -> Path | None:
    """A file named by `project.json`, resolved inside its own directory.

    A name with a separator in it is refused rather than joined: these files
    are not ours, and `../../` in one of them must not reach out of the
    Workshop tree.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    if "/" in name or "\\" in name or name.strip() in (".", ".."):
        return None
    candidate = directory / name.strip()
    try:
        info = candidate.lstat()
    except OSError as error:
        if on_unseen is not None and not is_absent(error):
            on_unseen(directory, UnseenKind.UNREADABLE, error.strerror or str(error))
        return None
    return candidate if stat.S_ISREG(info.st_mode) else None


def scan(
    extra_roots: Sequence[Path] = (),
    *,
    include_defaults: bool = True,
    on_unseen: OnUnseen | None = None,
) -> tuple[WorkshopItem, ...]:
    """Every installed Wallpaper Engine wallpaper, by id.

    Bounded and quiet: a Steam that is not installed, a Workshop directory that
    is not there, and an item somebody has been editing all produce fewer
    results rather than an error.
    """
    return scan_content_directories(
        content_directories(extra_roots, include_defaults=include_defaults, on_unseen=on_unseen),
        on_unseen,
    )


def scan_content_directories(
    directories: Sequence[Path],
    on_unseen: OnUnseen | None = None,
) -> tuple[WorkshopItem, ...]:
    """Scan exact Wallpaper Engine content directories without rediscovery.

    Normal application scans start from Steam roots. Evidence-gated migration
    instead retains already-validated ``.../content/431960`` directories and
    must not reinterpret them as Steam roots or silently add defaults.
    """
    found: dict[str, WorkshopItem] = {}
    for position, content in enumerate(directories):
        try:
            entries = sorted(content.iterdir())
        except OSError as error:
            if on_unseen is not None and not is_absent(error):
                on_unseen(content, UnseenKind.UNREADABLE, error.strerror or str(error))
            continue
        for directory in entries:
            if len(found) >= MAX_ITEMS:
                if on_unseen is not None:
                    # This folder, and every one after it, which is never listed.
                    for left in directories[position:]:
                        on_unseen(left, UnseenKind.LIMIT, f"stopped at {MAX_ITEMS} Workshop items")
                return tuple(found.values())
            if not _is_folder(directory, follow=False, on_unseen=on_unseen):
                continue
            item = _read_project(directory, on_unseen)
            if item is not None and item.id not in found:
                found[item.id] = item
    return tuple(found.values())


def videos(items: Iterable[WorkshopItem]) -> tuple[WorkshopItem, ...]:
    """The ones this app can already play, which is most of them."""
    return tuple(item for item in items if item.is_video)


def unplayable(items: Iterable[WorkshopItem]) -> tuple[WorkshopItem, ...]:
    """The ones that are not directly playable media files.

    This is kept as the historical Workshop-classification helper: videos have
    an ordinary media file while scenes require linux-wallpaperengine to read
    ``scene.pkg``. Callers still need the latter items so they are reported,
    never hidden from the library.
    """
    return tuple(item for item in items if not item.is_video)


# -- getting more of it ---------------------------------------------------
#
# Step 16, and deliberately the smallest thing that could work. Wallpaper
# Engine's catalogue is Steam's, and Steam already has a browser for it that
# handles the account, the payment, the subscription and the download. Writing
# a scraper would mean reimplementing all four badly, and subscribing without
# Steam is not possible at all -- the Workshop API has no unauthenticated
# subscribe. So this app links out and lets Steam be Steam.
#
# Both spellings are produced. `steam://` opens the client's own overlay, which
# is where somebody can actually press Subscribe; the https URL is the fallback
# for a machine where Steam is not installed, and is also what a middle-click
# into a browser expects.

#: The Workshop's own browse page for Wallpaper Engine.
BROWSE_URL: Final = f"https://steamcommunity.com/app/{APP_ID}/workshop/"

#: The same, as a handler the Steam client registers for itself.
BROWSE_URI: Final = f"steam://url/SteamWorkshopPage/{APP_ID}"

_ID_RE: Final = re.compile(r"\A[0-9]{1,20}\Z")


def is_workshop_id(value: str) -> bool:
    """Whether ``value`` is a plausible Workshop id.

    Digits only. These end up in a URL handed to a browser or to the Steam
    client, and an id read off a directory name on disk is not something to
    interpolate unchecked.
    """
    return _ID_RE.fullmatch(value) is not None


def item_page_url(workshop_id: str) -> str:
    """The Workshop page for one item, or ``""`` for an id we will not trust."""
    if not is_workshop_id(workshop_id):
        return ""
    return f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"


def item_page_uri(workshop_id: str) -> str:
    """The same page, opened inside the Steam client."""
    if not is_workshop_id(workshop_id):
        return ""
    return f"steam://url/CommunityFilePage/{workshop_id}"


def is_steam_installed(extra_roots: Sequence[Path] = (), *, include_defaults: bool = True) -> bool:
    """Whether there is a Steam here at all.

    Decides which of the two spellings to offer first: a `steam://` link on a
    machine with no Steam opens nothing and explains nothing.
    """
    return bool(steam_roots(extra_roots, include_defaults=include_defaults))


def links(
    workshop_id: str = "", extra_roots: Sequence[Path] = (), *, include_defaults: bool = True
) -> tuple[str, str]:
    """``(preferred, fallback)`` URLs for the Workshop, or for one item in it.

    Preferred is the Steam client where Steam exists, because that is the only
    place the Subscribe button works; the https page is what is left otherwise,
    and is always returned as the fallback so a caller can offer both.
    """
    if workshop_id:
        native, web = item_page_uri(workshop_id), item_page_url(workshop_id)
        if not web:
            return "", ""
    else:
        native, web = BROWSE_URI, BROWSE_URL
    installed = is_steam_installed(extra_roots, include_defaults=include_defaults)
    return (native, web) if installed else (web, web)
