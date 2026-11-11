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
- The preview is a `.gif` for 46 of them. It shows on the library card only:
  it is the author's file, so it is never a still or the source of colours
  (`preview_still`).

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
import os
import re
import stat
import string
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from wall_in_one import file_io
from wall_in_one.dials import MAX_TEXT_BYTES, PROPERTY_TYPES, SceneProperty, valid_text
from wall_in_one.library.model import Kind, MediaItem, Unplayable, UnseenKind, is_absent

#: Where the scan is told about a place it could not look into: the folder,
#: why (`UnseenKind`), and the system's words. Optional everywhere below.
OnUnseen = Callable[[Path, UnseenKind, str], None]

#: Wallpaper Engine's Steam application id.
APP_ID: Final = "431960"

#: What a Wallpaper Engine wallpaper is labelled as in the grid: the
#: `MediaItem.provider` of every item the scan lists from here
#: (`scan.WORKSHOP_PROVIDER`).
WORKSHOP_PROVIDER: Final = "Wallpaper Engine"


#: The directory components every Workshop item's folder sits under, wherever
#: Steam is (any drive, any library folder): `.../steamapps/workshop/content/431960/<id>`.
_CONTENT_PARTS: Final = ("steamapps", "workshop", "content", APP_ID.casefold())


def preview_still(item: MediaItem, previews: bool) -> bool:
    """Whether ``item``'s automatic still is drawn from its own preview.

    The one decision, with `no_automatic_still`: ``previews`` is
    `config.Settings.preview_stills` (web wallpapers on and "use the wallpaper's
    own preview as its still" on). Only a web page or a preset (`MediaItem.preview_kind`)
    is ever drawn from a preview, and only then: the preview is the author's file.
    """
    return item.preview_kind and previews


def no_automatic_still(item: MediaItem, previews: bool) -> bool:
    """Whether ``item`` gets no automatic still at all, only one a person picks.

    A web page or a preset while ``previews`` (`preview_still`) is off. A
    `<id>.png` of 0.4.1's, drawn from a preview, is then left on disk and unused.
    """
    return item.preview_kind and not previews


def labelled_workshop(item: MediaItem) -> bool:
    """Whether the scan labelled ``item`` a Workshop item (`WORKSHOP_PROVIDER`).

    Only the label, which the scan does not apply to Steam's tree when a library
    root contains it (the root walk reaches the file first and calls it
    ``local``). Never ask this alone: `is_workshop_content` is the test, and
    this is what a caller hands it as ``labelled``.
    """
    return item.provider == WORKSHOP_PROVIDER


@dataclass
class _ScanMemo:
    """What the place tests learned during one scan (`one_scan`)."""

    #: A path as given, resolved; ``None`` when it couldn't be.
    resolved: dict[Path, Path | None] = field(default_factory=dict)
    #: A resolved folder: whether it holds a ``project.json`` (or can't be looked at).
    holds_project: dict[Path, bool] = field(default_factory=dict)
    #: `in_content_tree` of a path as given.
    in_content: dict[Path, bool] = field(default_factory=dict)
    #: `item_folders_of` of a folder and the roots, as given.
    item_folders: dict[tuple[Path, tuple[Path, ...]], tuple[Path, ...]] = field(
        default_factory=dict
    )


_SCAN_MEMO: ContextVar[_ScanMemo | None] = ContextVar("wall_in_one_scan_places", default=None)


@contextmanager
def one_scan() -> Iterator[None]:
    """Within this, each path is resolved and each folder looked at once (`scan.scan`).

    The place tests (`in_content_tree`, `item_folder_of`, `folder_gate`,
    `is_workshop_content`) run several times for every video of a scan, by the
    walk and by the still and sidecar readers. What they learn is kept for that
    scan alone, context-local, and dropped with it: nothing outlives the scan
    to go stale.
    """
    token = _SCAN_MEMO.set(_ScanMemo())
    try:
        yield
    finally:
        _SCAN_MEMO.reset(token)


def _resolved(path: Path) -> Path | None:
    """``path`` resolved, or ``None`` if it can't be (kept for the scan, `one_scan`)."""
    memo = _SCAN_MEMO.get()
    if memo is not None and path in memo.resolved:
        return memo.resolved[path]
    try:
        found: Path | None = path.resolve()
    except OSError, ValueError, RuntimeError:
        found = None
    if memo is not None:
        memo.resolved[path] = found
    return found


def _holds_project(folder: Path) -> bool:
    """Whether the resolved ``folder`` holds a ``project.json`` (or can't be looked at)."""
    memo = _SCAN_MEMO.get()
    if memo is not None and folder in memo.holds_project:
        return memo.holds_project[folder]
    try:
        candidate = folder / "project.json"
        held = candidate.is_file() or candidate.is_symlink()
    except OSError, ValueError:
        held = True
    if memo is not None:
        memo.holds_project[folder] = held
    return held


def in_content_tree(path: Path) -> bool:
    """Whether the resolved ``path`` lies under a ``steamapps/workshop/content/431960`` folder.

    By path components, compared case-insensitively (a case-insensitive mount
    spells the same folder any way), so it holds on any drive and with no Steam
    config. A path that can't be resolved is treated as inside (not trusted).
    """
    memo = _SCAN_MEMO.get()
    if memo is not None and path in memo.in_content:
        return memo.in_content[path]
    resolved = _resolved(path)
    if resolved is None:
        inside = True
    else:
        parts = tuple(part.casefold() for part in resolved.parts)
        width = len(_CONTENT_PARTS)
        inside = any(
            parts[at : at + width] == _CONTENT_PARTS for at in range(len(parts) - width + 1)
        )
    if memo is not None:
        memo.in_content[path] = inside
    return inside


def item_folders_of(folder: Path, roots: Sequence[Path] = ()) -> tuple[Path, ...]:
    """Every folder, ``folder`` first, that holds a ``project.json``, resolved; empty if none.

    An item copied in by hand has subfolders (``assets/``, ``img/``) that belong
    to it as much as the files beside its ``project.json``. The search goes up
    from ``folder`` to the nearest library root that contains it, never above (a
    ``project.json`` in somebody's home folder makes nothing of what is below it
    Workshop content). With no ``roots`` only ``folder`` itself is looked at.
    A folder that can't be looked at counts as an item folder (fails closed), and
    so does ``folder`` itself when it can't be resolved.
    """
    memo = _SCAN_MEMO.get()
    key = (folder, tuple(roots))
    if memo is not None and key in memo.item_folders:
        return memo.item_folders[key]
    found = _item_folders_of(folder, roots)
    if memo is not None:
        memo.item_folders[key] = found
    return found


def _item_folders_of(folder: Path, roots: Sequence[Path]) -> tuple[Path, ...]:
    """`item_folders_of`'s walk up from ``folder``."""
    current = _resolved(folder)
    bounds = [_resolved(root) for root in roots]
    if current is None or None in bounds:
        return (folder,)
    # The nearest root that holds it, whatever the order of the roots: the one whose walk
    # lists its files (`folder_gate`, asked for each root), so that a library folder of
    # someone's own inside an item folder pairs what it lists (R42-12).
    ceiling = max(
        (root for root in bounds if root and current.is_relative_to(root)),
        key=lambda root: len(root.parts),
        default=None,
    )
    found: list[Path] = []
    while True:
        if _holds_project(current):
            found.append(current)
        if ceiling is None or current == ceiling or current.parent == current:
            return tuple(found)
        current = current.parent


def item_folder_of(folder: Path, roots: Sequence[Path] = ()) -> Path | None:
    """The nearest of `item_folders_of`, or ``None`` when ``folder`` is in no item's folder."""
    found = item_folders_of(folder, roots)
    return found[0] if found else None


def is_workshop_content(path: Path, *, labelled: bool, roots: Sequence[Path] = ()) -> bool:
    """Whether the file at ``path`` is Workshop content, which is the author's, not ours.

    The one test for "may what sits beside this file be trusted?", decided by
    place and not only by label. True when any of these holds:

    - ``labelled``: the scan called it a Workshop item (`labelled_workshop`);
    - its resolved path lies under a ``steamapps/workshop/content/431960``
      directory (`in_content_tree`), so it holds on any drive and inside a
      configured library root too;
    - a ``project.json`` sits in its folder, or in one above it up to the nearest
      library root it is in (``roots``, `item_folder_of`): a Workshop item copied
      into the library by hand is still its author's content, subfolders included.

    Nothing from beside such a file is a still of ours: not its sidecar, not a
    picture named like it, and not its preview. Its still is only what
    Wall-in-One makes from the item itself or what a person picked.
    """
    if labelled:
        return True
    if in_content_tree(path):
        return True
    return item_folder_of(path.parent, roots) is not None


def folder_gate(folder: Path, roots: Sequence[Path] = ()) -> frozenset[str] | None:
    """The names of ``folder``'s files the library walk may list as local media; ``None``: all.

    By name, so the walk compares its own spelling of each file, whatever links
    lie above the library root. The walk of a library root asks this once per
    folder, with the test every other gate uses (`is_workshop_content`).
    Workshop content is untrusted, so a root that holds Steam's tree, or an item
    copied in by hand, must not list an item's files (a web wallpaper's
    ``preview.gif`` is a video to the classifier), at any depth, as local media,
    where no web gate would hold:

    - under `in_content_tree`: nothing. The Workshop scan lists those items itself.
    - in or below a folder with a ``project.json`` (`item_folders_of`): an item's,
      and it fails closed. Everything below an item folder is the item's, and no
      ``project.json`` names a file in a subfolder, so a folder with any item
      folder above it lists nothing, whatever ``project.json`` files say (one
      nested in a web item can't choose what is listed). An item folder with
      none above it lists only the file its ``project.json`` names, if it is a
      readable ``video``, so a hand-copied video still works; a web page, an
      application, any other type, or one that can't be read, lists nothing.
    """
    if in_content_tree(folder):
        return frozenset()
    items = item_folders_of(folder, roots)
    if not items:
        return None
    if len(items) > 1 or items[0] != _resolved(folder):
        return frozenset()
    project = _read_project(items[0])
    if project is None or not project.is_video or project.entry is None:
        return frozenset()
    return frozenset({project.entry.name})


#: Where linux-wallpaperengine looks for `--bg <id>`, under the home folder, in
#: its own order: the service's `ENGINE_WORKSHOP_FOLDERS` (service/src/workshop.rs),
#: Steam's two usual homes, the Flatpak's and the Snap's. Each is followed by `<id>`.
ENGINE_WORKSHOP_FOLDERS: Final = (
    ".local/share/Steam/steamapps/workshop/content/431960",
    ".steam/steam/steamapps/workshop/content/431960",
    ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/workshop/content/431960",
    "snap/steam/common/.local/share/Steam/steamapps/workshop/content/431960",
)

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
MAX_PROPERTY_VALUE_BYTES: Final = MAX_TEXT_BYTES

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

#: The types kept from a declaration: those a preset passes on, and a line
#: of text a person may set on their own scene (`dials.PROPERTY_TYPES`).
_DECLARED_TYPES: Final[frozenset[str]] = frozenset(PROPERTY_TYPES)

#: A label longer than this is cut: it is a word or two in real content.
_MAX_LABEL: Final = 120


@dataclass(frozen=True, slots=True)
class Declared:
    """One property a wallpaper declares in its ``general.properties``."""

    name: str
    #: `bool`, `slider`, `color`, `combo` or `textinput` (the only types kept).
    type: str
    #: A combo's option values, as the engine keys them (an integer as its
    #: decimal text). Empty for the other types.
    options: frozenset[str] = frozenset()
    #: Its ``text``, or empty.
    label: str = ""
    #: Its ``value`` as the engine reads it, or ``None`` when the engine
    #: couldn't (`property_value`).
    default: str | None = None
    #: A slider's ``min``, ``max`` and ``step``, as the engine defaults them.
    minimum: float = 0.0
    maximum: float = 0.0
    step: float = 0.0
    #: A combo's (value, label), in the order declared.
    choices: tuple[tuple[str, str], ...] = ()
    #: Its ``order``, where the author sorted it.
    order: int = 0


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
    #: The `type` it declares, folded as `kind` is, even when it is a preset
    #: (`kind` then says `PRESET`); empty when it declares none. `NEVER_RUN`
    #: is judged on this too.
    declared_type: str = ""
    #: For a preset (`kind` is `PRESET`): the Workshop id of the wallpaper it
    #: is a preset of, or empty when it names none that can be trusted.
    dependency: str = ""
    #: `type` stripped and case-folded, which is how a Workshop video has
    #: always been read (mpvpaper plays it; the engine isn't asked), and how
    #: a `NEVER_RUN` type is recognised however it is padded or cased. `kind`
    #: is the engine-strict reading, for what is handed to the engine.
    lenient_type: str = ""
    #: A preset's own values, as JSON gave them, under names the service
    #: would accept. Translated against its base by `preset_properties`.
    preset: tuple[tuple[str, bool | int | float | str], ...] = ()
    #: The properties this wallpaper declares, for a preset of it.
    declared: tuple[Declared, ...] = ()

    @property
    def is_video(self) -> bool:
        """A video mpvpaper plays (its file is there): never judged as the engine's types are."""
        return self.is_video_type and self.entry is not None

    @property
    def is_video_type(self) -> bool:
        """It says `video` (leniently, `declared_video`) and isn't a preset."""
        return (self.lenient_type == "video" or self.kind == "video") and self.kind != PRESET


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
        # A preset object alone doesn't make one: an item with a type of its
        # own and no `dependency` is that type. Nor does it make one of an item
        # that says `video`: 0.4.0 played it as that video (`video:<path>`, which
        # its pairings and favourites are keyed by), and a real preset has no
        # type at all.
        kind=(
            PRESET
            if isinstance(preset, dict)
            and not _says_video(kind)
            and ("dependency" in document or not isinstance(kind, str))
            else _type_word(kind)
        ),
        declared_type=_type_word(kind) if isinstance(kind, str) else "",
        lenient_type=kind.strip().casefold() if isinstance(kind, str) else "",
        title=title.strip() if isinstance(title, str) and title.strip() else directory.name,
        entry=entry,
        preview=preview,
        # The engine opens `file` exactly as written (ProjectParser.cpp:44):
        # one padded with spaces names nothing it can find.
        complete=(
            isinstance(title, str) and isinstance(file, str) and bool(file) and file == file.strip()
        ),
        file=file if isinstance(file, str) else "",
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
NOT_AN_ID: Final = "a folder in Wallpaper Engine's whose name isn't a Workshop id"


def _why(error: OSError) -> str:
    """The end of `file_io.FileReadError`'s sentence, without the path it starts with."""
    text = str(error)
    if "exceeds" in text:
        return f"is larger than {MAX_PROJECT_BYTES // 1024} KiB"
    if "not a regular file" in text:
        return "is not a regular file"
    return "could not be read"


#: ASCII upper case to lower, and nothing else: C's ``tolower``.
_ASCII_LOWER: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _says_video(kind: object) -> bool:
    """Whether ``type`` says `video` as a Workshop video has always been read (`lenient_type`)."""
    return isinstance(kind, str) and kind.strip().casefold() == "video"


def _type_word(kind: object) -> str:
    """``type`` as the engine compares it, or `UNKNOWN_TYPE`.

    Lower-cased because the real content spells it both `video` and `Video`,
    and trusting the casing hid eight wallpapers out of forty-nine. But only
    as ProjectParser.cpp:33 does it, byte by byte with ``tolower``, and with
    nothing stripped: ``"\\u017fcene"`` (a long s, which `str.casefold` would make
    ``"scene"``) and ``" scene"`` are types the engine refuses, so they are
    not playable here either.
    """
    if not isinstance(kind, str):
        return UNKNOWN_TYPE
    folded = kind.translate(_ASCII_LOWER)
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
        if not isinstance(kind, str) or kind not in _DECLARED_TYPES:
            continue
        options: frozenset[str] = frozenset()
        choices: list[tuple[str, str]] = []
        if kind == "combo":
            listed = spec.get("options")
            if not isinstance(listed, list):
                continue
            for option in listed[:_MAX_DECLARED]:
                value = _combo_key(option)
                if value is None or value in options:
                    continue
                assert isinstance(option, dict)
                options |= {value}
                choices.append((value, _label(option.get("label"), value)))
        declared = Declared(
            name=name,
            type=kind,
            options=options,
            label=_label(spec.get("text"), ""),
            minimum=_bound(spec.get("min")),
            maximum=_bound(spec.get("max")),
            step=_bound(spec.get("step")),
            choices=tuple(choices),
            order=_order(spec.get("order")),
        )
        found.append(replace(declared, default=_declared_value(declared, spec)))
    return tuple(found)


def _label(text: object, otherwise: str) -> str:
    """A declaration's ``text`` as one plain line, or ``otherwise``."""
    if not isinstance(text, str):
        return otherwise
    line = " ".join(text.split())
    if not line or any(unicodedata.category(character) == "Cc" for character in line):
        return otherwise
    return line[:_MAX_LABEL]


def _bound(value: object) -> float:
    """A slider's ``min``, ``max`` or ``step``: what the engine reads, 0 when absent."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    if isinstance(value, int):
        return float(value) if abs(value) <= _MAX_SLIDER_MAGNITUDE else 0.0
    return value if math.isfinite(value) and abs(value) <= _MAX_SLIDER_MAGNITUDE else 0.0


def _order(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if _INT_MIN <= value <= _INT_MAX else 0


def _declared_value(declared: Declared, spec: dict[object, object]) -> str | None:
    """The declared ``value`` as the engine reads it (PropertyParser.cpp), or ``None``."""
    value = spec.get("value")
    if declared.type == "bool" and "value" not in spec:
        return "false"
    if declared.type == "textinput":
        return value if isinstance(value, str) and own_text(value) else None
    return property_value(declared, value)


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
    return valid_text(value)


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
    declared = {item.name: item for item in base.declared if item.type in _PASSED_TYPES}
    kept: dict[str, str] = {}
    for name, value in preset.preset:
        found = declared.get(name)
        if found is None:
            continue
        text = property_value(found, value)
        if text is not None:
            kept[name] = text
    return tuple(sorted(kept.items()))


def own_text(value: str) -> bool:
    """Whether ``value`` may be a ``textinput``'s: one line the service accepts."""
    return valid_property_value(value) and value == value.strip("\n\r")


def settable(declared: Iterable[Declared]) -> tuple[SceneProperty, ...]:
    """The properties of ``declared`` a person may set, as both interfaces show them.

    In the author's order. Only one whose declared value the engine reads
    (a control has to start somewhere true), a slider with a range, a combo
    with options, and at most `MAX_PROPERTIES`.
    """
    shown: list[tuple[int, int, SceneProperty]] = []
    for index, item in enumerate(declared):
        if item.default is None or item.type not in _DECLARED_TYPES:
            continue
        step = item.step
        if item.type == "slider":
            if not item.minimum < item.maximum:
                continue
            if not item.minimum <= float(item.default) <= item.maximum:
                continue
            if not 0 < step <= item.maximum - item.minimum:
                step = (item.maximum - item.minimum) / 100
        if item.type == "combo" and not item.choices:
            continue
        shown.append(
            (
                item.order,
                index,
                SceneProperty(
                    name=item.name,
                    type=item.type,
                    label=item.label or item.name,
                    default=item.default,
                    minimum=item.minimum,
                    maximum=item.maximum,
                    step=step,
                    options=item.choices,
                ),
            )
        )
    shown.sort(key=lambda entry: entry[:2])
    return tuple(found for _order, _index, found in shown[:MAX_PROPERTIES])


def own_value(setting: SceneProperty, value: str) -> str | None:
    """``value`` as the engine reads ``setting``, or ``None`` when it would refuse it.

    A person's value for one of their scene's properties, checked as a
    preset's is (`property_value`) and against the declaration's own range
    and options: the last look before it becomes an argument.
    """
    if not valid_property_value(value):
        return None
    if setting.type == "bool":
        return value if value in ("true", "false") else None
    if setting.type == "slider":
        try:
            number = float(value)
        except ValueError:
            return None
        if not math.isfinite(number) or not setting.minimum <= number <= setting.maximum:
            return None
        return property_value(Declared(name=setting.name, type="slider"), number)
    if setting.type == "color":
        return property_value(Declared(name=setting.name, type="color"), value)
    if setting.type == "combo":
        return value if value in {option for option, _label in setting.options} else None
    if setting.type == "textinput":
        return value if own_text(value) else None
    return None


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
    #: The picture its library card shows, when not its own preview (never its still).
    preview: Path | None = None
    #: It is, or would play, a program (`NEVER_RUN`): never run.
    program: bool = False
    #: It plays a web page: a ``web`` wallpaper, or a preset of one.
    web: bool = False


def _type_phrase(kind: str) -> str:
    return "with no type it can read" if kind == UNKNOWN_TYPE else f"of the type \u201c{kind}\u201d"


def _program(kind: str) -> Playback:
    """The refusal of an item of a `NEVER_RUN` type: a program, never run."""
    return Playback(
        unplayable=Unplayable.RUNTIME,
        reason=(
            f"it is a program (Wallpaper Engine\u2019s \u201c{kind}\u201d type), "
            "and Wall-in-One never runs programs from the Workshop"
        ),
        program=True,
    )


def _refusal(item: WorkshopItem) -> Playback | None:
    """Why ``item`` itself must not be handed to the engine, or ``None``.

    `NEVER_RUN` first, so no change to `SUPPORTED_TYPES` can let one through.
    """
    executable = next(
        (kind for kind in (item.kind, item.declared_type, item.lenient_type) if kind in NEVER_RUN),
        None,
    )
    if executable is not None:
        return _program(executable)
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
        return _refusal(item) or Playback(web=item.kind == "web")
    for declared in (item.declared_type, item.lenient_type):
        if declared in NEVER_RUN:
            # A preset object on an item that says it is a program: still one.
            return _program(declared)
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
    if base.kind == "video" and base.entry is None:
        # Not listed itself (`scan.workshop_items`): its file is gone, or one
        # this app won't open (a link, a path into another folder). The
        # preset has nothing it may play until the base is whole again.
        return Playback(
            base=base.id,
            unplayable=Unplayable.NEEDS_BASE,
            reason=(
                f"it is a preset of {base.title}, whose video file isn\u2019t there. "
                "Reinstall it in Steam, then check Wallpaper Engine again"
            ),
        )
    refused = _refusal(base)
    if refused is not None:
        return Playback(
            base=base.id,
            unplayable=refused.unplayable,
            reason=f"it is a preset of {base.title}, which can\u2019t play: {refused.reason}",
            program=refused.program,
        )
    if base.kind == "web":
        # linux-wallpaperengine hands a page no properties (nothing calls into
        # it: CWeb.cpp only loads the URL), so the screen shows the base as it
        # is. Say so and pass nothing. Its preview shows on the card only; it
        # is never its still or its colours (`preview_still`).
        return Playback(
            base=base.id,
            note=f"plays as {base.title}; its settings aren\u2019t applied",
            preview=base.preview or item.preview,
            web=True,
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


#: Why a web wallpaper isn't played while ``allow_web_wallpapers`` is off.
WEB_OFF_REASON: Final = (
    "web wallpapers are off: turn on Allow web wallpapers in Settings \u2192 Library to play it"
)


def web_off(item: MediaItem, allowed: bool) -> bool:
    """Whether ``item`` would play but for ``allow_web_wallpapers`` being off.

    A web page, and anything whose automatic still the preview setting decides
    (`MediaItem.preview_kind`), such as a preset whose own type says ``web`` over a
    true scene: failing closed, it is a web wallpaper's while they are off. One
    predicate for both, so that while web wallpapers are off nothing a preview
    could have drawn plays, and a library resolved under either preview setting
    compiles to the same document (`config.Settings.covers`).
    """
    return (item.web_page or item.preview_kind) and item.unplayable is None and not allowed


#: What a web wallpaper's tile says in place of a picture while they are off.
WEB_PREVIEW_OFF_NOTE: Final = "Web wallpapers are off"


def previews_off(item: MediaItem, allowed: bool) -> bool:
    """Whether nothing may be made from ``item``'s files: a web page while they are off.

    Wider than `web_off`: it holds for a web page that can't play either, and it
    is about previews, stills and colours, which open the wallpaper's own files.
    Only its ``project.json`` is read then.
    """
    return item.holds_web and not allowed


def holds_web(item: WorkshopItem, installed: Mapping[str, WorkshopItem]) -> bool:
    """Whether ``item``'s folder is, or may be, a web wallpaper's, read from ``project.json`` alone.

    Fails closed, unlike `playback`: a type that reads ``web`` leniently counts
    though it can't play, and a preset counts unless its base is installed and
    plainly not web (a base that is missing, unnamed or itself a preset can't
    be ruled out).
    """
    own = {item.kind, item.declared_type, item.lenient_type}
    if item.kind != PRESET:
        return "web" in own
    if "web" in own:
        return True
    base = installed.get(item.dependency) if item.dependency else None
    if base is None or base.kind == PRESET:
        return True
    return "web" in {base.kind, base.declared_type, base.lenient_type}


def web_files_now(item: MediaItem) -> bool:
    """Whether ``item``'s Workshop folder is, or may be, a web wallpaper's, read again now.

    `previews_off` asks the scan, which is as old as it is: a Steam update can
    turn a scene (or a video, or a preset's base) into a web page, and nothing
    rescans for it. So whatever hands one of a Workshop item's files to ffmpeg
    (`stills.ensure`'s frame of a video included) while web wallpapers are off
    asks this just before. Its `project.json` is
    read as the scan reads it (`_read_project`), and so is a preset's base's,
    where the scan found it (`MediaItem.scene_base_directory`, which may be
    another Steam library's). Both fail closed as `holds_web` does: a type
    that reads ``web`` in any spelling counts, so does a `project.json` that
    can't be read or isn't one, and so does a base that is gone or is itself
    a preset. False for anything the Workshop didn't list
    (`WORKSHOP_PROVIDER`): those are the user's own files.
    """
    if item.provider != WORKSHOP_PROVIDER:
        return False
    # A scene's path is its folder; a video's is its file, in its folder.
    project = _read_project(item.path if item.kind is Kind.SCENE else item.path.parent)
    if project is None or "web" in {project.kind, project.declared_type, project.lenient_type}:
        return True
    if not item.scene_base:
        return False
    base = item.scene_base_directory
    project = _read_project(base) if base is not None else None
    return (
        project is None
        or project.kind == PRESET
        or "web" in {project.kind, project.declared_type, project.lenient_type}
    )


class EngineProjectError(Exception):
    """A `project.json` the service's check would refuse, said as a sentence end."""


#: serde_json's nesting limit (`service/src/workshop.rs`): once the 128th
#: bracket opens, the service stops checking numbers and escapes anywhere
#: after that point in the file (shallow content that follows the deep part
#: included) and only reads the text's structure and the top level. It still
#: reads any depth.
ENGINE_STRICT_DEPTH: Final = 128

#: What a JSON string holds that is not a plain character: a surrogate pair
#: (fine), a lone surrogate escape (the service's parser refuses it), or any
#: other escape (consumed, so `\\ud800` is a backslash and text).
_ESCAPE: Final = re.compile(
    r"\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}"
    r"|(?P<lone>\\u[dD][89a-fA-F][0-9a-fA-F]{2})"
    r"|\\u[0-9a-fA-F]{4}|\\."
)
#: JSON's tokens, matched one at a time at the reader's position (`match`,
#: never a search), so a text that is not JSON fails at once instead of being
#: rescanned from every later position, and the string is possessive, so it
#: is not backtracked into: the whole read is linear in the file.
_JSON_TOKEN: Final = re.compile(
    r"""(?P<space>[ \t\n\r]+)
    |(?P<string>"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*+")
    |(?P<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)
    |(?P<literal>true|false|null)
    |(?P<punctuation>[\[\]{},:])""",
    re.VERBOSE,
)


def _engine_number(text: str) -> None:
    """Refuse a number the service's parser can't hold (it has no infinities)."""
    if math.isinf(float(text)):
        raise EngineProjectError("has a number out of range")


def _engine_unpaired(token: str) -> None:
    """Refuse a string token that holds a lone surrogate escape."""
    if "\\u" in token:
        for escape in _ESCAPE.finditer(token):
            if escape.group("lone"):
                raise EngineProjectError("has a lone surrogate escape")


def _engine_string(token: str) -> str:
    """A JSON string token's text, refusing a lone surrogate escape."""
    _engine_unpaired(token)
    return str(json.loads(token))


def _engine_read(text: str) -> str | None:
    """The top level's `type` of ``text``, read as the service reads a project.

    The grammar all through (no `NaN` or infinity), and, until the 128th
    bracket opens (`ENGINE_STRICT_DEPTH`), every number and escape; after it
    only the top level's own keys, its `type`, and the keys of an object that
    is its `type` (what the service's skip decodes). The last `type` wins, and
    is None unless it is a string. Iterative and linear, so no depth and no
    hostile text is too much for it.
    """
    stack: list[str] = []
    state = "value"
    kind: str | None = None
    key: str | None = None
    strict = True
    match_at = _JSON_TOKEN.match
    position = 0
    end = len(text)
    while position < end:
        match = match_at(text, position)
        if match is None:
            raise EngineProjectError("is not JSON")
        group, token = match.lastgroup, match.group()
        position = match.end()
        if group == "space":
            continue
        if state == "done":
            raise EngineProjectError("is not JSON")
        if state in ("value", "item"):
            if state == "item" and token == "]":
                stack.pop()
                state = "after" if stack else "done"
                continue
            if not stack and token != "{":
                raise EngineProjectError("is not an object")
            top_type = len(stack) == 1 and stack[0] == "{" and key == "type"
            if top_type:
                kind = None
            if group == "string":
                if strict or top_type:
                    value = _engine_string(token)
                    kind = value if top_type else kind
            elif group == "number":
                if strict or top_type:
                    _engine_number(token)
            elif group == "punctuation" and token in "[{":
                stack.append(token)
                if len(stack) >= ENGINE_STRICT_DEPTH:
                    strict = False
                state = "item" if token == "[" else "name"
                continue
            elif group != "literal":
                raise EngineProjectError("is not JSON")
            state = "after"
        elif state in ("name", "key"):
            if token == "}" and state == "name":
                stack.pop()
                state = "after" if stack else "done"
                continue
            if group != "string":
                raise EngineProjectError("is not JSON")
            # The top level's keys, and those of an object that is its `type`: the
            # service reads both as text at any depth (`TopLevelType`, `TypeText`).
            if strict or len(stack) == 1 or (len(stack) == 2 and key == "type"):
                name = _engine_string(token)
                key = name if len(stack) == 1 else key
            state = "colon"
        elif state == "colon":
            if token != ":":
                raise EngineProjectError("is not JSON")
            state = "value"
        else:  # after a value
            if token == ",":
                state = "key" if stack[-1] == "{" else "value"
            elif token == ("}" if stack[-1] == "{" else "]"):
                stack.pop()
                state = "after" if stack else "done"
            else:
                raise EngineProjectError("is not JSON")
    if state != "done":
        raise EngineProjectError("is not JSON")
    return kind


def engine_project_type(directory: Path) -> str:
    """The `type` the service's check reads from ``directory``'s `project.json`.

    The service's `workshop::project_type` (service/src/workshop.rs), and as
    strict: the file a regular file of at most `MAX_PROJECT_BYTES` (never a
    link, a FIFO or a folder), UTF-8 with no replacement, JSON with no `NaN`
    or infinity, no number that overflows and no lone surrogate escape, a
    top-level object whose last `type` is a string, lower-cased in ASCII
    only and nothing trimmed. It is not the scan's reader (`_read_project`),
    which forgives all of that to list what it can. Raises `EngineProjectError`
    for whatever the service would refuse.

    Linear in the file, whatever it holds (`_engine_read`): this runs on the
    GUI's stills thread, and a Workshop item's author wrote the file.
    """
    path = directory / "project.json"
    try:
        raw = file_io.read_regular_bytes(path, MAX_PROJECT_BYTES)
    except OSError as error:
        raise EngineProjectError(f"can\u2019t be read ({error.strerror or error})") from error
    if raw is None:
        raise EngineProjectError("can\u2019t be read (it is missing)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EngineProjectError("is not UTF-8") from error
    kind = _engine_read(text)
    if not isinstance(kind, str):
        raise EngineProjectError("is not a project with a type")
    return kind.translate(_ASCII_LOWER)


def engine_refusal(workshop_id: str, *, allow_web: bool) -> str:
    """Why linux-wallpaperengine must not be given ``workshop_id``.

    Empty when it may. The service's `refusal` (service/src/workshop.rs), as
    strict, for the engines this app starts itself: a scene's still, and the
    legacy Python playback. The two must agree on every item
    (service/tests/fixtures/engine-types/cases.json, run by both).

    It is read every time, web wallpapers allowed or not; ``allow_web`` only
    lets a ``web`` page through. The engine finds ``--bg <id>`` under the
    home folder, not where the scan found the item, and reads its type again
    when it starts, so every folder of `ENGINE_WORKSHOP_FOLDERS` that holds
    it is read now, with `engine_project_type`, not the scan's forgiving
    reader. It may start only when each copy there is a `scene` or a `video`,
    or a web page while ``allow_web``: exactly the spelling ``web``, as the
    service reads it. Any other type, an `application` among them, is
    refused; so is a `project.json` the service would not read, and so is
    ``web`` in any spelling `holds_web` counts while it is off (the lenient
    reading only ever refuses). A copy that can't be looked at or read, and
    an item found in none of them, are refused. The home folder is the
    caller's current one, as in `default_steam_roots`.
    """
    if not is_workshop_id(workshop_id):
        return "its Workshop id can\u2019t be checked"
    # The service's own reading of `HOME` (`workshop::engine_home`): set and
    # absolute. Not `Path.home()`, which would take "" for "/", a relative
    # value as it stands, and the password database when it is unset.
    home_text = os.environ.get("HOME")
    if not home_text or not os.path.isabs(home_text):
        return f"Workshop item {workshop_id} can\u2019t be checked: there is no home folder"
    home = Path(home_text)
    found = False
    for folder in ENGINE_WORKSHOP_FOLDERS:
        directory = home / folder / workshop_id
        try:
            # The engine follows a link to the item's folder, so this does.
            info = directory.stat()
        except OSError as error:
            if is_absent(error):
                continue
            return (
                f"Workshop item {workshop_id} can\u2019t be checked: "
                f"{directory}: {error.strerror or error}"
            )
        if not stat.S_ISDIR(info.st_mode):
            continue
        found = True
        try:
            kind = engine_project_type(directory)
        except EngineProjectError as error:
            if not allow_web and _spelled_web(directory):
                return f"Workshop item {workshop_id} is a web page"
            return f"Workshop item {workshop_id}\u2019s project.json in {directory} {error}"
        if kind == "web":
            if allow_web:
                continue
            return f"Workshop item {workshop_id} is a web page"
        if kind not in ("scene", "video"):
            if not allow_web and _spelled_web(directory):
                return f"Workshop item {workshop_id} is a web page"
            return f"Workshop item {workshop_id} in {directory} is neither a scene nor a video"
    if not found:
        # A Steam library on another drive is one the scan lists and the
        # engine never looks in.
        return (
            f"Workshop item {workshop_id} isn\u2019t where linux-wallpaperengine looks: "
            f"Steam\u2019s, the Flatpak\u2019s and the Snap\u2019s Workshop folders under {home}"
        )
    return ""


def _spelled_web(directory: Path) -> bool:
    """Whether the scan's forgiving reading of ``directory`` calls it a web page.

    Only to word a refusal that is made anyway while web wallpapers are off.
    """
    project = _read_project(directory)
    return project is not None and "web" in {
        project.kind,
        project.declared_type,
        project.lenient_type,
    }


def web_among(ids: Iterable[str], give_up: Callable[[], bool] = lambda: False) -> tuple[bool, bool]:
    """Whether a Workshop item of ``ids`` is a web wallpaper's, and whether one can't be checked.

    Bounded by ``ids``, not by the library: no folder is listed. Each id is
    looked for in every content directory (`content_directories`), and every
    copy's `project.json` is read as the scan reads one (`_read_project`: at
    most `MAX_PROJECT_BYTES`, a regular file, no symbolic link); a preset's
    base is read the same way. For a service with no check of an item's type
    of its own (`Application._web_off_on_disk_at_exit`; review r13f, L2).

    The first answer is what the files confirm: an item's own type says
    ``web`` (in any spelling `holds_web` counts), or a preset's base, installed
    and read, does. The second is every doubt: an id that isn't a Workshop id,
    one installed nowhere this can look (Steam's folder unreadable or not
    mounted, the item gone), a `project.json` that can't be read, a preset
    whose base is missing, unreadable or itself a preset, and all of them once
    ``give_up`` says so, asked before each read. It stops at the first web
    wallpaper it confirms.
    """
    directories = content_directories()
    read: dict[str, tuple[WorkshopItem | None, ...]] = {}

    def copies(identifier: str) -> tuple[WorkshopItem | None, ...] | None:
        """Every installed copy of ``identifier``, None for one that can't be read; or None."""
        if identifier in read:
            return read[identifier]
        found: list[WorkshopItem | None] = []
        for content in directories:
            if give_up():
                return None
            folder = content / identifier
            if _is_folder(folder, follow=False, on_unseen=None):
                found.append(_read_project(folder))
        read[identifier] = tuple(found)
        return read[identifier]

    unchecked = False
    for identifier in ids:
        if not is_workshop_id(identifier):
            unchecked = True
            continue
        items = copies(identifier)
        if items is None:
            return False, True  # given up
        if not items:
            unchecked = True  # installed nowhere this can look
            continue
        for item in items:
            if item is None:
                unchecked = True
                continue
            if "web" in {item.kind, item.declared_type, item.lenient_type}:
                return True, unchecked
            if item.kind != PRESET:
                continue
            if not is_workshop_id(item.dependency):
                unchecked = True
                continue
            bases = copies(item.dependency)
            if bases is None:
                return False, True
            if not bases:
                unchecked = True
            for base in bases:
                if base is None or base.kind == PRESET:
                    unchecked = True
                elif "web" in {base.kind, base.declared_type, base.lenient_type}:
                    return True, unchecked
    return False, unchecked


def why_not_played(item: MediaItem, allow_web: bool) -> str:
    """Why a Wallpaper Engine item is never played here as things are, or empty.

    Its own reason (`MediaItem.unplayable`), or, for a web page while
    ``allow_web`` is off, `WEB_OFF_REASON`. Never handed to a renderer then.
    """
    if item.unplayable is not None:
        return item.unplayable_reason or "playback can\u2019t use it"
    return WEB_OFF_REASON if web_off(item, allow_web) else ""


def web_off_titles(items: Iterable[MediaItem], allowed: bool) -> tuple[str, ...]:
    """The titles of web wallpapers (or presets of them) not played while they're off."""
    return tuple(sorted((item.name for item in items if web_off(item, allowed)), key=str.casefold))


def runs_a_program(items: Iterable[MediaItem]) -> bool:
    """Whether any of ``items`` is, or would play, a program (`NEVER_RUN`)."""
    return any(item.runs_program for item in items)


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


def _collecting() -> tuple[list[tuple[Path, UnseenKind, str]], OnUnseen]:
    """A list and the `OnUnseen` that fills it, to judge what was said before saying it."""
    heard: list[tuple[Path, UnseenKind, str]] = []

    def hear(place: Path, kind: UnseenKind, why: str) -> None:
        heard.append((place, kind, why))

    return heard, hear


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
            if not is_workshop_id(directory.name):
                # Steam names every item's folder by its Workshop id, in ASCII
                # digits; `str.isdigit` would also take "١٢٣". The id reaches
                # the engine's command line, so no such folder is handed to
                # it. A video goes to mpvpaper by its file, as it always did,
                # so one in such a folder is still listed.
                heard, hear = _collecting()
                item = _read_project(directory, hear)
                if item is not None and item.is_video:
                    if item.id not in found:
                        found[item.id] = item
                    continue
                if on_unseen is not None:
                    for place, kind, why in heard:
                        if kind.could_not_look:
                            on_unseen(place, kind, why)
                    on_unseen(directory, UnseenKind.NOT_A_WALLPAPER, NOT_AN_ID)
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
