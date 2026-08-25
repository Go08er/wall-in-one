"""Small-file reads that cannot follow a link or block on a special file.

Settings, manifests and sidecars are all user-writable inputs.  A convenient
``Path.read_bytes()`` follows symbolic links and can wait forever when the path
has been replaced with a FIFO.  The helpers here make the common contract
explicit: one bounded regular file, opened without following links, and still
the same inode after it is opened.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final


class FileReadError(OSError):
    """A present path was not a safely readable bounded regular file."""


class PathChangedError(OSError):
    """A pathname stopped naming the entry a destructive operation expected.

    ``preserved_path`` is set when the entry moved by the operation could not
    be restored without replacing something which subsequently appeared at
    the source name.  It is deliberately left at that path; losing a private
    quarantine is preferable to deleting an unproven replacement.
    """

    def __init__(self, message: str, *, preserved_path: Path | None = None) -> None:
        super().__init__(message)
        self.preserved_path = preserved_path


PathIdentity = tuple[int, int]

_AT_FDCWD: Final = -100
_RENAME_NOREPLACE: Final = 1
DELETION_CLAIM_PREFIX: Final = ".wall-in-one-removal-"
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2: Any = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename one entry without ever replacing ``destination``.

    Wall-in-One is Linux-only, and ``renameat2(RENAME_NOREPLACE)`` is the one
    kernel operation which closes both halves of the usual check/rename race.
    Failing on an older kernel or libc is safer than falling back to a rename
    which could overwrite another actor's file.
    """
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable on this system")
    result = _RENAMEAT2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(destination),
        )


def path_identity(path: Path) -> PathIdentity:
    """The filesystem identity used by journals and atomic path claims."""
    status = path.lstat()
    return status.st_dev, status.st_ino


def _allowed_entry(status: os.stat_result, *, require_regular: bool) -> bool:
    if require_regular:
        return stat.S_ISREG(status.st_mode)
    # Fault recovery may preserve a symlink or FIFO so a safe state file can
    # replace it, but it must never relocate a directory tree.
    return not stat.S_ISDIR(status.st_mode)


def _restore_after_failed_verification(
    source: Path,
    destination: Path,
) -> Path | None:
    """Restore a moved, unverified entry without replacing a new source.

    ``None`` means restoration succeeded.  Otherwise the returned destination
    still preserves the entry whose identity could not be established.
    """
    try:
        _rename_noreplace(destination, source)
    except OSError:
        return destination if os.path.lexists(destination) else None
    return None


def atomic_move_no_replace(
    source: Path,
    destination: Path,
    *,
    expected_identity: PathIdentity,
    require_regular: bool = True,
) -> None:
    """Atomically move exactly one expected entry to an unused pathname.

    The source is checked before the syscall and, critically, the destination
    is checked after it.  If a replacement wins the gap between those checks,
    that replacement is atomically restored without overwriting a newer source
    entry.  When restoration cannot be done safely it remains preserved at the
    destination and is reported through :class:`PathChangedError`.

    With ``require_regular=False``, non-directory entries such as a faulted
    symlink may be preserved. Directories are never accepted by this helper.
    """
    try:
        before = source.lstat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise OSError(error.errno, f"cannot inspect {source}: {error}") from error
    if (before.st_dev, before.st_ino) != expected_identity:
        raise PathChangedError(f"{source} changed before it could be claimed")
    if not _allowed_entry(before, require_regular=require_regular):
        description = "a regular file" if require_regular else "a non-directory entry"
        raise PathChangedError(f"{source} is not {description}")

    _rename_noreplace(source, destination)
    try:
        moved = destination.lstat()
    except OSError as error:
        preserved = _restore_after_failed_verification(source, destination)
        raise PathChangedError(
            f"could not verify {source} after its atomic claim: {error}",
            preserved_path=preserved,
        ) from error
    if (moved.st_dev, moved.st_ino) != expected_identity or not _allowed_entry(
        moved, require_regular=require_regular
    ):
        preserved = _restore_after_failed_verification(source, destination)
        raise PathChangedError(
            f"{source} changed while it was being claimed",
            preserved_path=preserved,
        )


@dataclass(slots=True)
class ClaimedPath:
    """An expected regular file held at an unpredictable private pathname."""

    original: Path
    path: Path
    identity: PathIdentity
    _directory: Path = field(repr=False)
    _consumed: bool = field(default=False, init=False, repr=False)

    def _verify(self) -> None:
        if self._consumed:
            raise OSError(f"the claim for {self.original} has already been consumed")
        try:
            status = self.path.lstat()
        except OSError as error:
            raise PathChangedError(
                f"the private claim for {self.original} is no longer available",
                preserved_path=self.path if os.path.lexists(self.path) else None,
            ) from error
        if (
            not stat.S_ISREG(status.st_mode)
            or (
                status.st_dev,
                status.st_ino,
            )
            != self.identity
        ):
            raise PathChangedError(
                f"the private claim for {self.original} was replaced",
                preserved_path=self.path,
            )

    def _finish(self) -> None:
        self._consumed = True
        # Another same-user actor may have populated the private directory.
        # Never recursively remove contents which this claim did not make.
        with contextlib.suppress(OSError):
            self._directory.rmdir()

    def discard(self) -> None:
        """Unlink only the verified private claim, never its original name."""
        self._verify()
        self.path.unlink()
        self._finish()

    def restore(self) -> bool:
        """Restore the claim without replacement; leave it preserved on conflict."""
        self._verify()
        try:
            _rename_noreplace(self.path, self.original)
        except FileExistsError:
            return False
        self._finish()
        return True


def claim_for_deletion(
    path: Path,
    *,
    expected_identity: PathIdentity,
    operation_token: str | None = None,
) -> ClaimedPath:
    """Atomically take deletion authority over one expected regular file.

    The claim is a new mode-0700 directory beside the source, so deleting the
    claimed pathname cannot accidentally delete a later replacement installed
    at the public source name.  Callers either :meth:`ClaimedPath.discard` it
    or restore it with no-replace semantics after a pre-commit failure.
    """
    if operation_token is None:
        directory = Path(tempfile.mkdtemp(prefix=".wall-in-one-claim-", dir=path.parent))
    else:
        directory = deletion_claim_directory(path, operation_token)
        directory.mkdir(mode=0o700)
        _require_private_claim_directory(directory)
    claimed = directory / "entry"
    try:
        atomic_move_no_replace(
            path,
            claimed,
            expected_identity=expected_identity,
            require_regular=True,
        )
    except OSError:
        with contextlib.suppress(OSError):
            directory.rmdir()
        raise
    return ClaimedPath(path, claimed, expected_identity, directory)


def deletion_claim_directory(path: Path, operation_token: str) -> Path:
    """The replayable private claim location for one durable removal intent."""
    if len(operation_token) != 32 or any(
        character not in "0123456789abcdef" for character in operation_token
    ):
        raise ValueError("removal operation token must be 32 lowercase hexadecimal characters")
    return path.parent / f"{DELETION_CLAIM_PREFIX}{operation_token}"


def _require_private_claim_directory(directory: Path) -> None:
    try:
        status = directory.lstat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise OSError(error.errno, f"cannot inspect removal claim {directory}: {error}") from error
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise OSError(f"removal claim is not a private directory owned by this user: {directory}")


def recover_deletion_claim(
    path: Path,
    *,
    expected_identity: PathIdentity,
    operation_token: str,
) -> ClaimedPath | None:
    """Recover an exact inode left claimed by process death during deletion.

    The strong journal token makes the location discoverable without scanning
    arbitrary hidden files. An empty claim means death happened before the
    move or after the unlink and is an idempotent no-op. Unexpected contents
    are preserved and fail closed.
    """
    directory = deletion_claim_directory(path, operation_token)
    try:
        _require_private_claim_directory(directory)
    except FileNotFoundError:
        return None
    claimed = directory / "entry"
    try:
        status = claimed.lstat()
    except FileNotFoundError:
        try:
            with os.scandir(directory) as iterator:
                populated = next(iterator, None) is not None
        except OSError as error:
            raise OSError(f"cannot inspect empty removal claim {directory}: {error}") from error
        if populated:
            raise PathChangedError(
                f"removal claim {directory} contains an unexpected entry",
                preserved_path=directory,
            ) from None
        with contextlib.suppress(OSError):
            directory.rmdir()
        return None
    except OSError as error:
        raise OSError(f"cannot inspect removal claim entry {claimed}: {error}") from error
    if (
        not stat.S_ISREG(status.st_mode)
        or (
            status.st_dev,
            status.st_ino,
        )
        != expected_identity
    ):
        raise PathChangedError(
            f"removal claim {claimed} does not contain the journaled file",
            preserved_path=claimed,
        )
    return ClaimedPath(path, claimed, expected_identity, directory)


def discard_regular_if_same(path: Path, *, expected_identity: PathIdentity) -> bool:
    """Best-effort deletion of exactly one known regular directory entry.

    ``True`` means this call removed the claimed inode. Absence, replacement,
    an unlink failure, or an unsafe restore conflict all return ``False`` and
    leave every unverified entry intact.
    """
    try:
        claim = claim_for_deletion(path, expected_identity=expected_identity)
    except OSError:
        return False
    try:
        claim.discard()
    except OSError:
        with contextlib.suppress(OSError):
            claim.restore()
        return False
    return True


def read_regular_bytes(path: Path, maximum_bytes: int) -> bytes | None:
    """Return a small regular file's bytes, or ``None`` when it is absent.

    Symbolic links, directories, devices, sockets and FIFOs are rejected.
    ``O_NONBLOCK`` is intentional even after the ``lstat`` check: replacement
    races must not turn a scan or unattended config compile into an indefinite
    wait.  The opened inode is compared with the inspected one before any
    bytes are consumed.
    """
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise FileReadError(error.errno, f"cannot inspect {path}: {error}") from error

    if not stat.S_ISREG(before.st_mode):
        raise FileReadError(f"{path} is not a regular file")
    if before.st_size > maximum_bytes:
        raise FileReadError(f"{path} exceeds its {maximum_bytes}-byte limit")

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FileReadError(error.errno, f"cannot open {path}: {error}") from error

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FileReadError(f"{path} is not a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise FileReadError(f"{path} changed while it was being opened")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as error:
        raise FileReadError(error.errno, f"cannot read {path}: {error}") from error
    finally:
        os.close(descriptor)

    data = b"".join(chunks)
    if len(data) > maximum_bytes:
        raise FileReadError(f"{path} exceeds its {maximum_bytes}-byte limit")
    return data


def read_regular_prefix(path: Path, count: int) -> bytes | None:
    """Return at most ``count`` bytes from one safely opened regular file.

    Media files are intentionally much larger than the metadata ceiling used
    by :func:`read_regular_bytes`, but a header probe still must not follow a
    symbolic link or block forever on a FIFO substituted at its pathname.
    This keeps the same lstat/open/fstat identity boundary while reading only
    the requested prefix and placing no size restriction on the regular file.
    """
    if count < 0:
        raise ValueError("prefix byte count cannot be negative")
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise FileReadError(error.errno, f"cannot inspect {path}: {error}") from error

    if not stat.S_ISREG(before.st_mode):
        raise FileReadError(f"{path} is not a regular file")

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FileReadError(error.errno, f"cannot open {path}: {error}") from error

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FileReadError(f"{path} is not a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise FileReadError(f"{path} changed while it was being opened")
        data = os.read(descriptor, count)
    except OSError as error:
        raise FileReadError(error.errno, f"cannot read {path}: {error}") from error
    finally:
        os.close(descriptor)
    return data


def read_regular_text(
    path: Path,
    maximum_bytes: int,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> str | None:
    """The text counterpart to :func:`read_regular_bytes`."""
    raw = read_regular_bytes(path, maximum_bytes)
    return None if raw is None else raw.decode(encoding, errors=errors)
