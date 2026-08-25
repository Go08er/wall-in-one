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
from collections.abc import Iterator
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


def read_object(
    path: Path, *, maximum_bytes: int, description: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one bounded regular JSON object without following symlinks.

    ``(None, None)`` is reserved for an absent file.  Every object that exists
    but is not a readable regular JSON file produces a fault, including a
    replacement race between ``lstat`` and ``open``.
    """
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

    if len(encoded) > maximum_bytes:
        return None, f"{path.name} is too large to be a {description} file"
    try:
        document = json.loads(encoded)
    except UnicodeDecodeError, ValueError, RecursionError:
        return None, f"{path.name} is not readable JSON"
    if not isinstance(document, dict):
        return None, f"{path.name} is not a {description} file"
    return document, None


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
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        # ``fdopen`` owns the descriptor from this point. Transfer ownership
        # before write/flush/fsync can fail so the outer cleanup never closes a
        # recycled numeric descriptor after the handle has already closed it.
        descriptor = -1
        with handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_fingerprint = temporary_pin.fingerprint
        candidate_document = contents.encode("utf-8")
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
