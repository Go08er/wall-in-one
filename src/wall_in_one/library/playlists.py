"""Named lists of wallpapers, so the rotation can be a choice rather than a folder.

`library.playlist`, singular, is only the cursor mechanism: what comes next,
what came before, and shuffle. This module owns the actual playback sources:
named lists somebody made, plus the visible one-entry ``Quick choice`` list.
The session always resolves one of these (or the built-in all-media fallback)
into that cursor; it never maintains a second direct-wallpaper playback path.

The two decisions that shape the file.

*An entry has an identity of its own.* Not the wallpaper's path -- the entry's.
Reordering, and later rebinding an entry to a different wallpaper, both have to
leave it the same entry, or "third in the list" stops meaning anything the
moment anybody edits. It also lets one wallpaper appear twice in a list, which
is a legitimate thing to want and impossible if the path is the key.

*A playlist may name wallpapers that are not here.* Exactly as the favourites
do, and for the same reason: a drive that is not mounted this morning is not
somebody deleting their list. Entries resolve against the library when the
rotation is built, and an entry that resolves to nothing is skipped rather than
dropped.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from wall_in_one import paths
from wall_in_one.library.model import MediaItem

#: The file, under `paths.app_state_dir()`, beside the favourites and pairings.
STATE_FILENAME: Final = "playlists.json"

#: Bumped only if the shape changes. Read leniently.
FORMAT_VERSION: Final = 1

#: Ceilings, so a file that grew a zero cannot be read forever.
MAX_PLAYLISTS: Final = 512
MAX_ENTRIES: Final = 10_000
MAX_STATE_BYTES: Final = 8 * 1024 * 1024

#: A name has to fit in a menu and a dropdown.
MAX_NAME_LENGTH: Final = 120

#: Where a file we could not parse is moved before it would be overwritten.
BROKEN_SUFFIX: Final = ".broken"


class PlaylistError(Exception):
    """A playlist could not be changed, with a machine-readable reason.

    Kinds in use: ``local-io``, ``no-such-playlist``, ``no-such-entry``,
    ``invalid-name``, ``full``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


def drop_position(
    entry_ids: tuple[str, ...], moving: str, anchor: str | None, *, after: bool = False
) -> int:
    """Insertion position for dragging ``moving`` beside ``anchor``.

    This is deliberately independent of GTK. Entry identity, duplicate media,
    and the adjustment caused by removing the moving entry first are model
    concerns and can be tested without a display server.
    """
    if anchor == moving and moving in entry_ids:
        return entry_ids.index(moving)
    remaining = [entry_id for entry_id in entry_ids if entry_id != moving]
    if anchor is None or anchor not in remaining:
        return len(remaining)
    position = remaining.index(anchor)
    return position + 1 if after else position


def drop_slot(row_bounds: tuple[tuple[float, float], ...], pointer_y: float) -> int:
    """The insertion slot nearest a pointer on a vertical sortable list.

    Bounds are ``(top, bottom)`` pairs in visual order. Keeping this decision
    independent of GTK matters: direct signal-handler tests cannot prove that
    a real compositor will deliver the motion events a drag depends on.
    """
    for position, (top, bottom) in enumerate(row_bounds):
        if pointer_y < top + (bottom - top) / 2:
            return position
    return len(row_bounds)


def new_id() -> str:
    """A short opaque identifier.

    Opaque on purpose: an identifier that looks like a name invites code that
    treats it as one, and then renaming breaks the references.
    """
    return secrets.token_hex(8)


def tidy_name(raw: str) -> str:
    """The name as it will be stored, or raise if it is not one.

    Collapsed whitespace, because a name with a newline in it would break the
    row format the control socket prints, and a name that is only spaces looks
    like a playlist that is not there.
    """
    name = " ".join(raw.split())
    if not name:
        raise PlaylistError("invalid-name", "a playlist needs a name")
    if len(name) > MAX_NAME_LENGTH:
        raise PlaylistError("invalid-name", f"a name has to be under {MAX_NAME_LENGTH} characters")
    return name


@dataclass(frozen=True, slots=True)
class Entry:
    """One position in a playlist.

    ``source`` is the wallpaper's path. ``id`` is the entry's own, which is
    what survives reordering.
    """

    id: str
    source: str

    @property
    def path(self) -> Path:
        return Path(self.source)


@dataclass(frozen=True, slots=True)
class Playlist:
    """A named, ordered list of wallpapers."""

    id: str
    name: str
    entries: tuple[Entry, ...] = ()

    def __len__(self) -> int:
        return len(self.entries)

    def with_added(self, source: Path, entry_id: str | None = None) -> Playlist:
        """This plus one entry at the end. Duplicates are allowed on purpose."""
        if len(self.entries) >= MAX_ENTRIES:
            raise PlaylistError("full", f"{self.name} already holds {MAX_ENTRIES} wallpapers")
        entry = Entry(id=entry_id or new_id(), source=str(source))
        return replace(self, entries=(*self.entries, entry))

    def without(self, entry_id: str) -> Playlist:
        kept = tuple(entry for entry in self.entries if entry.id != entry_id)
        if len(kept) == len(self.entries):
            raise PlaylistError("no-such-entry", f"{self.name} has no entry {entry_id}")
        return replace(self, entries=kept)

    def moved(self, entry_id: str, position: int) -> Playlist:
        """The same entries with one moved to ``position``, counting from zero.

        Out-of-range positions clamp rather than raise: a drag past the end of
        a list means the end of the list, and refusing would be pedantry about
        a gesture that has an obvious reading.
        """
        remaining = [entry for entry in self.entries if entry.id != entry_id]
        if len(remaining) == len(self.entries):
            raise PlaylistError("no-such-entry", f"{self.name} has no entry {entry_id}")
        moving = next(entry for entry in self.entries if entry.id == entry_id)
        index = max(0, min(len(remaining), position))
        remaining.insert(index, moving)
        return replace(self, entries=tuple(remaining))

    def resolve(self, library: Iterable[MediaItem]) -> tuple[MediaItem, ...]:
        """The wallpapers this list names, in its order, skipping the absent.

        An entry naming something not in the library is skipped rather than
        dropped -- the file may come back. One wallpaper listed twice is
        returned twice, because somebody put it in twice.
        """
        by_path = {str(item.path): item for item in library}
        found = [by_path.get(entry.source) for entry in self.entries]
        return tuple(item for item in found if item is not None)

    def missing(self, library: Iterable[MediaItem]) -> tuple[str, ...]:
        """Entries the library cannot account for, so a caller can say so."""
        known = {str(item.path) for item in library}
        return tuple(entry.source for entry in self.entries if entry.source not in known)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "entries": [{"id": entry.id, "source": entry.source} for entry in self.entries],
        }


def _entry(raw: object) -> Entry | None:
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("id")
    source = raw.get("source")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    if not isinstance(source, str) or not source.strip():
        return None
    if not Path(source).is_absolute():
        # Nothing to be relative to: this process may run from anywhere.
        return None
    return Entry(id=identifier.strip(), source=source)


def _playlist(raw: object) -> Playlist | None:
    """One stored playlist, or ``None``. One bad list costs only itself."""
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("id")
    name = raw.get("name")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    stored = raw.get("entries")
    entries: list[Entry] = []
    if isinstance(stored, list):
        for item in stored[:MAX_ENTRIES]:
            entry = _entry(item)
            if entry is not None:
                entries.append(entry)
    return Playlist(id=identifier.strip(), name=name.strip(), entries=tuple(entries))


def state_path() -> Path:
    return paths.app_state_dir() / STATE_FILENAME


def _read(path: Path) -> tuple[dict[str, Playlist], str | None]:
    """Every stored playlist by id, plus why the file was passed over."""
    try:
        if path.is_symlink() or not path.is_file():
            return {}, None
        if path.stat().st_size > MAX_STATE_BYTES:
            return {}, f"{path.name} is too large to be a list of playlists"
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return {}, f"could not read {path.name}: {error.strerror or error}"

    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return {}, f"{path.name} is not readable, so no playlists were loaded"
    if not isinstance(payload, dict):
        return {}, f"{path.name} is not a playlists file"
    stored = payload.get("playlists")
    if not isinstance(stored, list):
        return {}, f"{path.name} has no playlists in it"

    found: dict[str, Playlist] = {}
    for raw in stored[:MAX_PLAYLISTS]:
        playlist = _playlist(raw)
        if playlist is not None:
            found[playlist.id] = playlist
    return found, None


def load(path: Path | None = None) -> dict[str, Playlist]:
    """The stored playlists, or none at all. Never raises."""
    found, _fault = _read(path if path is not None else state_path())
    return found


def save(playlists: Mapping[str, Playlist], path: Path | None = None) -> Path:
    """Write them atomically, and return where they went."""
    target = path if path is not None else state_path()
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise PlaylistError(
            "local-io", f"could not create {target.parent}: {error.strerror or error}"
        ) from error

    payload = {
        "version": FORMAT_VERSION,
        "playlists": [playlists[key].to_json() for key in sorted(playlists)],
    }
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise PlaylistError(
            "local-io", f"could not write {target}: {error.strerror or error}"
        ) from error
    return target


class Store:
    """The playlists as the running app holds them: a map and a file.

    Written through on every change, for the reason the favourites are: a
    session that ends any way other than the close button loses the lot.
    """

    def __init__(
        self, playlists: Mapping[str, Playlist] | None = None, path: Path | None = None
    ) -> None:
        self._playlists: dict[str, Playlist] = dict(playlists or {})
        self._path = path
        self._fault: str | None = None

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        target = path if path is not None else state_path()
        found, fault = _read(target)
        store = cls(found, target)
        store._fault = fault
        return store

    @property
    def fault(self) -> str | None:
        return self._fault

    def __len__(self) -> int:
        return len(self._playlists)

    def all(self) -> tuple[Playlist, ...]:
        """Every playlist, by name, so a listing is stable between runs."""
        return tuple(sorted(self._playlists.values(), key=lambda one: one.name.casefold()))

    def get(self, identifier: str) -> Playlist | None:
        return self._playlists.get(identifier)

    def by_name(self, name: str) -> Playlist | None:
        """Look one up the way a person refers to it, case-insensitively."""
        wanted = name.casefold().strip()
        for playlist in self.all():
            if playlist.name.casefold() == wanted:
                return playlist
        return None

    def find(self, reference: str) -> Playlist:
        """A playlist by id or by name, or raise saying which was not found.

        Written against `None` rather than truthiness on purpose: `Playlist`
        defines `__len__`, so an *empty* one is falsy, and `get(x) or
        by_name(x)` silently fell through to the name lookup for every playlist
        nobody had put anything in yet.
        """
        found = self.get(reference)
        if found is None:
            found = self.by_name(reference)
        if found is None:
            raise PlaylistError("no-such-playlist", f"no playlist called {reference!r}")
        return found

    def create(self, name: str, entry_id: str | None = None) -> Playlist:
        """A new, empty playlist. Duplicate names are allowed but discouraged
        by `by_name` answering the first: they are the user's to sort out, and
        refusing one would be the app arguing about their filing."""
        if len(self._playlists) >= MAX_PLAYLISTS:
            raise PlaylistError("full", f"there are already {MAX_PLAYLISTS} playlists")
        playlist = Playlist(id=entry_id or new_id(), name=tidy_name(name))
        return self._commit(playlist)

    def rename(self, identifier: str, name: str) -> Playlist:
        playlist = self.find(identifier)
        return self._commit(replace(playlist, name=tidy_name(name)))

    def delete(self, identifier: str) -> bool:
        playlist = self.get(identifier)
        if playlist is None:
            playlist = self.by_name(identifier)
        if playlist is None:
            return False
        del self._playlists[playlist.id]
        self._write()
        return True

    def add(self, identifier: str, source: Path, entry_id: str | None = None) -> Playlist:
        return self._commit(self.find(identifier).with_added(source, entry_id))

    def set_singleton(
        self,
        identifier: str,
        name: str,
        source: Path,
        *,
        entry_id: str | None = None,
    ) -> Playlist:
        """Create or replace a stable one-entry playlist.

        Media-page activation uses this for the visible ``Quick choice``
        playlist.  It is deliberately a real stored playlist rather than a
        hidden direct-apply path, so there remains exactly one playback model
        and the choice can be inspected or edited on the Playlists page.
        """
        existing = self.get(identifier)
        if existing is None and len(self._playlists) >= MAX_PLAYLISTS:
            raise PlaylistError("full", f"there are already {MAX_PLAYLISTS} playlists")
        entry = Entry(id=entry_id or new_id(), source=str(source))
        playlist = Playlist(id=identifier, name=tidy_name(name), entries=(entry,))
        return self._commit(playlist)

    def remove_entry(self, identifier: str, entry: str) -> Playlist:
        return self._commit(self.find(identifier).without(entry))

    def move_entry(self, identifier: str, entry: str, position: int) -> Playlist:
        return self._commit(self.find(identifier).moved(entry, position))

    def forget_path(self, path: Path) -> bool:
        """Drop every entry naming ``path``, across every playlist.

        For a wallpaper the app itself destroyed. Entries outlive a missing
        file on purpose; not one we unlinked.
        """
        source = str(path)
        changed = False
        for identifier, playlist in list(self._playlists.items()):
            kept = tuple(entry for entry in playlist.entries if entry.source != source)
            if len(kept) != len(playlist.entries):
                self._playlists[identifier] = replace(playlist, entries=kept)
                changed = True
        if changed:
            self._write()
        return changed

    def _commit(self, playlist: Playlist) -> Playlist:
        self._playlists[playlist.id] = playlist
        self._write()
        return playlist

    def _write(self) -> None:
        target = self._path if self._path is not None else state_path()
        if self._fault is not None:
            broken = target.with_name(target.name + BROKEN_SUFFIX)
            with contextlib.suppress(OSError):
                os.replace(target, broken)
            self._fault = None
        save(self._playlists, target)


def rotation(
    playlists: Store, active: str, library: Sequence[MediaItem]
) -> tuple[MediaItem, ...] | None:
    """What ``active`` selects out of ``library``, or ``None`` to use it all.

    ``None`` rather than an empty tuple whenever the answer would be nothing to
    show: no playlist chosen, a playlist that has since been deleted, or one
    whose wallpapers are all on a drive that is not mounted. A wallpaper
    manager that stops changing the wallpaper is a worse answer than one that
    falls back to the library and keeps working -- the same rule the favourites
    follow, for the same reason.
    """
    if not active:
        return None
    playlist = playlists.get(active)
    if playlist is None:
        playlist = playlists.by_name(active)
    if playlist is None:
        return None
    chosen = playlist.resolve(library)
    return chosen or None
