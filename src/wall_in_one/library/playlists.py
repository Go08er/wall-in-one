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

import hashlib
import json
import math
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, TypeVar

from wall_in_one import paths
from wall_in_one.library import state_file
from wall_in_one.library.model import MediaItem

#: The file, under `paths.app_state_dir()`, beside the favourites and pairings.
STATE_FILENAME: Final = "playlists.json"

#: Bumped only if the shape changes. Newer documents are recovered for the UI
#: but faulted so this build cannot silently compile or rewrite them.
FORMAT_VERSION: Final = 1

#: Ceilings, so a file that grew a zero cannot be read forever. People retain
#: the existing 512 authored-list budget; generated playback sources have
#: separate bounded headroom (one global and up to one per supported display).
MAX_AUTHORED_PLAYLISTS: Final = 512
MAX_DISPLAY_QUICK_CHOICES: Final = 64
MAX_PLAYLISTS: Final = MAX_AUTHORED_PLAYLISTS + MAX_DISPLAY_QUICK_CHOICES + 1
MAX_ENTRIES: Final = 10_000
MAX_STATE_BYTES: Final = 8 * 1024 * 1024

#: A name has to fit in a menu and a dropdown.
MAX_NAME_LENGTH: Final = 120

#: Rust's runtime wire contract. Opaque ids normally come from ``new_id``, but
#: tests, migrations and the authoring socket can supply one explicitly.
MAX_IDENTIFIER_BYTES: Final = 256

#: These are generated playback sources, not names available to authoring.
#: Reserve both halves because the runtime resolves references by id *or* name.
#: ``Quick choice`` is materialised through ``set_singleton``; ``All media`` is
#: generated only by the runtime-config compiler.
RESERVED_IDENTITIES: Final = (
    ("all-media", "All media"),
    ("quick-choice", "Quick choice"),
)
DISPLAY_QUICK_CHOICE_ID_PREFIX: Final = "quick-choice:"
DISPLAY_QUICK_CHOICE_NAME_PREFIX: Final = "Quick choice · "
MAX_DISPLAY_CONNECTOR_BYTES: Final = 256

_MutationResult = TypeVar("_MutationResult")

#: Where a file we could not parse is moved before it would be overwritten.
BROKEN_SUFFIX: Final = ".broken"


class PlaylistError(Exception):
    """A playlist could not be changed, with a machine-readable reason.

    Kinds in use: ``local-io``, ``no-such-playlist``, ``no-such-entry``,
    ``invalid-name``, ``identity-conflict``, ``full``.
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


def cubic_bezier(progress: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Map linear ``progress`` through a CSS cubic-bezier timing function.

    CSS curves describe both coordinates parametrically, so evaluating y at
    ``progress`` directly is subtly wrong. Newton iteration finds the curve's
    x parameter quickly; bisection is the dependable fallback for flat slopes.
    """
    if progress <= 0.0:
        return 0.0
    if progress >= 1.0:
        return 1.0

    def coordinate(parameter: float, first: float, second: float) -> float:
        inverse = 1.0 - parameter
        return (
            3.0 * inverse * inverse * parameter * first
            + 3.0 * inverse * parameter * parameter * second
            + parameter * parameter * parameter
        )

    def x_slope(parameter: float) -> float:
        inverse = 1.0 - parameter
        return (
            3.0 * inverse * inverse * x1
            + 6.0 * inverse * parameter * (x2 - x1)
            + 3.0 * parameter * parameter * (1.0 - x2)
        )

    parameter = progress
    for _attempt in range(8):
        difference = coordinate(parameter, x1, x2) - progress
        if abs(difference) < 1e-7:
            return coordinate(parameter, y1, y2)
        slope = x_slope(parameter)
        if abs(slope) < 1e-7:
            break
        candidate = parameter - difference / slope
        if not 0.0 <= candidate <= 1.0:
            break
        parameter = candidate

    lower = 0.0
    upper = 1.0
    for _attempt in range(24):
        parameter = (lower + upper) / 2.0
        x = coordinate(parameter, x1, x2)
        if math.isclose(x, progress, abs_tol=1e-7):
            break
        if x < progress:
            lower = parameter
        else:
            upper = parameter
    return coordinate(parameter, y1, y2)


def live_sort_target(
    row_bounds: tuple[tuple[float, float], ...],
    source_index: int,
    pointer_y: float,
    grab_offset_y: float,
    dragged_height: float,
) -> int:
    """Choose a slot using the lifted row's centre and sibling midpoints.

    ``grab_offset_y`` is deliberately explicit: preserving the initial point
    under the pointer is what prevents a handle grab from centring the row.
    """
    dragged_centre = pointer_y - grab_offset_y + dragged_height / 2.0
    slot = 0
    for index, (top, bottom) in enumerate(row_bounds):
        if index == source_index:
            continue
        if dragged_centre < top + (bottom - top) * 0.5:
            return slot
        slot += 1
    return slot


def live_sort_slot(row_heights: tuple[float, ...], source_index: int, pointer_offset: float) -> int:
    """Final row index selected by a lifted row's centre.

    The pointer offset is measured from the gesture's starting point, so the
    lifted centre begins at its natural position. Comparing it with every
    *other* centre avoids making the vacated row into a phantom drop target.
    """
    source_top = sum(row_heights[:source_index])
    bounds: list[tuple[float, float]] = []
    top = 0.0
    for height in row_heights:
        bounds.append((top, top + height))
        top += height
    return live_sort_target(
        tuple(bounds),
        source_index,
        source_top + pointer_offset,
        0.0,
        row_heights[source_index],
    )


def live_sort_slot_change(
    row_bounds: tuple[tuple[float, float], ...],
    source_index: int,
    pointer_y: float,
    grab_offset_y: float,
    dragged_height: float,
    current_slot: int,
) -> int | None:
    """Return a new slot, or ``None`` while the remembered slot is unchanged.

    This is the prototype's hysteresis: there is no invented dead band around
    a midpoint, just no layout work until the chosen slot actually changes.
    """
    chosen = live_sort_target(row_bounds, source_index, pointer_y, grab_offset_y, dragged_height)
    return None if chosen == current_slot else chosen


def flip_deltas(
    first_positions: tuple[float, ...], last_positions: tuple[float, ...]
) -> tuple[float, ...]:
    """FLIP inversions from current visual y to the new layout y.

    Sub-pixel noise below the prototype's threshold should neither wake an
    animation nor cancel one that is already carrying a row into place.
    """
    if len(first_positions) != len(last_positions):
        raise ValueError("FLIP position lists must have the same length")
    deltas: list[float] = []
    for first, last in zip(first_positions, last_positions, strict=True):
        delta = first - last
        deltas.append(delta if abs(delta) > 0.5 else 0.0)
    return tuple(deltas)


def edge_scroll_speed(pointer_y: float, viewport_height: float) -> float:
    """Auto-scroll pixels per second for a pointer inside a 92 px edge band."""
    edge = 92.0
    # The prototype used 16 px per pointer event, but a frame-clock callback
    # runs even while the pointer is still. A per-second cap keeps that steady
    # scrolling useful without racing through a long playlist.
    maximum = 300.0
    if pointer_y < edge:
        return max(-maximum * (1.0 - pointer_y / edge), -maximum)
    if pointer_y > viewport_height - edge:
        return min(maximum * ((pointer_y - (viewport_height - edge)) / edge), maximum)
    return 0.0


def live_sort_sibling_offsets(
    row_heights: tuple[float, ...], source_index: int, target_slot: int
) -> tuple[float, ...]:
    """Vertical offsets that open ``target_slot`` without changing children.

    A live sort must leave GTK's child sequence untouched for the whole
    gesture. Rows crossed by the lifted one therefore occupy its old space by
    translating exactly one lifted-row height in the opposite direction.
    """
    source_height = row_heights[source_index]
    target = min(max(target_slot, 0), len(row_heights) - 1)
    offsets = [0.0] * len(row_heights)
    if target > source_index:
        for index in range(source_index + 1, target + 1):
            offsets[index] = -source_height
    elif target < source_index:
        for index in range(target, source_index):
            offsets[index] = source_height
    return tuple(offsets)


def live_sort_settle_offset(
    row_heights: tuple[float, ...], source_index: int, target_slot: int
) -> float:
    """Distance from a row's natural top to its chosen live-sort slot."""
    target = min(max(target_slot, 0), len(row_heights) - 1)
    if target > source_index:
        return sum(row_heights[source_index + 1 : target + 1])
    if target < source_index:
        return -sum(row_heights[target:source_index])
    return 0.0


def new_id() -> str:
    """A short opaque identifier.

    Opaque on purpose: an identifier that looks like a name invites code that
    treats it as one, and then renaming breaks the references.
    """
    return secrets.token_hex(8)


def display_quick_choice_id(connector: str) -> str:
    """Deterministic reserved singleton id for one connector."""
    _display_connector(connector)
    digest = hashlib.sha256(connector.encode("utf-8")).hexdigest()[:16]
    return f"{DISPLAY_QUICK_CHOICE_ID_PREFIX}{digest}"


def display_quick_choice_name(connector: str) -> str:
    """Human label paired with :func:`display_quick_choice_id`."""
    identifier = display_quick_choice_id(connector)
    if len(DISPLAY_QUICK_CHOICE_NAME_PREFIX) + len(connector) <= MAX_NAME_LENGTH:
        return f"{DISPLAY_QUICK_CHOICE_NAME_PREFIX}{connector}"
    suffix = f" · {identifier.removeprefix(DISPLAY_QUICK_CHOICE_ID_PREFIX)}"
    available = MAX_NAME_LENGTH - len(DISPLAY_QUICK_CHOICE_NAME_PREFIX) - len(suffix)
    return f"{DISPLAY_QUICK_CHOICE_NAME_PREFIX}{connector[:available]}{suffix}"


def _display_connector(connector: str) -> None:
    try:
        encoded = connector.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PlaylistError("identity-conflict", "display connector must be valid UTF-8") from error
    if not connector:
        raise PlaylistError("identity-conflict", "display connector cannot be empty")
    if len(encoded) > MAX_DISPLAY_CONNECTOR_BYTES:
        raise PlaylistError(
            "identity-conflict",
            f"display connector must be at most {MAX_DISPLAY_CONNECTOR_BYTES} UTF-8 bytes",
        )
    if any(character.isspace() for character in connector):
        raise PlaylistError("identity-conflict", "display connector cannot contain whitespace")
    if any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in connector):
        raise PlaylistError(
            "identity-conflict", "display connector cannot contain control characters"
        )


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


def _fold_identity(value: str) -> str:
    """The comparison used by the human-facing id-or-name lookup surface."""
    return value.casefold()


def _is_generated_playlist_id(identifier: str) -> bool:
    folded = _fold_identity(identifier)
    return folded == _fold_identity("quick-choice") or folded.startswith(
        _fold_identity(DISPLAY_QUICK_CHOICE_ID_PREFIX)
    )


def _identifier(raw: str) -> str:
    """Validate an explicitly supplied opaque playlist id without rewriting it."""
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PlaylistError(
            "identity-conflict", "a playlist id must be valid UTF-8 text; choose another id"
        ) from error
    if not raw.strip():
        raise PlaylistError("identity-conflict", "a playlist id cannot be empty")
    if raw != raw.strip():
        raise PlaylistError(
            "identity-conflict", "a playlist id cannot have leading or trailing whitespace"
        )
    if len(encoded) > MAX_IDENTIFIER_BYTES:
        raise PlaylistError(
            "identity-conflict",
            f"a playlist id must be at most {MAX_IDENTIFIER_BYTES} UTF-8 bytes",
        )
    if any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in raw):
        raise PlaylistError("identity-conflict", "a playlist id cannot contain control characters")
    return raw


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
    payload, fault = state_file.read_object(
        path, maximum_bytes=MAX_STATE_BYTES, description="playlists"
    )
    if payload is None:
        return {}, fault
    faults = [
        found for found in (state_file.version_fault(path, payload, FORMAT_VERSION),) if found
    ]
    stored = payload.get("playlists")
    if not isinstance(stored, list):
        return {}, f"{path.name} has no playlists in it"

    if len(stored) > MAX_PLAYLISTS:
        faults.append(f"{path.name} has more than {MAX_PLAYLISTS} playlists")

    found: dict[str, Playlist] = {}
    malformed = 0
    duplicate_playlists = 0
    duplicate_entries = 0
    for raw in stored[:MAX_PLAYLISTS]:
        playlist = _playlist(raw)
        if playlist is None:
            malformed += 1
            continue
        assert isinstance(raw, dict)
        raw_entries = raw.get("entries")
        if not isinstance(raw_entries, list):
            malformed += 1
        else:
            if len(raw_entries) > MAX_ENTRIES:
                faults.append(
                    f"{path.name} playlist {playlist.id!r} has more than {MAX_ENTRIES} entries"
                )
            parsed_entries = [_entry(item) for item in raw_entries[:MAX_ENTRIES]]
            malformed += sum(entry is None for entry in parsed_entries)
            ids = [entry.id for entry in parsed_entries if entry is not None]
            duplicate_entries += len(ids) - len(set(ids))
        if playlist.id in found:
            duplicate_playlists += 1
        found[playlist.id] = playlist
    if malformed:
        faults.append(f"{path.name} has {malformed} malformed playlist records")
    if duplicate_playlists:
        faults.append(f"{path.name} has {duplicate_playlists} duplicate playlist ids")
    if duplicate_entries:
        faults.append(f"{path.name} has {duplicate_entries} duplicate playlist entry ids")
    return found, state_file.joined_faults(faults)


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
    try:
        state_file.write_atomic_text(
            target, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as error:
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
        self,
        playlists: Mapping[str, Playlist] | None = None,
        path: Path | None = None,
        *,
        _loaded: bool = False,
    ) -> None:
        self._playlists: dict[str, Playlist] = dict(playlists or {})
        self._path = path
        self._fault: str | None = None
        # Direct construction may intentionally seed an absent file. ``open``
        # is always a disk snapshot, including when that snapshot was empty.
        self._loaded = _loaded

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        target = path if path is not None else state_path()
        found, fault = _read(target)
        store = cls(found, target, _loaded=True)
        store._fault = fault
        return store

    def worker_copy(self, *, rebase: bool = False) -> Store:
        """Return a detached copy, optionally reading disk on its worker.

        An unopened, directly constructed Store may intentionally seed an
        absent file, so that value is retained until a file has existed.  An
        opened Store instead treats absence as the current durable snapshot.
        """
        target = self._path if self._path is not None else state_path()
        if rebase:
            try:
                target.lstat()
            except FileNotFoundError:
                if self._loaded:
                    return type(self).open(target)
            except OSError:
                return type(self).open(target)
            else:
                return type(self).open(target)
        copied = type(self)(self._playlists, target, _loaded=self._loaded)
        copied._fault = self._fault
        return copied

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

    @staticmethod
    def _find_in(authored: Mapping[str, Playlist], reference: str) -> Playlist:
        """The transaction-local equivalent of :meth:`find`."""
        found = authored.get(reference)
        if found is None:
            wanted = reference.casefold().strip()
            found = next(
                (
                    playlist
                    for playlist in sorted(authored.values(), key=lambda one: one.name.casefold())
                    if playlist.name.casefold() == wanted
                ),
                None,
            )
        if found is None:
            raise PlaylistError("no-such-playlist", f"no playlist called {reference!r}")
        return found

    def _validate_identity(
        self,
        identifier: str,
        name: str,
        *,
        authored: Mapping[str, Playlist] | None = None,
        replacing: str | None = None,
        generated: bool = False,
        generated_display: bool = False,
    ) -> None:
        """Keep every id-or-name reference unambiguous before it reaches Rust.

        Rust accepts both an opaque id and a display name as a playlist
        reference. Consequently, duplicate names and *cross* collisions (one
        playlist's name equals another playlist's id) are not merely cosmetic:
        they make a schedule resolve according to iteration order. Compare
        without case here because this is the human-facing authoring boundary.
        """
        candidate_id = _fold_identity(identifier)
        candidate_name = _fold_identity(name)
        reserved = {_fold_identity(token) for pair in RESERVED_IDENTITIES for token in pair}
        canonical_generated = (identifier, name) in RESERVED_IDENTITIES
        reserved_display = candidate_id.startswith(
            _fold_identity(DISPLAY_QUICK_CHOICE_ID_PREFIX)
        ) or candidate_name.startswith(_fold_identity(DISPLAY_QUICK_CHOICE_NAME_PREFIX))
        if not (generated and canonical_generated) and not generated_display:
            for label, token in (("id", candidate_id), ("name", candidate_name)):
                if token in reserved or reserved_display:
                    raise PlaylistError(
                        "identity-conflict",
                        f"playlist {label} {identifier if label == 'id' else name!r} is "
                        "reserved for a generated playlist; choose a different name",
                    )

        occupied_playlists = self._playlists if authored is None else authored
        for playlist in occupied_playlists.values():
            if playlist.id == replacing:
                continue
            occupied = {
                _fold_identity(playlist.id): ("id", playlist.id),
                _fold_identity(playlist.name): ("name", playlist.name),
            }
            for label, value, token in (
                ("id", identifier, candidate_id),
                ("name", name, candidate_name),
            ):
                collision = occupied.get(token)
                if collision is None:
                    continue
                other_label, other_value = collision
                raise PlaylistError(
                    "identity-conflict",
                    f"playlist {label} {value!r} conflicts with {other_label} "
                    f"{other_value!r} on {playlist.name!r}; choose a different name",
                )

    def create(self, name: str, entry_id: str | None = None) -> Playlist:
        """A new, empty playlist with an unambiguous id and display name."""
        identifier = new_id() if entry_id is None else _identifier(entry_id)
        tidy = tidy_name(name)

        def create(authored: dict[str, Playlist]) -> tuple[Playlist, bool]:
            authored_count = sum(not _is_generated_playlist_id(found) for found in authored)
            if authored_count >= MAX_AUTHORED_PLAYLISTS:
                raise PlaylistError(
                    "full",
                    f"there are already {MAX_AUTHORED_PLAYLISTS} authored playlists",
                )
            self._validate_identity(identifier, tidy, authored=authored)
            playlist = Playlist(id=identifier, name=tidy)
            authored[playlist.id] = playlist
            return playlist, True

        return self._mutate(create)

    def rename(self, identifier: str, name: str) -> Playlist:
        tidy = tidy_name(name)

        def rename(authored: dict[str, Playlist]) -> tuple[Playlist, bool]:
            playlist = self._find_in(authored, identifier)
            if tidy == playlist.name:
                return playlist, False
            if _fold_identity(playlist.id) in {
                _fold_identity(generated_id) for generated_id, _name in RESERVED_IDENTITIES
            } or _fold_identity(playlist.id).startswith(
                _fold_identity(DISPLAY_QUICK_CHOICE_ID_PREFIX)
            ):
                raise PlaylistError(
                    "identity-conflict",
                    f"{playlist.name} is generated automatically and cannot be renamed",
                )
            self._validate_identity(
                playlist.id,
                tidy,
                authored=authored,
                replacing=playlist.id,
            )
            renamed = replace(playlist, name=tidy)
            authored[playlist.id] = renamed
            return renamed, True

        return self._mutate(rename)

    def delete(self, identifier: str) -> bool:
        def delete(authored: dict[str, Playlist]) -> tuple[bool, bool]:
            try:
                playlist = self._find_in(authored, identifier)
            except PlaylistError as error:
                if error.kind != "no-such-playlist":
                    raise
                return False, False
            del authored[playlist.id]
            return True, True

        return self._mutate(delete)

    def add(self, identifier: str, source: Path, entry_id: str | None = None) -> Playlist:
        def add(authored: dict[str, Playlist]) -> tuple[Playlist, bool]:
            updated = self._find_in(authored, identifier).with_added(source, entry_id)
            authored[updated.id] = updated
            return updated, True

        return self._mutate(add)

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
        identifier = _identifier(identifier)
        tidy = tidy_name(name)

        def set_singleton(authored: dict[str, Playlist]) -> tuple[Playlist, bool]:
            existing = authored.get(identifier)
            generated_global = (identifier, name) in RESERVED_IDENTITIES
            authored_count = sum(not _is_generated_playlist_id(found) for found in authored)
            if existing is None and generated_global and len(authored) >= MAX_PLAYLISTS:
                raise PlaylistError("full", f"there are already {MAX_PLAYLISTS} playlists")
            if (
                existing is None
                and not generated_global
                and authored_count >= MAX_AUTHORED_PLAYLISTS
            ):
                raise PlaylistError(
                    "full",
                    f"there are already {MAX_AUTHORED_PLAYLISTS} authored playlists",
                )
            self._validate_identity(
                identifier,
                tidy,
                authored=authored,
                replacing=identifier,
                generated=True,
            )
            entry = Entry(id=entry_id or new_id(), source=str(source))
            playlist = Playlist(id=identifier, name=tidy, entries=(entry,))
            authored[playlist.id] = playlist
            return playlist, True

        return self._mutate(set_singleton)

    def set_display_singleton(
        self,
        connector: str,
        source: Path,
        *,
        entry_id: str | None = None,
    ) -> Playlist:
        """Create or replace only the generated singleton owned by a display.

        Keeping the connector-to-identity derivation inside the Store closes a
        destructive edge: a caller cannot claim that an arbitrary
        ``quick-choice:<hash>`` playlist is generated and overwrite a user's
        older row with the same id.
        """
        identifier = display_quick_choice_id(connector)
        name = display_quick_choice_name(connector)

        def set_display(authored: dict[str, Playlist]) -> tuple[Playlist, bool]:
            existing = authored.get(identifier)
            if existing is not None and existing.name != name:
                raise PlaylistError(
                    "identity-conflict",
                    f"generated playlist id {identifier!r} is already owned by "
                    f"{existing.name!r}; nothing was overwritten",
                )
            generated_displays = sum(
                _fold_identity(found).startswith(_fold_identity(DISPLAY_QUICK_CHOICE_ID_PREFIX))
                for found in authored
            )
            if existing is None and generated_displays >= MAX_DISPLAY_QUICK_CHOICES:
                raise PlaylistError(
                    "full",
                    f"there are already {MAX_DISPLAY_QUICK_CHOICES} display Quick choices",
                )
            if existing is None and len(authored) >= MAX_PLAYLISTS:
                raise PlaylistError("full", f"there are already {MAX_PLAYLISTS} playlists")
            self._validate_identity(
                identifier,
                name,
                authored=authored,
                replacing=identifier,
                generated_display=True,
            )
            entry = Entry(id=entry_id or new_id(), source=str(source))
            playlist = Playlist(id=identifier, name=name, entries=(entry,))
            authored[playlist.id] = playlist
            return playlist, True

        return self._mutate(set_display)

    def remove_entry(self, identifier: str, entry: str) -> Playlist:
        def remove(authored: dict[str, Playlist]) -> tuple[Playlist, bool]:
            updated = self._find_in(authored, identifier).without(entry)
            authored[updated.id] = updated
            return updated, True

        return self._mutate(remove)

    def move_entry(self, identifier: str, entry: str, position: int) -> Playlist:
        def move(authored: dict[str, Playlist]) -> tuple[Playlist, bool]:
            updated = self._find_in(authored, identifier).moved(entry, position)
            authored[updated.id] = updated
            return updated, True

        return self._mutate(move)

    def move_entry_relative(self, identifier: str, entry: str, step: int) -> Playlist:
        """Move from the latest durable position by one captured gesture step."""

        def move(authored: dict[str, Playlist]) -> tuple[Playlist, bool]:
            current = self._find_in(authored, identifier)
            entry_ids = tuple(candidate.id for candidate in current.entries)
            try:
                position = entry_ids.index(entry)
            except ValueError as error:
                raise PlaylistError(
                    "no-such-entry", f"playlist {current.name!r} has no entry {entry!r}"
                ) from error
            target = min(max(position + step, 0), len(entry_ids) - 1)
            updated = current.moved(entry, target)
            authored[updated.id] = updated
            return updated, target != position

        return self._mutate(move)

    def forget_path(self, path: Path) -> bool:
        """Drop every entry naming ``path``, across every playlist.

        For a wallpaper the app itself destroyed. Entries outlive a missing
        file on purpose; not one we unlinked.
        """
        source = str(path)

        def forget(authored: dict[str, Playlist]) -> tuple[bool, bool]:
            changed = False
            for identifier, playlist in tuple(authored.items()):
                kept = tuple(entry for entry in playlist.entries if entry.source != source)
                if len(kept) != len(playlist.entries):
                    authored[identifier] = replace(playlist, entries=kept)
                    changed = True
            return changed, changed

        return self._mutate(forget)

    def adopt_worker_forget_path(self, path: Path) -> bool:
        """Mirror a durable cleanup while preserving unrelated live edits."""
        source = str(path)
        changed = False
        for identifier, playlist in tuple(self._playlists.items()):
            kept = tuple(entry for entry in playlist.entries if entry.source != source)
            if len(kept) == len(playlist.entries):
                continue
            self._playlists[identifier] = replace(playlist, entries=kept)
            changed = True
        self._fault = None
        self._loaded = True
        return changed

    def adopt_worker_repair(self, expected: str) -> bool:
        """Clear only the same fault a detached worker proved repaired."""
        if self._fault != expected:
            return False
        self._fault = None
        self._loaded = True
        return True

    def _mutate(
        self,
        change: Callable[[dict[str, Playlist]], tuple[_MutationResult, bool]],
    ) -> _MutationResult:
        """Apply one lock-read-change-write transaction over the latest file.

        Atomic replacement alone cannot prevent a stale GUI, control helper,
        or crash replayer from overwriting a valid concurrent edit.  Every
        semantic mutation is therefore recomputed after acquiring the shared
        state-file lock, including removal cleanup across every playlist.
        """
        target = self._path if self._path is not None else state_path()
        try:
            with state_file.mutation_lock(target, description="playlists"):
                try:
                    target.lstat()
                except FileNotFoundError:
                    present = False
                except OSError as error:
                    raise PlaylistError(
                        "local-io",
                        f"could not inspect {target}: {error.strerror or error}",
                    ) from error
                else:
                    present = True

                current, fault = _read(target)
                authored = dict(current) if present or self._loaded else dict(self._playlists)
                result, changed = change(authored)
                if changed:
                    if fault is not None:
                        try:
                            state_file.preserve_faulted(target)
                        except OSError as error:
                            raise PlaylistError(
                                "local-io",
                                f"could not preserve unreadable {target}: "
                                f"{error.strerror or error}",
                            ) from error
                    save(authored, target)
                    fault = None

                # Adopt memory only after the durable write, so an exception
                # leaves the Store on its previous known-good snapshot.
                self._playlists = authored
                self._fault = fault
                self._loaded = True
                return result
        except PlaylistError:
            raise
        except OSError as error:
            raise PlaylistError(
                "local-io",
                f"could not safely update playlists at {target}: {error.strerror or error}",
            ) from error


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
