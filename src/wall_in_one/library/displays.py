"""Which playlist each screen shows.

The last piece of step 14, and the only part of it that is a *decision* rather
than plumbing: the renderers have always taken a connector, `wallpaper.outputs`
now says what the connectors are, and this says what each of them should show.

Deliberately a map from connector to playlist name rather than to a wallpaper.
A wallpaper pinned to a screen is a screen that never changes again, which is
not what anybody wants from a wallpaper manager; a playlist per screen is
"cityscapes on the big one, something quiet on the laptop" and still rotates.

A connector with no entry falls back to whatever the app is doing generally,
which is what every single-screen setup wants and is also what happens the
moment a monitor is unplugged. Assignments for screens that are not currently
attached are **kept**, not pruned: unplugging a dock at the end of the day
should not silently forget the arrangement, and a stale entry costs one lookup
that misses.

Written through on every change, like the playlists and the favourites, for
the same reason -- a session that ends any way other than the close button
should not lose the arrangement.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Final, TypeVar

from wall_in_one import paths
from wall_in_one.library import state_file

STATE_FILENAME: Final = "displays.json"
BROKEN_SUFFIX: Final = ".broken"
FORMAT_VERSION: Final = 1

#: A connector name is short. This is a ceiling on damage from a file somebody
#: has been editing, not a limit anybody will meet.
MAX_STATE_BYTES: Final = 64 * 1024
MAX_ENTRIES: Final = 64
MAX_NAME_BYTES: Final = 256

_MutationResult = TypeVar("_MutationResult")


class DisplayError(Exception):
    """An assignment could not be stored."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


def state_path() -> Path:
    return paths.app_state_dir() / STATE_FILENAME


def _clean(value: object) -> str:
    """A playlist name, or ``""`` for anything unusable."""
    if not isinstance(value, str):
        return ""
    flattened = " ".join(value.split())
    try:
        encoded = flattened.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    if not flattened or len(encoded) > MAX_NAME_BYTES:
        return ""
    return flattened


def _clean_connector(value: object) -> str:
    """One protocol-addressable connector token, with no whitespace."""
    if not isinstance(value, str):
        return ""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    if (
        not value
        or any(character.isspace() for character in value)
        or len(encoded) > MAX_NAME_BYTES
        or any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        return ""
    return value


def _read(path: Path) -> tuple[dict[str, str], str | None]:
    """Stored assignments, plus why the file was passed over."""
    document, fault = state_file.read_object(
        path, maximum_bytes=MAX_STATE_BYTES, description="display assignments"
    )
    if document is None:
        return {}, fault
    faults = [
        found for found in (state_file.version_fault(path, document, FORMAT_VERSION),) if found
    ]

    found: dict[str, str] = {}
    entries = document.get("displays")
    if not isinstance(entries, dict):
        return {}, f"{path.name} has no display assignments in it"
    if len(entries) > MAX_ENTRIES:
        faults.append(f"{path.name} has more than {MAX_ENTRIES} display assignments")
    malformed = 0
    for key, value in entries.items():
        if len(found) >= MAX_ENTRIES:
            break
        connector, playlist = _clean_connector(key), _clean(value)
        if connector and playlist:
            found[connector] = playlist
        else:
            malformed += 1
    if malformed:
        faults.append(f"{path.name} has {malformed} malformed display assignments")
    return found, state_file.joined_faults(faults)


def save(
    assignments: Mapping[str, str],
    path: Path | None = None,
    *,
    replace_existing: bool = True,
) -> Path:
    """Write the assignments, atomically."""
    target = path if path is not None else state_path()
    payload = {"version": FORMAT_VERSION, "displays": dict(assignments)}
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise DisplayError(
            "local-io", f"could not prepare {target.parent}: {error.strerror or error}"
        ) from error
    try:
        state_file.write_atomic_text(
            target,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            replace_existing=replace_existing,
        )
    except (OSError, UnicodeError) as error:
        detail = getattr(error, "strerror", None) or str(error)
        raise DisplayError("local-io", f"could not write {target}: {detail}") from error
    return target


class Store:
    """Connector to playlist name, and the file it lives in."""

    def __init__(
        self,
        assignments: Mapping[str, str] | None = None,
        path: Path | None = None,
        *,
        _loaded: bool = False,
    ) -> None:
        self._assignments: dict[str, str] = dict(assignments or {})
        self._path = path
        self._fault: str | None = None
        # Direct construction may intentionally seed an absent file. Opening
        # one is a disk snapshot even when the file was absent, so its later
        # mutation must rebase rather than overwrite another process's write.
        self._loaded = _loaded

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        target = path if path is not None else state_path()
        found, fault = _read(target)
        store = cls(found, target, _loaded=True)
        store._fault = fault
        return store

    @property
    def fault(self) -> str | None:
        return self._fault

    def __len__(self) -> int:
        return len(self._assignments)

    def all(self) -> tuple[tuple[str, str], ...]:
        """Every assignment, by connector, so a listing is stable between runs."""
        return tuple(sorted(self._assignments.items()))

    def playlist_for(self, connector: str) -> str:
        """The playlist ``connector`` should show, or ``""`` for the default.

        Empty is the ordinary answer, not a failure: it means this screen has
        no opinion and follows whatever the app is doing generally.
        """
        return self._assignments.get(_clean_connector(connector), "")

    def assign(self, connector: str, playlist: str) -> None:
        """Point one screen at one playlist."""
        name, wanted = _clean_connector(connector), _clean(playlist)
        if not name:
            raise DisplayError("validation", "that is not a connector name")
        if not wanted:
            raise DisplayError("validation", "that is not a playlist name")

        def assign(assignments: dict[str, str]) -> tuple[None, bool]:
            if len(assignments) >= MAX_ENTRIES and name not in assignments:
                raise DisplayError(
                    "validation", f"no more than {MAX_ENTRIES} screens can be assigned"
                )
            changed = assignments.get(name) != wanted
            assignments[name] = wanted
            return None, changed

        self._mutate(assign)

    def unassign(self, connector: str) -> bool:
        """Let a screen follow the default again. ``False`` if it already did."""
        name = _clean_connector(connector)

        def unassign(assignments: dict[str, str]) -> tuple[bool, bool]:
            changed = name in assignments
            assignments.pop(name, None)
            return changed, changed

        return self._mutate(unassign)

    def forget_playlist(self, playlist: str) -> int:
        """Drop every assignment naming ``playlist``, and say how many.

        Called when a playlist is deleted. Without it a screen would keep
        pointing at a name that resolves to nothing, which reads as "this
        screen is broken" rather than "that list is gone".
        """
        wanted = _clean(playlist)

        def forget(assignments: dict[str, str]) -> tuple[int, bool]:
            stale = [key for key, value in assignments.items() if value == wanted]
            for key in stale:
                del assignments[key]
            return len(stale), bool(stale)

        return self._mutate(forget)

    def adopt_worker_forget_playlists(self, playlists: Iterable[str]) -> int:
        """Mirror detached dangling-reference cleanup without another write."""
        removed = frozenset(_clean(playlist) for playlist in playlists)
        stale = [
            connector for connector, playlist in self._assignments.items() if playlist in removed
        ]
        for connector in stale:
            del self._assignments[connector]
        return len(stale)

    def describe(self, attached: Iterable[str] = ()) -> tuple[str, ...]:
        """One line per assignment, marking any screen that is not plugged in.

        Detached screens are listed rather than hidden: the whole reason they
        are kept is so somebody can see the arrangement they set up for a dock
        that is not on the desk right now.
        """
        present = frozenset(attached)
        lines: list[str] = []
        for connector, playlist in self.all():
            missing = "" if not present or connector in present else "  (not attached)"
            lines.append(f"{connector}\t{playlist}{missing}")
        return tuple(lines)

    def _mutate(
        self,
        change: Callable[[dict[str, str]], tuple[_MutationResult, bool]],
    ) -> _MutationResult:
        """Apply one connector mutation to the latest durable assignments."""
        target = self._path if self._path is not None else state_path()
        try:
            with (
                state_file.mutation_lock(target, description="display assignments"),
                state_file.observe(target) as observed,
            ):
                try:
                    target.lstat()
                except FileNotFoundError:
                    present = False
                except OSError as error:
                    raise DisplayError(
                        "local-io",
                        f"could not inspect {target}: {error.strerror or error}",
                    ) from error
                else:
                    present = True

                current, fault = _read(target)
                using_durable = present or self._loaded
                assignments = dict(current) if using_durable else dict(self._assignments)
                if using_durable:
                    # Report the current disk fault even when the requested
                    # semantic change later fails; do not adopt its value
                    # until persistence succeeds or no write is required.
                    self._fault = fault
                    self._loaded = True
                result, changed = change(assignments)
                if changed:
                    if fault is not None:
                        try:
                            state_file.preserve_faulted(target, observed=observed)
                        except OSError as error:
                            raise DisplayError(
                                "local-io",
                                f"could not preserve unreadable {target}: "
                                f"{error.strerror or error}",
                            ) from error
                        save(assignments, target, replace_existing=False)
                    else:
                        save(assignments, target)
                    fault = None

                # Adopt only after persistence, so a failed save leaves the
                # Store on the last state it could truthfully report.
                self._assignments = assignments
                self._fault = fault
                self._loaded = using_durable or changed
                return result
        except DisplayError:
            raise
        except OSError as error:
            raise DisplayError(
                "local-io",
                f"could not safely update display assignments at {target}: "
                f"{error.strerror or error}",
            ) from error
