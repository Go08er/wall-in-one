"""Favourites: the handful of wallpapers worth coming back to.

The library is everything on disk. Favourites are the few of those the user
actually wants in rotation, which is the difference between a wallpaper
manager and a slideshow of six hundred files.

They are a list of paths and nothing else, so they live in a small state file
of their own rather than in `config.Settings`. A settings file is a document
people open and hand-edit; three hundred absolute paths in it would bury the
dozen lines that are worth reading, and every favourite toggled from a tile
menu would rewrite the file somebody was in the middle of editing. State that
the app maintains and the user never types belongs under the state directory,
next to the palette.

No toolkit anywhere in here, the same as `library.filter` and `library.manage`:
which wallpapers are favourites is a question about paths, and answering it
where there is no display is what lets the answer be tested.

Two decisions worth reading before changing any of this.

*A favourite whose file is no longer in the library is kept, not pruned.* The
alternative loses data for the most ordinary reasons imaginable: a root
temporarily removed from the settings, a wallpaper directory on a drive that
was not mounted when the app started, a scan that hit its ceiling. Pruning on
load means the app quietly forgets a list the user built by hand, and it
forgets it precisely when it is least able to tell that anything is wrong.
Keeping the entry costs one unresolved path; the grid simply has no tile for
it, and it comes back the moment the file does. `missing` exists so the user
can be told the difference between "you have no favourites" and "your
favourites are not here right now". Only two things drop an entry: the user
saying so, and the app itself destroying the file through `library.manage`.

*A file that cannot be read is not a reason to fail.* `load` never raises. A
truncated or hand-mangled file degrades to no favourites, because a wallpaper
manager that will not start over its own bookmark list is worse than one that
starts empty. What it does not do is throw the bytes away: the first save
after a fault moves the unreadable file aside instead of replacing it, so
whatever was in there is still recoverable by hand.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import paths

#: The file, under `paths.app_state_dir()`.
STATE_FILENAME: Final = "favourites.json"

#: Bumped only if the shape below ever changes. Read leniently -- an
#: unrecognised version is still a list of paths, and refusing it would throw
#: away favourites over a number.
FORMAT_VERSION: Final = 1

#: A ceiling, so a file that grew a zero on the end cannot be read forever.
MAX_FAVOURITES: Final = 10_000

#: The same idea for the file itself, before it is parsed.
MAX_STATE_BYTES: Final = 4 * 1024 * 1024

#: Where a file we could not parse is moved before it would be overwritten.
BROKEN_SUFFIX: Final = ".broken"


class FavouritesError(Exception):
    """The favourites could not be written, with a machine-readable reason.

    Kinds in use: ``local-io``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


def state_path() -> Path:
    """Where the favourites are stored."""
    return paths.app_state_dir() / STATE_FILENAME


def _readable(candidates: Iterable[object]) -> tuple[Path, ...]:
    """The entries of a stored list that are usable, in the order given.

    Anything that is not an absolute path is dropped rather than repaired: a
    relative path in this file has no directory to be relative to, since the
    process that reads it may be running from anywhere.
    """
    seen: set[Path] = set()
    kept: list[Path] = []
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        path = Path(candidate)
        if not path.is_absolute() or path in seen:
            continue
        seen.add(path)
        kept.append(path)
        if len(kept) >= MAX_FAVOURITES:
            break
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class Favourites:
    """The marked wallpapers, in the order they were marked.

    Insertion order rather than sorted: it is the only order the user has any
    memory of, and the grid sorts the tiles it shows by whatever the sort
    control says anyway.
    """

    entries: tuple[Path, ...] = ()

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[Path]:
        return iter(self.entries)

    def __contains__(self, item: object) -> bool:
        return item in self.entries

    @property
    def paths(self) -> frozenset[Path]:
        """The same set, for the membership tests the grid does per tile."""
        return frozenset(self.entries)

    def with_added(self, path: Path) -> Favourites:
        """This plus ``path``, unchanged if it is already a favourite."""
        if path in self.entries:
            return self
        return Favourites(entries=(*self.entries, path)[:MAX_FAVOURITES])

    def without(self, path: Path) -> Favourites:
        if path not in self.entries:
            return self
        return Favourites(entries=tuple(entry for entry in self.entries if entry != path))

    def missing(self, present: Iterable[Path]) -> tuple[Path, ...]:
        """Favourites that ``present`` does not account for.

        The honest form of the decision in the module docstring: the entries
        are still here, and this is how a caller says so out loud instead of
        letting them look like a shorter list than the user made.
        """
        known = set(present)
        return tuple(entry for entry in self.entries if entry not in known)

    def to_json(self) -> str:
        payload = {
            "version": FORMAT_VERSION,
            "paths": [str(entry) for entry in self.entries],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _read(path: Path) -> tuple[Favourites, str | None]:
    """Parse the file, with a sentence about why it could not be parsed.

    Never raises. The fault is a message for a toast, not a control flow: the
    caller gets working, empty favourites either way.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return Favourites(), None
        if path.stat().st_size > MAX_STATE_BYTES:
            return Favourites(), f"{path.name} is too large to be a list of favourites"
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return Favourites(), f"could not read {path.name}: {error.strerror or error}"

    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return Favourites(), f"{path.name} is not readable, so no favourites were loaded"
    if not isinstance(payload, dict):
        return Favourites(), f"{path.name} is not a favourites file"
    stored = payload.get("paths")
    if not isinstance(stored, list):
        return Favourites(), f"{path.name} has no list of favourites in it"
    return Favourites(entries=_readable(stored)), None


def load(path: Path | None = None) -> Favourites:
    """The stored favourites, or none at all. Never raises."""
    favourites, _fault = _read(path if path is not None else state_path())
    return favourites


def save(favourites: Favourites, path: Path | None = None) -> Path:
    """Write ``favourites`` atomically, and return where they went.

    Temporary file in the same directory, then `os.replace`, exactly as
    `config.save` and `providers.credentials.save_key` do it. A half-written
    list is the one outcome worth engineering against: it would read back as a
    shorter list of favourites, which looks like the app silently dropping
    some rather than like a file that needs attention.
    """
    target = path if path is not None else state_path()
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise FavouritesError(
            "local-io", f"could not create {target.parent}: {error.strerror or error}"
        ) from error

    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(favourites.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise FavouritesError(
            "local-io", f"could not write {target}: {error.strerror or error}"
        ) from error
    return target


class Store:
    """The favourites as the running app holds them: a value and a file.

    Every mutation is written through immediately. The alternative -- saving
    on exit -- loses the lot when the session ends any way other than the
    close button, and starring a wallpaper is exactly the kind of one-second
    decision a user will make and then never think about again.
    """

    def __init__(self, favourites: Favourites | None = None, path: Path | None = None) -> None:
        self._favourites = favourites if favourites is not None else Favourites()
        self._path = path
        self._paths = self._favourites.paths
        self._fault: str | None = None

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        """Load from disk. Always succeeds; ``fault`` says if something was lost."""
        target = path if path is not None else state_path()
        favourites, fault = _read(target)
        store = cls(favourites, target)
        store._fault = fault
        return store

    # -- state -----------------------------------------------------------

    @property
    def favourites(self) -> Favourites:
        return self._favourites

    @property
    def paths(self) -> frozenset[Path]:
        """The membership set the grid and the playlist ask about."""
        return self._paths

    @property
    def fault(self) -> str | None:
        """Why the stored favourites were not loaded, if they were not.

        Worth surfacing once: a user whose list has vanished deserves to be
        told it was unreadable rather than left to conclude the app forgot.
        """
        return self._fault

    def __len__(self) -> int:
        return len(self._favourites)

    def is_favourite(self, path: Path) -> bool:
        return path in self._paths

    def missing(self, present: Iterable[Path]) -> tuple[Path, ...]:
        return self._favourites.missing(present)

    # -- mutation --------------------------------------------------------

    def _commit(self, updated: Favourites) -> None:
        """Adopt ``updated`` and persist it.

        In memory first, then the file, and the file's failure does not roll
        the memory back: the user asked for this and gets it for as long as
        the app is running, while the raised error is what tells them it will
        not outlive the session. Silently refusing the star would be worse.
        """
        self._favourites = updated
        self._paths = updated.paths
        self._write()

    def _write(self) -> None:
        target = self._path if self._path is not None else state_path()
        if self._fault is not None:
            # Do not overwrite bytes we could not understand. They are the
            # user's list, in some form, and a copy costs nothing.
            broken = target.with_name(target.name + BROKEN_SUFFIX)
            # Nothing to move, or nowhere to move it: either way the save below
            # is still the right thing to do.
            with contextlib.suppress(OSError):
                os.replace(target, broken)
            self._fault = None
        save(self._favourites, target)

    def add(self, path: Path) -> bool:
        """Mark ``path``. False if it was already marked."""
        updated = self._favourites.with_added(path)
        if updated is self._favourites:
            return False
        self._commit(updated)
        return True

    def discard(self, path: Path) -> bool:
        """Unmark ``path``. False if it was not marked."""
        updated = self._favourites.without(path)
        if updated is self._favourites:
            return False
        self._commit(updated)
        return True

    def toggle(self, path: Path) -> bool:
        """Flip ``path``, and answer with what it is now."""
        if self.is_favourite(path):
            self.discard(path)
            return False
        self.add(path)
        return True
