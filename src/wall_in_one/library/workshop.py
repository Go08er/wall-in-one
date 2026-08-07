"""Wallpaper Engine content installed through Steam.

Surveyed against the 49 items on the development machine before any of this
was written, and the survey changed the design:

- **45 of 49 are `video`**, and their entry file is a real `.mp4` sitting in the
  Workshop directory. Those need nothing but mpvpaper, which this app already
  drives. They are ordinary video wallpapers that happen to live under Steam.
- **4 are `scene`**, and their `file` names a `scene.json` that *does not exist*
  -- the content is packed inside `scene.pkg`. Those genuinely require
  `linux-wallpaperengine` to render, and are reported here but not yet playable.
- `type` is spelled both `video` and `Video`, so it is compared case-folded.
  Trusting the casing would have silently hidden eight wallpapers.
- The preview is a `.gif` for 46 of them. A gif is *not* a still -- this app
  plays gifs as video -- so a preview is a good source for a still rather than
  a still itself. It is also 900 KB against a 100 MB video, and it is the frame
  the author chose.

Nothing here writes anything. Steam's directories belong to Steam: items are
`Ownership.USER` so `library.manage` refuses to delete them, and the entry
below is a read-only view of a directory this app does not own.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Wallpaper Engine's Steam application id.
APP_ID: Final = "431960"

#: Where Steam keeps the file that lists every library folder.
_LIBRARY_INDEX: Final = Path("steamapps") / "libraryfolders.vdf"

#: The usual roots. Flatpak's is included because a Flatpak Steam puts the
#: same tree somewhere else entirely and a user is unlikely to know that.
DEFAULT_STEAM_ROOTS: Final[tuple[Path, ...]] = (
    Path.home() / ".local" / "share" / "Steam",
    Path.home() / ".steam" / "steam",
    Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
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


def steam_roots(extra: Sequence[Path] = ()) -> tuple[Path, ...]:
    """Steam installations to look in, in order, without duplicates."""
    seen: dict[Path, None] = {}
    for candidate in (*extra, *DEFAULT_STEAM_ROOTS):
        expanded = Path(candidate).expanduser()
        if expanded.is_dir():
            seen.setdefault(expanded, None)
    return tuple(seen)


def library_folders(root: Path) -> tuple[Path, ...]:
    """Every library folder Steam knows about, including ``root`` itself.

    Steam spreads installations across drives, and Workshop content follows the
    game rather than the main installation -- so looking only where Steam is
    installed finds nothing for anyone with a second disk.
    """
    found: dict[Path, None] = {root: None}
    index = root / _LIBRARY_INDEX
    try:
        if index.is_file() and index.stat().st_size <= MAX_INDEX_BYTES:
            for match in _PATH_LINE.finditer(index.read_text(encoding="utf-8", errors="replace")):
                candidate = Path(match.group(1))
                if candidate.is_dir():
                    found.setdefault(candidate, None)
    except OSError:
        # An unreadable index is not a reason to find nothing: the root itself
        # is still a library folder.
        pass
    return tuple(found)


def content_directories(extra_roots: Sequence[Path] = ()) -> tuple[Path, ...]:
    """Every directory that could hold installed Wallpaper Engine content."""
    found: dict[Path, None] = {}
    for root in steam_roots(extra_roots):
        for folder in library_folders(root):
            content = folder / "steamapps" / "workshop" / "content" / APP_ID
            if content.is_dir():
                found.setdefault(content, None)
    return tuple(found)


def _read_project(directory: Path) -> WorkshopItem | None:
    """One item from its `project.json`, or ``None`` if it is not one.

    Never raises: a Workshop directory is written by Steam and edited by
    whoever likes, and one unreadable item must not cost the other forty-eight.
    """
    project = directory / "project.json"
    try:
        if project.is_symlink() or not project.is_file():
            return None
        if project.stat().st_size > MAX_PROJECT_BYTES:
            return None
        raw = project.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None

    kind = document.get("type")
    title = document.get("title")
    entry = _beside(directory, document.get("file"))
    preview = _beside(directory, document.get("preview"))
    return WorkshopItem(
        id=directory.name,
        directory=directory,
        # Case-folded: the real content spells it both `video` and `Video`, and
        # trusting the casing hid eight wallpapers out of forty-nine.
        kind=kind.strip().casefold() if isinstance(kind, str) else "",
        title=title.strip() if isinstance(title, str) and title.strip() else directory.name,
        entry=entry,
        preview=preview,
    )


def _beside(directory: Path, name: object) -> Path | None:
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
    return candidate if candidate.is_file() else None


def scan(extra_roots: Sequence[Path] = ()) -> tuple[WorkshopItem, ...]:
    """Every installed Wallpaper Engine wallpaper, by id.

    Bounded and quiet: a Steam that is not installed, a Workshop directory that
    is not there, and an item somebody has been editing all produce fewer
    results rather than an error.
    """
    found: dict[str, WorkshopItem] = {}
    for content in content_directories(extra_roots):
        try:
            entries = sorted(content.iterdir())
        except OSError:
            continue
        for directory in entries:
            if len(found) >= MAX_ITEMS:
                return tuple(found.values())
            try:
                if directory.is_symlink() or not directory.is_dir():
                    continue
            except OSError:
                continue
            item = _read_project(directory)
            if item is not None and item.id not in found:
                found[item.id] = item
    return tuple(found.values())


def videos(items: Iterable[WorkshopItem]) -> tuple[WorkshopItem, ...]:
    """The ones this app can already play, which is most of them."""
    return tuple(item for item in items if item.is_video)


def unplayable(items: Iterable[WorkshopItem]) -> tuple[WorkshopItem, ...]:
    """The ones that need a renderer this app does not have yet.

    Reported rather than hidden. A scene silently missing from the library
    looks like the app failing to find it, which is a different bug from the
    one that is actually there.
    """
    return tuple(item for item in items if not item.is_video)
