"""Finding the still that stands behind a video, by convention.

The read half, and the narrow one: given a video, which still already
represents it. `library.pairings` (plural) owns the record, the choice and the
file, and calls in here for the default. `library.stills` is the write half.

When dynamics are paused the app shows a still instead of the video, so every
video wants a still standing behind it. There are four ways one gets there,
tried in this order:

1. a sidecar we wrote, `<video>.wall-in-one.json`, naming the still outright;
2. a path-keyed file in the managed `Automatic Stills` directory, which is
   where generated stills land without same-stem videos colliding;
3. an exact generation-bound relationship adopted during a deployed-profile
   upgrade;
4. a sibling named by convention -- `foo.mp4` pairs with `foo-still.png` or
   plain `foo.png`.

Rule 4 is what the user's own library already does (`snowy-village-still.png`),
so it is not a fallback so much as the common case.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import file_io
from wall_in_one.library.model import IMAGE_EXTENSIONS

#: Written by us next to a video to record which still represents it.
SIDECAR_SUFFIX: Final = ".wall-in-one.json"

#: Everything this app writes into someone's wallpaper root goes under here,
#: so that one directory can be deleted to undo all of it. `providers.download`
#: names the same directory for its own downloads.
MANAGED_PARENT: Final = "Wall-in-One"

#: Directory generated stills are written into, beneath `MANAGED_PARENT`.
AUTOMATIC_STILLS_DIRECTORY: Final = "Automatic Stills"

#: The key a sidecar records the still under.
SIDECAR_STILL_KEY: Final = "still_path"

#: Suffix on a hand-made still, before the extension.
STILL_NAME_SUFFIX: Final = "-still"

#: What Wallpaper Engine calls the picture it ships beside a wallpaper.
#:
#: Deliberately *not* used as a still, and not as the source for one. Measured
#: against the real content: they run from 192x192 to 1080x1080, so a still
#: taken from one puts a thumbnail on a 4K screen and hands Noctalia a
#: thumbnail to derive 72 colour tokens from. They are the right size for a
#: grid tile and the wrong size for a wallpaper, which is the whole difference
#: between `thumbnails` and `stills`.
PREVIEW_STEM: Final = "preview"

#: A sidecar is a few hundred bytes. Anything larger is not one of ours.
MAX_SIDECAR_BYTES: Final = 64 * 1024

# The retired Noctalia plugin used the same sidecar suffix but stored an
# ownership record beside the generated still itself.  Migration keeps those
# bytes in place for rollback, so scans and deletion need one narrow reader for
# that exact predecessor record.  It does not grant ownership to an arbitrary
# image: directory shape, plugin id, kind, exact path and dynamic identity all
# have to agree.
LEGACY_PLUGIN_ID: Final = "goober/wall-in-one"
LEGACY_AUTOMATIC_KIND: Final = "automatic-still"

#: Preference order when several stills could serve. Lossless first, since a
#: still is usually a frame grab.
_STILL_EXTENSION_ORDER: Final[tuple[str, ...]] = (".png", ".webp", ".avif", ".jpg", ".jpeg")


#: A directory entry: ``(st_dev, st_ino)`` of the directory holding it, and its name.
Entry = tuple[int, int, str]


@dataclass(frozen=True, slots=True)
class Exclusion:
    """Files a dry run treats as already deleted (`excluding`).

    A removal deletes directory entries. Every path that reaches a deleted
    entry is gone with it: its own spelling, one through a linked folder or a
    bind mount, a symbolic link to it. A path is matched by what it reaches,
    never by its spelling:

    - a deleted file's identity, ``(st_dev, st_ino)``: a file with one link
      has one entry, the deleted one, so every path to it is gone;
    - for a file with more than one link, the entry a path reaches, ``(st_dev,
      st_ino)`` of the directory holding it plus the name: its other names
      stay, as they do on disk;
    - a deleted entry that isn't there yet (a capture the removal reserves)
      counts as gone too, whatever comes to occupy it.

    Whatever can't be told apart counts as gone, which can only refuse more.
    """

    #: ``(st_dev, st_ino)`` of the deleted regular files that exist.
    identities: frozenset[tuple[int, int]] = frozenset()
    #: Those whose deleted entry couldn't be identified: every name of them goes.
    every_name: frozenset[tuple[int, int]] = frozenset()
    #: The deleted entries, there or not yet.
    entries: frozenset[Entry] = frozenset()
    #: Their names: only a path with one of these can reach one by name.
    names: frozenset[str] = frozenset()

    @classmethod
    def of(cls, paths: Iterable[Path]) -> Exclusion:
        """The entries at ``paths`` (a regular file there, or nothing yet).

        Read on the worker, from the files themselves: ``fstat`` of an
        ``O_PATH`` descriptor that doesn't follow a final symlink, for the
        file and for the directory holding it. Anything else at a path (a
        symlink, a folder) is left alone, as a removal deletes only regular
        files.
        """
        identities: set[tuple[int, int]] = set()
        every_name: set[tuple[int, int]] = set()
        entries: set[Entry] = set()
        for path in paths:
            try:
                there = os.lstat(path)
            except FileNotFoundError:
                there = None
            except OSError, ValueError:
                continue
            if there is not None and not stat.S_ISREG(there.st_mode):
                continue
            entry = _directory_entry(path)
            if entry is not None:
                entries.add(entry)
            found = file_identity(path) if there is not None else None
            if found is not None:
                identities.add(found)
                if entry is None:
                    every_name.add(found)
        return cls(
            frozenset(identities),
            frozenset(every_name),
            frozenset(entries),
            frozenset(name for _device, _inode, name in entries),
        )

    def __bool__(self) -> bool:
        return bool(self.identities or self.entries)

    def covers(self, path: Path, status: os.stat_result) -> bool:
        """Whether ``path``, whose ``stat`` is ``status``, reaches a deleted entry."""
        if path.name in self.names:
            reached = _reached_entry(path)
            if reached is None or reached in self.entries:
                return True
        identity = (status.st_dev, status.st_ino)
        if identity not in self.identities:
            return False
        if identity in self.every_name or status.st_nlink <= 1:
            return True
        reached = _reached_entry(path)
        return reached is None or reached in self.entries

    def removes(self, path: Path) -> bool:
        """Whether ``path`` exists now and would not once the entries go."""
        try:
            status = path.stat()
        except OSError, ValueError:
            return False
        return self.covers(path, status)


def file_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of the regular file at ``path``, or None.

    From the file itself (``fstat`` of an ``O_PATH`` descriptor that does not
    follow a final symlink), the way a removal opens what it deletes.
    """
    try:
        descriptor = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError, ValueError:
        return None
    try:
        status = os.fstat(descriptor)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return (status.st_dev, status.st_ino) if stat.S_ISREG(status.st_mode) else None


def _directory_entry(path: Path) -> Entry | None:
    """The entry ``path`` names, as the removal will delete it.

    The directory holding it from an ``O_PATH | O_NOFOLLOW`` descriptor: a
    removal never goes through a linked directory, so this is the directory
    itself. None when that isn't a directory.
    """
    try:
        descriptor = os.open(path.parent, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError, ValueError:
        return None
    try:
        status = os.fstat(descriptor)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        return None
    return (status.st_dev, status.st_ino, path.name)


def _reached_entry(path: Path) -> Entry | None:
    """The entry a scanned ``path`` reaches, or None when that can't be told.

    A final symlink is followed to the entry it names. The directory is
    identified by what the path reaches, links followed: a path through a
    linked folder or a bind mount reaches the same directory, which has the
    same ``(st_dev, st_ino)`` whatever the spelling.
    """
    try:
        target = Path(os.path.realpath(path)) if path.is_symlink() else path
        directory = os.stat(target.parent)
    except OSError, ValueError:
        return None
    if not stat.S_ISDIR(directory.st_mode):
        return None
    return (directory.st_dev, directory.st_ino, target.name)


#: What a dry run treats as already deleted (`excluding`); None outside one,
#: so everything is as on disk.
_EXCLUDED: ContextVar[Exclusion | None] = ContextVar("wall_in_one_excluded_files", default=None)


@contextmanager
def excluding(exclusion: Exclusion | None) -> Iterator[None]:
    """Within this, ``exclusion``'s entries count as gone (`present`, `is_excluded`).

    For a dry run of a removal (`scan.scan(excluded=...)`): the scanner sees
    the library as the removal would leave it. Context-local, so only the
    scan that entered it is affected.
    """
    token = _EXCLUDED.set(exclusion if exclusion else None)
    try:
        yield
    finally:
        _EXCLUDED.reset(token)


def active_exclusion() -> Exclusion | None:
    """The exclusion in force (`excluding`), or None: an ordinary scan pays nothing."""
    return _EXCLUDED.get()


def is_excluded(path: Path, status: os.stat_result) -> bool:
    """Whether ``path`` (``status``) is a file a dry run treats as gone."""
    exclusion = _EXCLUDED.get()
    return exclusion is not None and exclusion.covers(path, status)


def present(path: Path) -> bool:
    """`Path.is_file`, as every convention here asks it, honoring a dry run's exclusions."""
    if _EXCLUDED.get() is None:
        return path.is_file()
    try:
        status = path.stat()
    except OSError, ValueError:
        return False
    return stat.S_ISREG(status.st_mode) and not is_excluded(path, status)


def _still_extensions() -> tuple[str, ...]:
    """Image extensions, preferred ones first, and never `.gif`.

    A gif pairing a video would itself be played as a video, which defeats the
    point of pausing dynamics.
    """
    preferred = [ext for ext in _STILL_EXTENSION_ORDER if ext in IMAGE_EXTENSIONS]
    rest = sorted(IMAGE_EXTENSIONS - set(preferred) - {".gif"})
    return tuple(preferred + rest)


def read_sidecar(video: Path) -> Path | None:
    """Read `<video>.wall-in-one.json` and return the still it names."""
    sidecar = video.with_name(video.name + SIDECAR_SUFFIX)
    try:
        raw = file_io.read_regular_bytes(sidecar, MAX_SIDECAR_BYTES)
    except OSError:
        return None
    if raw is None:
        return None
    try:
        document = json.loads(raw)
    except ValueError, RecursionError:
        return None
    if not isinstance(document, dict):
        return None
    recorded = document.get(SIDECAR_STILL_KEY)
    if not isinstance(recorded, str) or not recorded:
        return None
    candidate = Path(recorded)
    if not candidate.is_absolute():
        candidate = video.parent / candidate
    return candidate if present(candidate) else None


def legacy_automatic_identity(still: Path) -> str | None:
    """Return a proven predecessor dynamic id for ``still``, or ``None``.

    The v0.8 Luau plugin wrote
    ``<still>.wall-in-one.json`` with ``kind=automatic-still``.  Merely living
    in a directory called Automatic Stills is insufficient: a user file there
    stays an ordinary first-class library item unless its exact adjacent
    record proves the old application created it.
    """
    if (
        still.parent.name != AUTOMATIC_STILLS_DIRECTORY
        or still.parent.parent.name != MANAGED_PARENT
        or not still.is_absolute()
    ):
        return None
    sidecar = still.with_name(still.name + SIDECAR_SUFFIX)
    try:
        raw = file_io.read_regular_bytes(sidecar, MAX_SIDECAR_BYTES)
    except OSError:
        return None
    if raw is None:
        return None
    try:
        document = json.loads(raw)
    except ValueError, RecursionError:
        return None
    if not isinstance(document, dict):
        return None
    dynamic_id = document.get("dynamic_id")
    if (
        document.get("schema") != 1
        or document.get("plugin") != LEGACY_PLUGIN_ID
        or document.get("kind") != LEGACY_AUTOMATIC_KIND
        or document.get("path") != str(still)
        or not isinstance(dynamic_id, str)
        or not dynamic_id
        or any(ord(character) < 32 for character in dynamic_id)
    ):
        return None
    try:
        return dynamic_id if not still.is_symlink() and still.is_file() else None
    except OSError:
        return None


def still_directory(root: Path) -> Path:
    """Where generated stills live under ``root``. Shared with `library.stills`,
    so the half that writes them and the half that finds them cannot drift."""
    return root / MANAGED_PARENT / AUTOMATIC_STILLS_DIRECTORY


def automatic_still_stem(video: Path) -> str:
    """Stable, collision-resistant basename for a video's generated still.

    The absolute byte path is the media identity already used by the stores.
    Keeping the readable ``video-`` prefix while hashing that identity avoids
    two different ``intro.mp4`` files overwriting each other's still.
    """
    identity = os.fsencode(video.absolute())
    return f"video-{hashlib.sha256(identity).hexdigest()[:24]}"


def _automatic_still(video: Path, roots: Iterable[Path]) -> Path | None:
    """Look for a generated still under a managed `Automatic Stills` directory."""
    for root in roots:
        directory = still_directory(root)
        for extension in _still_extensions():
            candidate = directory / (automatic_still_stem(video) + extension)
            if present(candidate):
                return candidate
    return None


def _sibling_still(video: Path) -> Path | None:
    """`foo.mp4` -> `foo-still.png`, else `foo.png`."""
    for stem in (video.stem + STILL_NAME_SUFFIX, video.stem):
        for extension in _still_extensions():
            candidate = video.with_name(stem + extension)
            if candidate != video and present(candidate):
                return candidate
    return None


def preview_beside(video: Path) -> Path | None:
    """A `preview.*` in the same directory, whatever kind of file it is.

    Here for `thumbnails`, which wants a small picture, and not for `stills`,
    which wants a wallpaper. See `PREVIEW_STEM`.
    """
    directory = video.parent
    for extension in (*_still_extensions(), ".gif", ".mp4", ".webm"):
        candidate = directory / f"{PREVIEW_STEM}{extension}"
        if candidate != video and candidate.is_file():
            return candidate
    return None


def scene_still(scene: str, roots: Iterable[Path] = ()) -> Path | None:
    """The captured still for a Workshop scene, named by its id.

    By id rather than by directory, so a reinstall that moves the directory
    still finds the still that was taken before.
    """
    for root in roots:
        directory = still_directory(root)
        for extension in _still_extensions():
            candidate = directory / f"{scene}{extension}"
            if present(candidate):
                return candidate
    return None


def find_still(
    video: Path,
    roots: Iterable[Path] = (),
    *,
    adopted: Path | None = None,
) -> Path | None:
    """Best still for ``video``, or ``None`` if it has none."""
    roots = tuple(roots)
    from_sidecar = read_sidecar(video)
    if from_sidecar is not None:
        return from_sidecar
    generated = _automatic_still(video, roots)
    if generated is not None:
        return generated
    if adopted is not None:
        # The adoption loader already pinned and generation-checked this
        # exact relationship.  Never recreate the retired basename lookup
        # here: an accepted mapping is authority, a matching name is not.
        return adopted
    return _sibling_still(video)
