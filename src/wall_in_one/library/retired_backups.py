"""Remove the copies of a file that earlier versions kept before a format change.

From 0.2.0 until updates became one-way, the first save of ``playlists.json``,
``schedules.json``, ``displays.json`` or ``ui.toml`` in a newer format first
copied the old bytes beside the file as ``<file>.v<old>-backup``, so that an
older version could be handed the file back. Going back is no longer
supported and nothing reads those copies. `remove` takes away each one whose
file beside it still holds everything the copy does, when the app or its
service starts.

It looks for an exact, enumerated set: each name in the one folder it was
ever written to, the app's own state or config folder. Never a pattern, and
never anywhere else. The stores' copies are listed here (`STORE_CANDIDATES`);
``ui.toml``'s one is named by the window preferences' own module
(``remove_retired_backup`` there), which only a start that opens a window
calls, because no other start reads ``ui.toml``.
A copy goes only when all of this holds,
and stays, for a later start, when any of it does not or cannot be checked:

* the name holds a regular file, not a link, owned by this user, with one
  link, and no larger than its store's file may be;
* its contents are a document in exactly the version its name says, which is
  what the copy was when it was made;
* the file it is a copy of is there and reads without a fault, in a version
  this build knows. A damaged, unreadable or missing file, or one a newer
  version saved, keeps its copy: it may be the only good one;
* the copy itself is read in full by that store's own reader (the one the
  file is read with: a repeated key or NaN make it unreadable), and nothing in
  it is missing from the file: every record in it, by its identity (a
  playlist's id, a schedule rule's id, a display's connector), is in the
  file. A record under the same identity with other contents counts as
  there: the copy then holds an older state of something changed since.
  The file reading cleanly now does not show it is the copy's translation
  (it may have been recovered from damage with only what was added since).
  For ``ui.toml`` the file must hold an interface choice.

A copy that stays is listed by Tidy up (read-only, with the reason it stays
and that it may be deleted by hand). The survey asks `why_it_stays`, the one
function `remove` decides by, so a listed copy is one `remove` would keep.
A copy that may go is not listed: the next start removes it.

The checks and the removal happen under that file's own mutation lock, on one
pinned generation of the copy (`file_io.discard_regular_if_same`), so
something else put under the name meanwhile is left where it is. Like every
discard in this application, it leaves an empty record in the folder's
``.wall-in-one-retained``, which Tidy up lists.

A profile without such a copy is only looked at: no lock is taken and nothing
is written.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import file_io
from wall_in_one.library import displays, playlists, schedules, state_file

LOGGER: Final = logging.getLogger(__name__)
#: How long one copy's removal waits for its file's lock. The removal is
#: housekeeping: a busy file just keeps its copy until a later start.
LOCK_TIMEOUT_SECONDS: Final = 1.0


def _json_version(
    formats: state_file.FormatVersions, maximum_bytes: int
) -> Callable[[bytes], int | None]:
    def declared(contents: bytes) -> int | None:
        document, fault = state_file.decode_object(
            contents, "copy", maximum_bytes=maximum_bytes, description="store"
        )
        return formats.declared(document) if document is not None and fault is None else None

    return declared


@dataclass(frozen=True, slots=True)
class Candidate:
    """One copy an earlier version may have kept: its exact name, and of what."""

    #: The copy's whole file name. It is only ever looked for beside `live`.
    name: str
    #: The file it is a copy of, in the app's own state or config folder.
    live: Callable[[], Path]
    #: The version the copy holds: the one its file had before the bump.
    version: int
    #: What the lock on `live` is called when it is busy.
    description: str
    maximum_bytes: int
    #: The version a document declares, or None when it is not that store's.
    declared: Callable[[bytes], int | None]
    #: Whether `live` is there and reads without a fault, in a known version.
    reads_cleanly: Callable[[Path], bool]
    #: The identities of the records in the copy (a playlist's id, a schedule
    #: rule's id, a display's connector), or None when the copy is not read in
    #: full by this store's own reader.
    needed: Callable[[bytes], frozenset[str] | None]
    #: What is missing, as a clause for a person: `{live}` is the file's name and
    #: `{n}` how many of what the copy holds it lacks.
    lack: str
    #: The identities of the records `live` holds, or None unless it reads in full.
    held: Callable[[Path], frozenset[str] | None]
    #: Whether `live` was saved by a newer version (a copy stays then, and says so).
    newer: Callable[[Path], bool]

    def path(self) -> Path:
        return self.live().with_name(self.name)


#: Every copy of a store any version ever made, by name, in the app's state
#: folder. ``schedules.json`` has two: a version 1 file that was first saved
#: with a rule name went straight to 3.
STORE_LACK: Final = "{live} lacks {n} of the records the copy holds"


STORE_CANDIDATES: Final[tuple[Candidate, ...]] = (
    Candidate(
        "playlists.json.v1-backup",
        playlists.state_path,
        1,
        "playlists",
        playlists.MAX_STATE_BYTES,
        _json_version(playlists.FORMATS, playlists.MAX_STATE_BYTES),
        playlists.reads_cleanly,
        playlists.record_ids,
        STORE_LACK,
        playlists.held_record_ids,
        playlists.saved_by_newer_version,
    ),
    Candidate(
        "displays.json.v1-backup",
        displays.state_path,
        1,
        "display assignments",
        displays.MAX_STATE_BYTES,
        _json_version(displays.FORMATS, displays.MAX_STATE_BYTES),
        displays.reads_cleanly,
        displays.record_ids,
        STORE_LACK,
        displays.held_record_ids,
        displays.saved_by_newer_version,
    ),
    Candidate(
        "schedules.json.v1-backup",
        schedules.state_path,
        1,
        "schedule",
        schedules.MAX_STATE_BYTES,
        _json_version(schedules.FORMATS, schedules.MAX_STATE_BYTES),
        schedules.reads_cleanly,
        schedules.record_ids,
        STORE_LACK,
        schedules.held_record_ids,
        schedules.saved_by_newer_version,
    ),
    Candidate(
        "schedules.json.v2-backup",
        schedules.state_path,
        2,
        "schedule",
        schedules.MAX_STATE_BYTES,
        _json_version(schedules.FORMATS, schedules.MAX_STATE_BYTES),
        schedules.reads_cleanly,
        schedules.record_ids,
        STORE_LACK,
        schedules.held_record_ids,
        schedules.saved_by_newer_version,
    ),
)


def _kept(path: Path, reason: str) -> bool:
    LOGGER.info("Kept %s: %s", path, reason)
    return False


#: Said after a reason that holds for a real copy that stays, and only then.
_ADVICE: Final = (
    " An older version kept it so it could be handed the file back; nothing reads it "
    "now. You can delete it yourself if you don't want it."
)


def _not_regular(candidate: Candidate, mode: int) -> str:
    what = "a folder" if stat.S_ISDIR(mode) else "a link" if stat.S_ISLNK(mode) else "not a file"
    return (
        f"{candidate.name} is {what}, so it isn't one of Wall-in-One's old copies "
        "and is left alone."
    )


def _reason_to_stay(
    candidate: Candidate, path: Path, pin: file_io.PinnedPath
) -> tuple[str | None, file_io.FileFingerprint]:
    """Why the pinned copy stays (None when every check passes), and the generation checked.

    Read-only. The one place the stay/go rules are written; `why_it_stays`
    (the survey) and `_remove` (under the lock) both use it. The fingerprint
    returned is the one the bytes were read under, so a removal that is
    pinned to it removes exactly what was checked.

    A reason that begins by showing the name is not such a copy says what it
    is and gives no advice; a copy that stays says why and adds `_ADVICE`.
    """
    live = candidate.live()
    name = candidate.name
    status = pin.status()
    generation = file_io.file_fingerprint(status)
    # These checks show only that the name can't be proven to hold such a copy:
    # a real one may have been hard-linked, truncated or edited since.
    not_a_copy = "so it can't be shown to be one of Wall-in-One's old copies, and is left alone."
    if status.st_uid != os.getuid() or status.st_nlink != 1:
        return f"{name} belongs to another user or has another name, {not_a_copy}", generation
    if status.st_size > candidate.maximum_bytes:
        return f"{name} is larger than a {live.name} may be, {not_a_copy}", generation
    contents = file_io.read_pinned_regular_bytes(
        pin, candidate.maximum_bytes, expected_fingerprint=generation
    )
    if candidate.declared(contents) != candidate.version:
        return (
            f"{name} is not a readable version {candidate.version} {live.name}, {not_a_copy}",
            generation,
        )
    if candidate.newer(live):
        return f"{name} stays: {live.name} was saved by a newer version.{_ADVICE}", generation
    unread = f"{name} stays: {live.name} is missing or could not be read in full.{_ADVICE}"
    if not candidate.reads_cleanly(live):
        return unread, generation
    needed = candidate.needed(contents)
    if needed is None:
        return f"{name} stays: it could not be read in full.{_ADVICE}", generation
    held = candidate.held(live)
    if held is None:
        return unread, generation
    if not needed <= held:
        lack = candidate.lack.format(live=live.name, n=len(needed - held))
        return f"{name} stays: {lack}.{_ADVICE}", generation
    return None, generation


def why_it_stays(candidate: Candidate) -> str | None:
    """Why this copy would stay, or None when it may go (or is not there). Read-only.

    The same decision `remove` makes, by the same code: no lock is taken and
    nothing is written, so Tidy up can list a copy that stays and be right by
    construction. The text names the copy and is meant to be read as it is;
    only a real copy that stays is told it can be deleted by hand. A copy
    that may go is not listed there; the next start removes it. Never raises.
    """
    path = candidate.path()
    try:
        found = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return _unlooked(candidate, error)
    if not stat.S_ISREG(found.st_mode):
        return _not_regular(candidate, found.st_mode)
    try:
        pin = file_io.pin_regular_path(path)
        try:
            return _reason_to_stay(candidate, path, pin)[0]
        finally:
            with contextlib.suppress(OSError):
                pin.close()
    except (OSError, TimeoutError, ValueError) as error:
        return _unlooked(candidate, error)


def _unlooked(candidate: Candidate, error: Exception) -> str:
    detail = file_io.public_text(str(getattr(error, "strerror", None) or error))
    return f"{candidate.name} could not be looked at ({detail}), so it is left alone."


def _remove(candidate: Candidate, path: Path) -> bool:
    try:
        found = path.lstat()
    except FileNotFoundError:
        # The ordinary case. Nothing is locked, made or written for it.
        return False
    if not stat.S_ISREG(found.st_mode):
        return _kept(path, _not_regular(candidate, found.st_mode))
    live = candidate.live()
    with state_file.mutation_lock(
        live, description=candidate.description, timeout=LOCK_TIMEOUT_SECONDS
    ):
        pin = file_io.pin_regular_path(path)
        try:
            reason, generation = _reason_to_stay(candidate, path, pin)
            if reason is not None:
                return _kept(path, reason)
            if not file_io.discard_regular_if_same(
                path,
                expected_identity=generation[:2],
                expected_fingerprint=generation,
                pinned_source=pin,
                logical_retained_parent=path.parent,
            ):
                return _kept(path, "it changed, or could not be taken away")
        finally:
            with contextlib.suppress(OSError):
                pin.close()
    LOGGER.info(
        "Removed %s: nothing in it is missing from %s, which reads in full", path, live.name
    )
    return True


def remove(candidates: Sequence[Candidate] = STORE_CANDIDATES) -> tuple[Path, ...]:
    """Remove each of ``candidates`` that passes every check; say which went.

    Never raises: a copy that could not be looked at, locked or removed (a
    folder that can't be written, a busy file) stays for a later start, and
    a start is never held up or refused over one.
    """
    removed: list[Path] = []
    for candidate in candidates:
        try:
            path = candidate.path()
            if _remove(candidate, path):
                removed.append(path)
        except (OSError, TimeoutError, ValueError) as error:
            LOGGER.info("Kept %s: %s", candidate.name, error)
    return tuple(removed)
