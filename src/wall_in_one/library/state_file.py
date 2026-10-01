"""Shared, fail-closed reading for the app's small authoring documents.

The interactive stores recover to an empty or partial value so the GUI can
still open.  ``fault`` is the other half of that contract: unattended runtime
configuration publication sees it and preserves the last-known-good file.
Keeping the filesystem checks here prevents one store from accidentally
treating a symlink, FIFO, directory, or truncated JSON document as a clean
first run while the others fail closed.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from wall_in_one import file_io

MUTATION_LOCK_TIMEOUT_SECONDS: Final = 5.0
MUTATION_LOCK_POLL_SECONDS: Final = 0.025
# A migration transaction deliberately nests target-specific Store locks on
# the same worker while retaining process-wide exclusion. Reentrancy is only
# same-thread; other workers remain serialized exactly as before.
_MUTATION_GATE = threading.RLock()
_READ_SNAPSHOTS: ContextVar[Mapping[Path, bytes | None] | None] = ContextVar(
    "wall_in_one_state_file_read_snapshots",
    default=None,
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently accepting last-win state."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _validate_json_canonicalization(document: object) -> None:
    (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class StateFileObservation:
    """One pathname generation held across a Store read and fault recovery."""

    path: Path
    identity: file_io.PathIdentity | None
    expected_file_type: int | None
    expected_fingerprint: file_io.FileFingerprint | None
    pinned: file_io.PinnedPath | None = field(repr=False)

    @property
    def present(self) -> bool:
        return self.identity is not None


@contextlib.contextmanager
def read_snapshots(snapshots: Mapping[Path, bytes | None]) -> Iterator[None]:
    """Make exact observed bytes authoritative for nested Store reads.

    The deployed-profile detector pins authoring files before asking each
    Store to parse them. A context-local snapshot lets those ordinary Store
    readers consume the retained bytes (or retained absence) rather than a
    pathname generation swapped between observation and parsing.
    """
    canonical: dict[Path, bytes | None] = {}
    for path, contents in snapshots.items():
        key = path.absolute()
        if key in canonical:
            raise ValueError(f"duplicate state-file read snapshot for {key}")
        if contents is not None and not isinstance(contents, bytes):
            raise TypeError(f"state-file read snapshot for {key} is not bytes")
        canonical[key] = contents
    token = _READ_SNAPSHOTS.set(canonical)
    try:
        yield
    finally:
        _READ_SNAPSHOTS.reset(token)


@contextlib.contextmanager
def observe(path: Path) -> Iterator[StateFileObservation]:
    """Pin the entry a locked Store transaction is about to read.

    The app's mutation lock excludes cooperating processes, but a text editor
    does not take that lock.  Holding this observation through
    :func:`preserve_faulted` binds a later destructive move to the generation
    which preceded the read.  A regular-file fingerprint also detects an
    in-place repair, where the inode remains the same.
    """
    try:
        inspected = path.lstat()
    except FileNotFoundError:
        yield StateFileObservation(path, None, None, None, None)
        return

    identity = inspected.st_dev, inspected.st_ino
    expected_file_type = stat.S_IFMT(inspected.st_mode)
    expected_fingerprint = (
        file_io.file_fingerprint(inspected) if expected_file_type == stat.S_IFREG else None
    )
    if expected_file_type == stat.S_IFDIR:
        # Directories are never moved by fault recovery.  Retaining their
        # observation still lets the caller report the existing state as
        # present before preserve_faulted fails closed.
        yield StateFileObservation(
            path,
            identity,
            expected_file_type,
            expected_fingerprint,
            None,
        )
        return

    try:
        pinned = file_io.pin_path(
            path,
            expected_file_type=expected_file_type,
            expected_identity=identity,
            expected_fingerprint=expected_fingerprint,
        )
    except ValueError as error:
        raise OSError(f"cannot observe unsupported entry type at {path}") from error
    try:
        yield StateFileObservation(
            path,
            identity,
            expected_file_type,
            expected_fingerprint,
            pinned,
        )
    finally:
        pinned.close()


def _decode_object(
    encoded: bytes,
    path: Path,
    *,
    maximum_bytes: int,
    description: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if len(encoded) > maximum_bytes:
        return None, f"{path.name} is too large to be a {description} file"
    try:
        document = json.loads(
            encoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_canonicalization(document)
    except UnicodeError, TypeError, ValueError, RecursionError:
        return None, f"{path.name} is not readable JSON"
    if not isinstance(document, dict):
        return None, f"{path.name} is not a {description} file"
    return document, None


def read_object(
    path: Path, *, maximum_bytes: int, description: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one bounded regular JSON object without following symlinks.

    ``(None, None)`` is reserved for an absent file.  Every object that exists
    but is not a readable regular JSON file produces a fault, including a
    replacement race between ``lstat`` and ``open``.
    """
    snapshots = _READ_SNAPSHOTS.get()
    if snapshots is not None:
        key = path.absolute()
        if key in snapshots:
            encoded = snapshots[key]
            if encoded is None:
                return None, None
            return _decode_object(
                encoded,
                path,
                maximum_bytes=maximum_bytes,
                description=description,
            )
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f"could not inspect {path.name}: {error.strerror or error}"

    if stat.S_ISLNK(before.st_mode):
        return None, f"{path.name} is a symbolic link, not a regular {description} file"
    if not stat.S_ISREG(before.st_mode):
        return None, f"{path.name} is not a regular {description} file"
    if before.st_size > maximum_bytes:
        return None, f"{path.name} is too large to be a {description} file"

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        return None, f"could not read {path.name}: {error.strerror or error}"

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return None, f"{path.name} is not a regular {description} file"
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return None, f"{path.name} changed while it was being read"
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            encoded = handle.read(maximum_bytes + 1)
    except OSError as error:
        return None, f"could not read {path.name}: {error.strerror or error}"
    finally:
        os.close(descriptor)

    return _decode_object(
        encoded,
        path,
        maximum_bytes=maximum_bytes,
        description=description,
    )


def version_fault(path: Path, document: dict[str, Any], expected: int) -> str | None:
    """Explain a present schema marker this build cannot safely rewrite.

    The first revisions of these files did not consistently emit ``version``;
    absence therefore remains the legacy version.  A marker which is present
    is authoritative and must match exactly (``bool`` is not an integer schema
    version despite Python's numeric inheritance).
    """
    version = document.get("version")
    if version is None:
        return None
    if type(version) is int and version == expected:
        return None
    return f"{path.name} has unsupported version {version!r}; expected {expected}"


#: The two kinds of Store fault. A document from a newer build is read-only
#: here: it is shown as far as this build understands it, and every mutation
#: is refused with this kind so the file stays byte-identical and no
#: ``.broken`` copy is made. Anything else this build cannot use is
#: ``unreadable`` and keeps the historical recovery, in which the next write
#: moves the original aside before saving what could be parsed.
NEWER_VERSION: Final = "newer-version"
UNREADABLE: Final = "unreadable"


def newer_version_fault(path: Path, document: Mapping[str, Any], understood: int) -> str | None:
    """Explain a schema marker newer than every version this build understands.

    Only an integer above ``understood`` qualifies: a newer build wrote it,
    and whatever it added must survive this build untouched. Every other
    marker this build does not accept (a string, a bool, zero, an older
    unknown number) is damage rather than the future, and stays an ordinary
    unreadable fault. Callers check this before any shape validation, since a
    newer schema may have moved the very container an older parser expects.
    """
    version = document.get("version")
    if type(version) is int and version > understood:
        return (
            f"{path.name} was saved by a newer version of Wall-in-One "
            f"(unsupported version {version}; this version understands up to {understood})"
        )
    return None


def newer_version_refusal(path: Path) -> str:
    """The sentence every Store uses when it refuses to change a newer file."""
    return (
        f"{path.name} was saved by a newer version of Wall-in-One; "
        "open that version to change it. Nothing was changed."
    )


def fault_kind(fault: str | None, *, newer_version: bool) -> str | None:
    """Classify a Store fault as :data:`NEWER_VERSION` or :data:`UNREADABLE`."""
    if fault is None:
        return None
    return NEWER_VERSION if newer_version else UNREADABLE


@dataclass(frozen=True, slots=True)
class Shape:
    """The fields of one kind of JSON object that this build models.

    Everything outside ``known`` is carried, not interpreted: it is captured
    when a Store reads its file and merged back when the Store writes it.
    ``records`` names fields that hold a list of objects, each found again
    after an edit by its ``identity`` field; ``objects`` names fields that
    hold one nested object. Both kinds of field count as known.
    ``strip_identity`` mirrors a parser that trims the identity it stores, so
    the merge finds the record under the identity it is written back with.
    """

    known: frozenset[str]
    identity: str | None = None
    strip_identity: bool = False
    records: Mapping[str, Shape] = field(default_factory=dict)
    objects: Mapping[str, Shape] = field(default_factory=dict)

    def models(self, key: str) -> bool:
        return key in self.known or key in self.records or key in self.objects

    def identity_of(self, record: Mapping[str, Any]) -> str | None:
        if self.identity is None:
            return None
        value = record.get(self.identity)
        if not isinstance(value, str):
            return None
        return value.strip() if self.strip_identity else value


@dataclass(frozen=True, slots=True)
class Unknown:
    """Fields a document carried that this build does not model.

    A newer build, or a person, may add a key without this build knowing what
    it means. Dropping it on the next unrelated edit loses data silently, so
    it rides through every save instead: at the top level, on each record
    still present (keyed by identity, so reordering and renaming keep it),
    and inside nested objects. A key this build *does* model is never
    carried, so clearing or omitting one of its own fields stays possible.
    """

    fields: Mapping[str, Any] = field(default_factory=dict)
    records: Mapping[str, Mapping[str, Unknown]] = field(default_factory=dict)
    objects: Mapping[str, Unknown] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.fields or self.records or self.objects)


NOTHING_UNKNOWN: Final = Unknown()


def capture_unknown(document: Mapping[str, Any], shape: Shape) -> Unknown:
    """Collect every field of ``document`` that ``shape`` does not model."""
    fields = {key: value for key, value in document.items() if not shape.models(key)}
    records: dict[str, dict[str, Unknown]] = {}
    for name, child in shape.records.items():
        stored = document.get(name)
        if not isinstance(stored, list):
            continue
        seen: set[str] = set()
        found: dict[str, Unknown] = {}
        for raw in stored:
            if not isinstance(raw, dict):
                continue
            identity = child.identity_of(raw)
            if identity is None or identity in seen:
                # The first record with an identity owns it, as a duplicate is
                # already a fault the parser reports.
                continue
            seen.add(identity)
            nested = capture_unknown(raw, child)
            if nested:
                found[identity] = nested
        if found:
            records[name] = found
    objects: dict[str, Unknown] = {}
    for name, child in shape.objects.items():
        stored = document.get(name)
        if isinstance(stored, dict):
            nested = capture_unknown(stored, child)
            if nested:
                objects[name] = nested
    if not (fields or records or objects):
        return NOTHING_UNKNOWN
    return Unknown(fields=fields, records=records, objects=objects)


def merge_unknown(document: dict[str, Any], unknown: Unknown, shape: Shape) -> dict[str, Any]:
    """Put captured fields back into a freshly serialized ``document``.

    Never overwrites a key the serializer wrote. A record or nested object
    the serializer no longer writes takes its unknown fields with it: those
    belonged to something this edit removed.
    """
    for key, value in unknown.fields.items():
        if not shape.models(key):
            document.setdefault(key, value)
    for name, nested in unknown.objects.items():
        child = shape.objects.get(name)
        current = document.get(name)
        if child is not None and isinstance(current, dict):
            merge_unknown(current, nested, child)
    for name, by_identity in unknown.records.items():
        child = shape.records.get(name)
        current = document.get(name)
        if child is None or not isinstance(current, list):
            continue
        for record in current:
            if not isinstance(record, dict):
                continue
            identity = child.identity_of(record)
            if identity is not None and identity in by_identity:
                merge_unknown(record, by_identity[identity], child)
    return document


@dataclass(frozen=True, slots=True)
class Reading[T]:
    """One Store document as parsed: its value and how it was read.

    ``value`` is whatever could be recovered, so the interactive app can still
    show it. ``fault`` keeps its historical meaning for runtime compilation,
    which refuses any faulted store; ``newer_version`` says the fault is a
    document from a newer build, which no mutation may rewrite.
    ``version`` is the format the file is in, for a store that uses
    :class:`FormatVersions` (see :meth:`FormatVersions.declared`): ``None``
    when there is no file, it is unreadable, or it declares no version this
    build reads.
    """

    value: T
    fault: str | None = None
    newer_version: bool = False
    unknown: Unknown = NOTHING_UNKNOWN
    version: int | None = None

    @property
    def fault_kind(self) -> str | None:
        return fault_kind(self.fault, newer_version=self.newer_version)


# -- format-change guards ------------------------------------------------------
#
# Owner-approved for every Release 2 format change (2026-10-01):
#
# 1. Lazy bump on use. A file moves to a newer version only when the data
#    being saved needs it; every other edit keeps writing the version the
#    file already has, so most profiles never leave formats older builds read.
# 2. One-time backup before the first bump. Just before a store first writes
#    a newer version than its file holds, the old bytes are copied to
#    ``<file>.v<old>-backup``; that copy is never overwritten or deleted, and
#    without it the save is refused.
#
# A store adopts both in five steps; schedules.py is the worked example
# (FORMATS, required_version, _read, save and Store._mutate there).
#
# * Declare its versions once. For schedules: v1 is read, v2 is what every
#   supported older build reads and writes, v3 adds rule names::
#
#       FORMATS = state_file.FormatVersions(oldest=1, floor=2, current=3)
#
#   A store whose current version is also its floor today (playlists.json
#   at 1) declares ``FormatVersions(oldest=1, floor=1, current=2)`` when it
#   adds version 2.
# * Say which version the data needs -- "the minimum version that can
#   represent this data" -- as a pure function of the value::
#
#       def required_version(rules: Sequence[Rule]) -> int:
#           if any(rule.name is not None for rule in rules):
#               return 3
#           return FORMATS.floor
#
#   Add the new field to the store's :class:`Shape` so it is modeled rather
#   than carried, and serialize it only when it is used.
# * Record the declared version when reading:
#   ``Reading(..., version=FORMATS.declared(document))``.
# * In the locked mutation, after the change and before anything is moved
#   or written::
#
#       version = FORMATS.to_write(reading.version, required_version(value))
#       if reading.version is not None and FORMATS.is_bump(reading.version, version):
#           state_file.backup_before_bump(path, observed=observed, replaced=reading.version)
#       ...  # then preserve_faulted() if faulted, then save at ``version``
#
#   and turn an ``OSError`` from the backup into the store's own refusal,
#   saying that nothing was changed.
# * Write ``version`` into the saved document instead of a constant, and
#   check every other writer of the file (an importer, a migration) does the
#   same with ``FORMATS.to_write(None, required_version(value))``.


@dataclass(frozen=True, slots=True)
class FormatVersions:
    """The versions of one store's file this build reads and writes.

    ``oldest`` is the oldest version this build reads, and the one a file
    without a ``version`` marker is taken to be. ``floor`` is the oldest it
    writes: a readable file older than that is brought up to ``floor`` on its
    next save, as stores always did, because every supported older build reads
    ``floor`` too. ``current`` is the newest version this build understands;
    a file declaring a newer one is read-only here (:data:`NEWER_VERSION`).

    Writing a version above both ``floor`` and the file's own is a *bump*: an
    older supported build may not read the result, so guard 2 keeps a backup
    first. A file never moves back down. Once bumped and backed up it stays at
    its version even if its data would fit an older one again, so a file does
    not flip between formats as it is edited, and "the backup is the bytes
    before the bump" stays true.
    """

    oldest: int
    floor: int
    current: int

    def __post_init__(self) -> None:
        if not 1 <= self.oldest <= self.floor <= self.current:
            raise ValueError(f"inconsistent format versions {self!r}")

    def declared(self, document: Mapping[str, Any]) -> int | None:
        """The version ``document`` is in, if it is one this build reads.

        A missing marker is :attr:`oldest`. A marker this build does not
        read -- newer, damaged, or not an integer -- is ``None``: a newer file
        is never written, and a damaged one is moved aside as ``.broken``
        (which is its backup) before the store saves what it could parse.
        """
        version = document.get("version")
        if version is None:
            return self.oldest
        if type(version) is int and self.oldest <= version <= self.current:
            return version
        return None

    def to_write(self, on_disk: int | None, required: int) -> int:
        """Guard 1: the version the next save writes.

        ``on_disk`` is :attr:`Reading.version`; ``required`` is the minimum
        version that can represent the data being saved. The result keeps the
        file's version unless the data needs more, and is never below
        :attr:`floor`.
        """
        if not self.floor <= required <= self.current:
            raise ValueError(f"required version {required} is outside {self!r}")
        if on_disk is not None and not self.oldest <= on_disk <= self.current:
            raise ValueError(f"on-disk version {on_disk} is outside {self!r}")
        return max(self.floor, required, on_disk if on_disk is not None else self.floor)

    def is_bump(self, on_disk: int | None, writing: int) -> bool:
        """Whether saving ``writing`` over ``on_disk`` needs guard 2's backup.

        Only a readable file moved above both its own version and
        :attr:`floor` qualifies. Nothing to replace needs no copy, and an
        unreadable file is preserved as ``.broken`` instead.
        """
        return on_disk is not None and writing > on_disk and writing > self.floor


def version_backup_path(path: Path, replaced: int) -> Path:
    """Where guard 2 keeps ``path`` as it was at version ``replaced``."""
    return path.with_name(f"{path.name}.v{replaced}-backup")


def _kept_backup(backup: Path) -> bool:
    """Whether ``backup`` already holds a backup; refuse anything else there."""
    try:
        found = backup.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISREG(found.st_mode):
        return True
    raise OSError(f"{backup} exists but is not a regular file, so no backup can be kept there")


def _observed_bytes(observed: StateFileObservation) -> tuple[bytes, int]:
    """The exact bytes and permission bits of the generation ``observed`` pinned."""
    path = observed.path
    expected = observed.expected_fingerprint
    if not observed.present or observed.expected_file_type != stat.S_IFREG or expected is None:
        raise file_io.PathChangedError(f"cannot back up {path}: it is not a regular file")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or file_io.file_fingerprint(before) != expected:
            raise file_io.PathChangedError(
                f"{path} changed after it was read, so it was not copied"
            )
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    contents = b"".join(chunks)
    if len(contents) != before.st_size or file_io.file_fingerprint(after) != expected:
        raise file_io.PathChangedError(f"{path} changed while it was backed up")
    return contents, stat.S_IMODE(before.st_mode)


def backup_before_bump(path: Path, *, observed: StateFileObservation, replaced: int) -> Path:
    """Guard 2: keep the bytes a store's first format bump replaces.

    Call it with the store's mutation lock held, before the newer version is
    written and before a faulted file is moved aside. The generation
    ``observed`` pinned is copied byte for byte to
    :func:`version_backup_path` (``<file>.v<replaced>-backup``) in the same
    directory, with the original's permission bits, published atomically
    without replacing anything.

    A regular file already at that name is an earlier backup of the same
    version, kept from the first bump, and is never overwritten; the save
    goes ahead. Nothing ever deletes a backup. Returns the backup's path.

    Raises :class:`OSError` when no backup can be kept: something other than
    a regular file holds the name, the file changed since it was observed, or
    the copy could not be written. The caller must then refuse the save
    rather than bump without a backup.
    """
    backup = version_backup_path(path, replaced)
    if _kept_backup(backup):
        return backup
    contents, permissions = _observed_bytes(observed)
    try:
        write_atomic_bytes(backup, contents, replace_existing=False, mode=permissions)
    except FileExistsError:
        # Another writer kept one first. The first copy is the one that counts.
        if _kept_backup(backup):
            return backup
        raise
    return backup


def joined_faults(faults: list[str]) -> str | None:
    """One stable message for a Store's public ``fault`` property."""
    return "; ".join(faults) if faults else None


def fsync_parent(path: Path) -> None:
    """Make a same-directory atomic replacement durable across power loss."""
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_relocated_publication_entry(
    temporary: Path,
    path: Path,
    candidate: file_io.PinnedPath,
    candidate_fingerprint: file_io.FileFingerprint,
    candidate_document: bytes,
) -> str | None:
    """Restore a post-publication replacement relocated to our temporary.

    ``atomic_move_no_replace`` restores an unverified destination to its
    source name.  During no-replace publication that source is our private
    temporary, so the restored entry can be a concurrent manual repair rather
    than our candidate.  The candidate remains pinned while this comparison
    and no-replace restoration run.
    """
    try:
        current = temporary.lstat()
        pinned = candidate.status()
    except FileNotFoundError:
        return None
    except OSError as error:
        return f"could not inspect a relocated concurrent state entry: {error}"
    current_type = stat.S_IFMT(current.st_mode)
    same_candidate = False
    current_fingerprint = (
        file_io.file_fingerprint(current) if current_type == stat.S_IFREG else None
    )
    if (
        current_type == stat.S_IFREG
        and (current.st_dev, current.st_ino) == candidate_fingerprint[:2]
        and (pinned.st_dev, pinned.st_ino) == candidate_fingerprint[:2]
        and current_fingerprint == file_io.file_fingerprint(pinned)
    ):
        chunks: list[bytes] = []
        offset = 0
        remaining = len(candidate_document) + 1
        try:
            while remaining:
                chunk = os.pread(candidate.descriptor, min(remaining, 64 * 1024), offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
            after = candidate.status()
            named_after = temporary.lstat()
        except OSError as error:
            return f"could not inspect a relocated concurrent state entry: {error}"
        same_candidate = (
            b"".join(chunks) == candidate_document
            and stat.S_ISREG(after.st_mode)
            and stat.S_ISREG(named_after.st_mode)
            and (named_after.st_dev, named_after.st_ino) == candidate_fingerprint[:2]
            and file_io.file_fingerprint(after)
            == file_io.file_fingerprint(named_after)
            == current_fingerprint
            and (after.st_dev, after.st_ino) == candidate_fingerprint[:2]
        )
    relocated_description = "publication candidate" if same_candidate else "concurrent entry"
    if current_type == 0 or current_type == stat.S_IFDIR:
        return f"a concurrent directory entry remains preserved at {temporary}"
    current_identity = current.st_dev, current.st_ino
    try:
        file_io.atomic_move_no_replace(
            temporary,
            path,
            expected_identity=current_identity,
            expected_file_type=current_type,
            expected_fingerprint=current_fingerprint,
        )
    except FileExistsError:
        return (
            f"a newer concurrent entry remains at {path}; "
            f"the {relocated_description} remains preserved at {temporary}"
        )
    except OSError as error:
        return f"the {relocated_description} remains preserved at {temporary}: {error}"
    try:
        fsync_parent(path)
    except OSError as error:
        return f"the concurrent entry was restored to {path}, but sync failed: {error}"
    return f"the {relocated_description} was restored to {path}"


def write_atomic_text(
    path: Path,
    contents: str,
    *,
    replace_existing: bool = True,
    mode: int | None = None,
) -> None:
    """Durably replace ``path`` with ``contents`` encoded as UTF-8.

    See :func:`write_atomic_bytes`, which does the work.
    """
    write_atomic_bytes(
        path,
        contents.encode("utf-8"),
        replace_existing=replace_existing,
        mode=mode,
    )


def write_atomic_bytes(
    path: Path,
    contents: bytes,
    *,
    replace_existing: bool = True,
    mode: int | None = None,
) -> None:
    """Durably replace ``path`` from a private same-directory temporary.

    ``mkstemp`` is important here rather than a name derived from the process
    id.  The latter lets a pre-created symbolic link redirect the write, and
    two re-entrant saves in one process select the same temporary.  The file
    descriptor returned here is opened with exclusive creation, so neither is
    possible.  The same-directory replace remains atomic, and syncing both the
    file and its parent preserves the stores' power-loss contract. Fault
    recovery sets ``replace_existing=False`` after moving the unreadable
    generation aside: if a manual repair appears in that newly empty pathname,
    the repair wins instead of being overwritten by the recovered mutation.
    With ``replace_existing=False`` an existing ``path`` raises
    :class:`FileExistsError` and is left exactly as it was.
    ``mode`` sets the new file's permission bits before any byte is written;
    without it the file keeps ``mkstemp``'s private 0600.
    """
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    temporary_status = os.fstat(descriptor)
    temporary_identity = temporary_status.st_dev, temporary_status.st_ino
    temporary_fingerprint: file_io.FileFingerprint | None = None
    temporary_pin: file_io.PinnedPath | None = None
    try:
        temporary_pin = file_io.PinnedPath(
            temporary,
            os.dup(descriptor),
            stat.S_IFREG,
        )
        if mode is not None:
            os.fchmod(descriptor, mode)
        handle = os.fdopen(descriptor, "wb")
        # ``fdopen`` owns the descriptor from this point. Transfer ownership
        # before write/flush/fsync can fail so the outer cleanup never closes a
        # recycled numeric descriptor after the handle has already closed it.
        descriptor = -1
        with handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_fingerprint = temporary_pin.fingerprint
        candidate_document = contents
        if replace_existing:
            os.replace(temporary, path)
        else:
            try:
                file_io.atomic_move_no_replace(
                    temporary,
                    path,
                    expected_identity=temporary_identity,
                    expected_fingerprint=temporary_fingerprint,
                    pinned_source=temporary_pin,
                )
            except file_io.PathChangedError as error:
                restoration = _restore_relocated_publication_entry(
                    temporary,
                    path,
                    temporary_pin,
                    temporary_fingerprint,
                    candidate_document,
                )
                if restoration is None:
                    raise
                raise file_io.PathChangedError(
                    f"{error}; {restoration}",
                    preserved_path=error.preserved_path,
                ) from error
        fsync_parent(path)
    except OSError, UnicodeError:
        if temporary_pin is None:
            # Even descriptor exhaustion must not make cleanup identify the
            # private temporary only by a recyclable inode number.
            temporary_pin = file_io.PinnedPath(temporary, descriptor, stat.S_IFREG)
            descriptor = -1
        if temporary_fingerprint is None:
            with contextlib.suppress(OSError):
                temporary_fingerprint = temporary_pin.fingerprint
        file_io.discard_regular_if_same(
            temporary,
            expected_identity=temporary_identity,
            expected_fingerprint=temporary_fingerprint,
            pinned_source=temporary_pin,
        )
        raise
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if temporary_pin is not None:
            temporary_pin.close()


@contextlib.contextmanager
def mutation_lock(
    target: Path,
    *,
    description: str,
    timeout: float = MUTATION_LOCK_TIMEOUT_SECONDS,
    process_gate: bool = True,
) -> Iterator[None]:
    """Serialize one private state-file rebase/write across processes.

    Atomic replacement prevents torn JSON but does not stop two valid stale
    snapshots replacing each other. The companion lock is never unlinked:
    removing it while another process waits on the inode would let a third
    process lock a new inode and enter beside it.
    """
    if timeout < 0:
        raise OSError(f"{description} mutation lock timeout cannot be negative")
    lock_path = target.absolute().with_name(f".{target.name}.mutation.lock")
    deadline = time.monotonic() + timeout
    remaining = max(0.0, deadline - time.monotonic())
    gate_acquired = False
    if process_gate:
        if not _MUTATION_GATE.acquire(timeout=remaining):
            raise TimeoutError(
                f"timed out after {timeout:g}s waiting for {description} mutation lock {lock_path}"
            )
        gate_acquired = True

    descriptor: int | None = None
    locked = False
    try:
        paths_parent = lock_path.parent
        paths_parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"{description} mutation lock {lock_path} is not a regular file")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(f"{description} mutation lock {lock_path} changed while opening")
        if opened.st_uid != os.getuid() or opened.st_nlink != 1:
            raise OSError(
                f"{description} mutation lock {lock_path} is not a private file owned by this user"
            )
        os.fchmod(descriptor, 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {timeout:g}s waiting for {description} mutation lock "
                        f"{lock_path}"
                    ) from None
                time.sleep(min(MUTATION_LOCK_POLL_SECONDS, remaining))
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(f"{description} mutation lock {lock_path} changed while waiting")
        yield
    finally:
        try:
            if descriptor is not None:
                if locked:
                    with contextlib.suppress(OSError):
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        finally:
            if gate_acquired:
                _MUTATION_GATE.release()


def preserve_faulted(
    path: Path,
    *,
    observed: StateFileObservation | None = None,
) -> Path:
    """Move an unreadable state object aside without replacing any backup.

    The source is moved with the kernel's no-replace operation and verified at
    the destination before the public name is considered released. Directories
    fail safely: the caller must never recursively relocate an unexpected tree
    just to make room for a JSON document. A Store passes the observation held
    across its read so a valid manual repair cannot be mistaken for the bytes
    which produced the fault. Direct callers get the same protection from a
    fresh observation.
    """
    if observed is None:
        with observe(path) as current:
            return preserve_faulted(path, observed=current)
    if observed.path != path:
        raise ValueError("the state-file observation belongs to a different path")
    if not observed.present:
        raise FileNotFoundError(path)
    if observed.expected_file_type == stat.S_IFDIR:
        raise OSError(f"cannot preserve directory at {path}")
    if observed.identity is None or observed.expected_file_type is None or observed.pinned is None:
        raise file_io.PathChangedError(f"cannot prove the faulted generation at {path}")
    for index in range(10_000):
        suffix = ".broken" if index == 0 else f".broken.{index}"
        backup = path.with_name(path.name + suffix)
        try:
            file_io.atomic_move_no_replace(
                path,
                backup,
                expected_identity=observed.identity,
                expected_file_type=observed.expected_file_type,
                expected_fingerprint=observed.expected_fingerprint,
                pinned_source=observed.pinned,
            )
        except FileExistsError:
            continue
        fsync_parent(path)
        return backup
    raise OSError(f"could not preserve {path}: too many recovery backups")
