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

import contextlib
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from wall_in_one import paths

STATE_FILENAME: Final = "displays.json"
BROKEN_SUFFIX: Final = ".broken"

#: A connector name is short. This is a ceiling on damage from a file somebody
#: has been editing, not a limit anybody will meet.
MAX_STATE_BYTES: Final = 64 * 1024
MAX_ENTRIES: Final = 64
MAX_NAME_BYTES: Final = 256


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
    """A connector or playlist name, or ``""`` for anything unusable."""
    if not isinstance(value, str):
        return ""
    flattened = " ".join(value.split())
    if not flattened or len(flattened.encode("utf-8")) > MAX_NAME_BYTES:
        return ""
    return flattened


def _read(path: Path) -> tuple[dict[str, str], str | None]:
    """Stored assignments, plus why the file was passed over."""
    try:
        if path.is_symlink() or not path.is_file():
            return {}, None
        if path.stat().st_size > MAX_STATE_BYTES:
            return {}, f"{path.name} is too large to be a list of displays"
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return {}, f"could not read {path.name}: {error.strerror or error}"
    try:
        document = json.loads(text)
    except ValueError:
        return {}, f"{path.name} is not readable JSON"
    if not isinstance(document, dict):
        return {}, f"{path.name} does not hold a list of displays"

    found: dict[str, str] = {}
    entries = document.get("displays")
    if not isinstance(entries, dict):
        return {}, None
    for key, value in entries.items():
        if len(found) >= MAX_ENTRIES:
            break
        connector, playlist = _clean(key), _clean(value)
        if connector and playlist:
            found[connector] = playlist
    return found, None


def save(assignments: Mapping[str, str], path: Path | None = None) -> Path:
    """Write the assignments, atomically."""
    target = path if path is not None else state_path()
    payload = {"version": 1, "displays": dict(assignments)}
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise DisplayError(
            "local-io", f"could not prepare {target.parent}: {error.strerror or error}"
        ) from error
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise DisplayError(
            "local-io", f"could not write {target}: {error.strerror or error}"
        ) from error
    return target


class Store:
    """Connector to playlist name, and the file it lives in."""

    def __init__(
        self, assignments: Mapping[str, str] | None = None, path: Path | None = None
    ) -> None:
        self._assignments: dict[str, str] = dict(assignments or {})
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
        return len(self._assignments)

    def all(self) -> tuple[tuple[str, str], ...]:
        """Every assignment, by connector, so a listing is stable between runs."""
        return tuple(sorted(self._assignments.items()))

    def playlist_for(self, connector: str) -> str:
        """The playlist ``connector`` should show, or ``""`` for the default.

        Empty is the ordinary answer, not a failure: it means this screen has
        no opinion and follows whatever the app is doing generally.
        """
        return self._assignments.get(_clean(connector), "")

    def assign(self, connector: str, playlist: str) -> None:
        """Point one screen at one playlist."""
        name, wanted = _clean(connector), _clean(playlist)
        if not name:
            raise DisplayError("validation", "that is not a connector name")
        if not wanted:
            raise DisplayError("validation", "that is not a playlist name")
        if len(self._assignments) >= MAX_ENTRIES and name not in self._assignments:
            raise DisplayError("validation", f"no more than {MAX_ENTRIES} screens can be assigned")
        self._assignments[name] = wanted
        self._write()

    def unassign(self, connector: str) -> bool:
        """Let a screen follow the default again. ``False`` if it already did."""
        if self._assignments.pop(_clean(connector), None) is None:
            return False
        self._write()
        return True

    def forget_playlist(self, playlist: str) -> int:
        """Drop every assignment naming ``playlist``, and say how many.

        Called when a playlist is deleted. Without it a screen would keep
        pointing at a name that resolves to nothing, which reads as "this
        screen is broken" rather than "that list is gone".
        """
        wanted = _clean(playlist)
        stale = [key for key, value in self._assignments.items() if value == wanted]
        for key in stale:
            del self._assignments[key]
        if stale:
            self._write()
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

    def _write(self) -> None:
        target = self._path if self._path is not None else state_path()
        if self._fault is not None:
            broken = target.with_name(target.name + BROKEN_SUFFIX)
            with contextlib.suppress(OSError):
                os.replace(target, broken)
            self._fault = None
        save(self._assignments, target)
