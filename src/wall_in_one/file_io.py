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
import hashlib
import os
import secrets
import stat
import sys
import tempfile
from collections.abc import Iterator
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
# A persisted regular-file generation.  Device/inode alone are not a
# generation identifier: after the last reference closes, a filesystem may
# immediately recycle both for a new file.  The timestamp fields cannot be
# chosen by an ordinary pathname replacement (ctime in particular), while a
# live O_PATH descriptor below prevents recycling altogether.
FileFingerprint = tuple[int, int, int, int, int]
_RenameInvariantFingerprint = tuple[int, int, int, int]

_AT_FDCWD: Final = -100
_RENAME_NOREPLACE: Final = 1
DELETION_CLAIM_PREFIX: Final = ".wall-in-one-removal-"
RETAINED_ENTRY_DIRECTORY: Final = ".wall-in-one-retained"
RETAINED_ENTRY_PREFIX: Final = "entry-"
_RETAINED_ENTRY_ATTEMPTS: Final = 10_000
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


def _close_descriptor_preserving_error(descriptor: int, purpose: str) -> None:
    """Close one owned fd without replacing an exception already in flight."""
    active_error = sys.exception()
    try:
        os.close(descriptor)
    except OSError as close_error:
        if active_error is not None:
            active_error.add_note(f"also could not close {purpose}: {close_error}")


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


def file_fingerprint(status: os.stat_result) -> FileFingerprint:
    """Return the persisted generation evidence for one regular-file stat."""
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("a file fingerprint requires a regular file")
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _rename_invariant_fingerprint(
    fingerprint: FileFingerprint,
) -> _RenameInvariantFingerprint:
    """Fields a same-filesystem rename cannot legitimately change."""
    return fingerprint[:4]


def _is_consumed_regular_tombstone(
    status: os.stat_result,
    expected_fingerprint: FileFingerprint,
) -> bool:
    """Whether ``status`` proves this journaled inode was already emptied.

    Before truncation, :class:`ClaimedPath` stamps the exact inode's atime with
    its journaled pre-claim ctime and verifies that value through the writable
    descriptor. ``ftruncate`` leaves atime intact. The marker therefore binds
    the zero-size transition to generation evidence which an arbitrary empty
    replacement would not carry, while device/inode and the live claim link
    continue to bind the filesystem object itself.
    """
    return (
        expected_fingerprint[2] > 0
        and stat.S_ISREG(status.st_mode)
        and (status.st_dev, status.st_ino) == expected_fingerprint[:2]
        and status.st_nlink == 1
        and status.st_size == 0
        and status.st_atime_ns == expected_fingerprint[4]
    )


def regular_file_fingerprint(path: Path) -> FileFingerprint:
    """Inspect one named regular file and capture its generation evidence."""
    try:
        status = path.lstat()
    except OSError as error:
        raise OSError(error.errno, f"cannot inspect {path}: {error}") from error
    try:
        return file_fingerprint(status)
    except ValueError as error:
        raise PathChangedError(f"{path} is not a regular file") from error


def _entry_has_type(status: os.stat_result, expected_file_type: int) -> bool:
    return stat.S_IFMT(status.st_mode) == expected_file_type


def _same_pinned_entry(
    pinned: os.stat_result,
    named: os.stat_result,
    expected_file_type: int,
) -> bool:
    """Whether a descriptor and pathname still expose one entry generation."""
    if not (
        _entry_has_type(pinned, expected_file_type)
        and _entry_has_type(named, expected_file_type)
        and (pinned.st_dev, pinned.st_ino) == (named.st_dev, named.st_ino)
    ):
        return False
    if expected_file_type == stat.S_IFREG:
        return file_fingerprint(pinned) == file_fingerprint(named)
    return True


@dataclass(slots=True)
class PinnedPath:
    """A live Linux path reference which prevents its inode being recycled."""

    path: Path
    descriptor: int = field(repr=False)
    expected_file_type: int
    _closed: bool = field(default=False, init=False, repr=False)

    def status(self) -> os.stat_result:
        if self._closed:
            raise OSError(f"the pinned reference for {self.path} is closed")
        return os.fstat(self.descriptor)

    @property
    def identity(self) -> PathIdentity:
        status = self.status()
        return status.st_dev, status.st_ino

    @property
    def fingerprint(self) -> FileFingerprint:
        return file_fingerprint(self.status())

    def close(self) -> None:
        if not self._closed:
            descriptor = self.descriptor
            self._closed = True
            self.descriptor = -1
            _close_descriptor_preserving_error(
                descriptor,
                f"the pinned reference for {self.path}",
            )

    def __enter__(self) -> PinnedPath:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            with contextlib.suppress(OSError):
                self.close()


@dataclass(frozen=True, slots=True)
class RootScopedPin:
    """A file pin plus the directory context used to reach that file."""

    source: PinnedPath
    root_identity: PathIdentity
    parent_identity: PathIdentity


@dataclass(slots=True)
class PinnedDirectoryContext:
    """Retained root and descendant-directory capabilities for crash replay.

    ``directory_anchor`` deliberately uses Linux's descriptor namespace.  A
    later rename, unmount, or replacement of the public root cannot redirect
    child lookups through this path to a different filesystem context.
    """

    root: Path
    directory: Path
    root_descriptor: int = field(repr=False)
    directory_descriptor: int = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def root_anchor(self) -> Path:
        if self._closed:
            raise OSError(f"the pinned directory context for {self.root} is closed")
        return Path("/proc/self/fd") / str(self.root_descriptor)

    @property
    def directory_anchor(self) -> Path:
        if self._closed:
            raise OSError(f"the pinned directory context for {self.directory} is closed")
        return Path("/proc/self/fd") / str(self.directory_descriptor)

    def child(self, name: str) -> Path:
        """Address one direct child through the retained directory capability."""
        if not name or name in (".", "..") or "/" in name:
            raise ValueError("a pinned directory child must be one path component")
        return self.directory_anchor / name

    def verify_public(self) -> None:
        """Require the public root and directory names to retain these capabilities.

        Descriptor-anchored work deliberately survives a public rename.  A
        publisher, however, must not report success through a replacement
        public directory after writing into the renamed-away original.  Walk
        the public names again without symlinks and compare both live inode
        identities before crossing that visibility boundary.
        """
        if self._closed:
            raise OSError(f"the pinned directory context for {self.root} is closed")
        root_status = os.fstat(self.root_descriptor)
        directory_status = os.fstat(self.directory_descriptor)
        verification = pin_directory_beneath(
            self.root,
            self.directory,
            expected_root_identity=(root_status.st_dev, root_status.st_ino),
            expected_directory_identity=(directory_status.st_dev, directory_status.st_ino),
        )
        verification.close()

    @contextlib.contextmanager
    def access_descendant(self, path: Path) -> Iterator[Path]:
        """Yield one descendant through retained, no-symlink parent traversal."""
        if self._closed:
            raise OSError(f"the pinned directory context for {self.root} is closed")
        try:
            relative = path.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"{path} is not beneath pinned root {self.root}") from error
        components = relative.parts
        if not components or any(component in ("", ".", "..") for component in components):
            raise ValueError(f"{path} is not one bounded descendant of {self.root}")
        parent_descriptor = os.dup(self.root_descriptor)
        try:
            flags = os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            for component in components[:-1]:
                child = os.open(component, flags, dir_fd=parent_descriptor)
                try:
                    if not stat.S_ISDIR(os.fstat(child).st_mode):
                        raise PathChangedError(f"{path} contains a non-directory component")
                except BaseException:
                    _close_descriptor_preserving_error(
                        child,
                        f"the retained descendant component for {path}",
                    )
                    raise
                previous_descriptor = parent_descriptor
                parent_descriptor = child
                _close_descriptor_preserving_error(
                    previous_descriptor,
                    f"the retained ancestor component for {path}",
                )
            yield Path("/proc/self/fd") / str(parent_descriptor) / components[-1]
        finally:
            descriptor = parent_descriptor
            parent_descriptor = -1
            _close_descriptor_preserving_error(
                descriptor,
                f"the retained descendant parent for {path}",
            )

    def close(self) -> None:
        if not self._closed:
            directory_descriptor = self.directory_descriptor
            root_descriptor = self.root_descriptor
            self._closed = True
            self.directory_descriptor = -1
            self.root_descriptor = -1
            try:
                _close_descriptor_preserving_error(
                    directory_descriptor,
                    f"the pinned directory {self.directory}",
                )
            finally:
                _close_descriptor_preserving_error(
                    root_descriptor,
                    f"the pinned root {self.root}",
                )

    def __enter__(self) -> PinnedDirectoryContext:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            with contextlib.suppress(OSError):
                self.close()


def pin_path(
    path: Path,
    *,
    expected_file_type: int,
    expected_identity: PathIdentity | None = None,
    expected_fingerprint: FileFingerprint | None = None,
) -> PinnedPath:
    """Open and verify a no-follow O_PATH reference to one named entry.

    Keeping the returned descriptor open prevents Linux from recycling the
    referenced inode.  The pathname is checked against the descriptor after
    opening, so a replacement which wins the open race is never mistaken for
    the entry observed by the caller.
    """
    if (
        expected_file_type == 0
        or expected_file_type == stat.S_IFDIR
        or stat.S_IFMT(expected_file_type) != expected_file_type
    ):
        raise ValueError("expected_file_type must be one exact non-directory stat type")
    if expected_fingerprint is not None and expected_file_type != stat.S_IFREG:
        raise ValueError("only a regular file has a persisted fingerprint")
    flags = os.O_PATH | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OSError(error.errno, f"cannot pin {path}: {error}") from error
    pinned = PinnedPath(path, descriptor, expected_file_type)
    try:
        opened = pinned.status()
        named = path.lstat()
        if not (
            _entry_has_type(opened, expected_file_type)
            and _entry_has_type(named, expected_file_type)
        ):
            raise PathChangedError(f"{path} changed and no longer has its expected entry type")
        if not _same_pinned_entry(opened, named, expected_file_type):
            raise PathChangedError(f"{path} changed while it was being pinned")
        if expected_identity is not None and pinned.identity != expected_identity:
            raise PathChangedError(f"{path} changed before it could be claimed")
        if expected_fingerprint is not None and pinned.fingerprint != expected_fingerprint:
            raise PathChangedError(f"{path} changed generation before it could be claimed")
    except BaseException:
        pinned.close()
        raise
    return pinned


def pin_regular_path(
    path: Path,
    *,
    expected_identity: PathIdentity | None = None,
    expected_fingerprint: FileFingerprint | None = None,
) -> PinnedPath:
    """Pin one exact regular file for a later atomic claim."""
    return pin_path(
        path,
        expected_file_type=stat.S_IFREG,
        expected_identity=expected_identity,
        expected_fingerprint=expected_fingerprint,
    )


def pin_directory_path(
    path: Path,
    *,
    expected_identity: PathIdentity | None = None,
    require_private: bool = False,
) -> PinnedPath:
    """Pin one exact directory without following its final pathname.

    ``require_private`` additionally verifies mode ``0700`` and current-user
    ownership. The returned descriptor is an anchored access capability, not
    proof that this process created the directory: ``mkdir(2)`` cannot return
    an ownership descriptor atomically. A caller needs independent provenance
    before using any directory pin as move or cleanup authority.
    """
    descriptor: int | None = None
    pinned: PinnedPath | None = None
    try:
        descriptor = os.open(
            path,
            os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        pinned = PinnedPath(path, descriptor, stat.S_IFDIR)
        descriptor = None
        opened = pinned.status()
        named = path.lstat()
        if not _same_pinned_entry(opened, named, stat.S_IFDIR):
            raise PathChangedError(f"{path} changed while its directory was being pinned")
        if expected_identity is not None and pinned.identity != expected_identity:
            raise PathChangedError(f"{path} changed before its directory could be claimed")
        if require_private and not (
            opened.st_uid == os.getuid()
            and named.st_uid == os.getuid()
            and stat.S_IMODE(opened.st_mode) == 0o700
            and stat.S_IMODE(named.st_mode) == 0o700
        ):
            raise PathChangedError(f"{path} is not a private directory owned by this user")
    except BaseException:
        if pinned is not None:
            pinned.close()
        elif descriptor is not None:
            _close_descriptor_preserving_error(
                descriptor,
                f"the pinned directory {path}",
            )
        raise
    if pinned is None:  # pragma: no cover - every non-returning branch raises above
        raise AssertionError("directory pinning did not retain a descriptor")
    return pinned


def _pin_directory_for_durability(directory: Path) -> PinnedPath:
    """Retain a directory capability which can anchor a later fsync.

    Destructive callers sometimes already address a directory through one of
    our ``/proc/self/fd/<n>`` capabilities. Reopening that spelling with
    ``O_NOFOLLOW`` would reject the procfs link, so duplicate the descriptor
    itself in that narrow case. Ordinary paths keep the existing final-name
    no-follow check from :func:`pin_directory_path`.
    """
    parts = directory.parts
    if len(parts) == 5 and parts[:4] == ("/", "proc", "self", "fd") and parts[4].isdecimal():
        descriptor = os.dup(int(parts[4]))
        pinned = PinnedPath(directory, descriptor, stat.S_IFDIR)
        try:
            opened = pinned.status()
            named = directory.stat()
            if not _same_pinned_entry(opened, named, stat.S_IFDIR):
                raise PathChangedError(f"the retained directory capability {directory} changed")
        except BaseException:
            pinned.close()
            raise
        return pinned
    return pin_directory_path(directory)


def _fsync_directory_capability(
    descriptor: int,
    *,
    expected_identity: PathIdentity,
    logical_path: Path,
) -> None:
    """Persist directory entries through one retained directory capability."""
    sync_descriptor: int | None = None
    try:
        retained = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(retained.st_mode)
            or (retained.st_dev, retained.st_ino) != expected_identity
        ):
            raise PathChangedError(f"the retained durability directory {logical_path} changed")
        sync_descriptor = os.open(
            ".",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
        opened = os.fstat(sync_descriptor)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != expected_identity:
            raise PathChangedError(f"the retained durability directory {logical_path} changed")
        os.fsync(sync_descriptor)
        after = os.fstat(sync_descriptor)
        retained_after = os.fstat(descriptor)
        if not (
            stat.S_ISDIR(after.st_mode)
            and stat.S_ISDIR(retained_after.st_mode)
            and (after.st_dev, after.st_ino) == expected_identity
            and (retained_after.st_dev, retained_after.st_ino) == expected_identity
        ):
            raise PathChangedError(
                f"the retained durability directory {logical_path} changed during fsync"
            )
    finally:
        if sync_descriptor is not None:
            _close_descriptor_preserving_error(
                sync_descriptor,
                f"the durability descriptor for {logical_path}",
            )


def _open_directory_without_symlinks(path: Path) -> int:
    """Open an absolute directory by walking every component without links."""
    if not path.is_absolute():
        raise ValueError("the anchored directory path must be absolute")
    flags = os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(Path(path.anchor), flags)
    try:
        for component in path.parts[1:]:
            if component in ("", ".", ".."):
                raise ValueError("anchored paths cannot contain dot components")
            child = os.open(component, flags, dir_fd=descriptor)
            try:
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise PathChangedError(f"{path} contains a non-directory component")
            except BaseException:
                _close_descriptor_preserving_error(
                    child,
                    f"the directory component for {path}",
                )
                raise
            previous_descriptor = descriptor
            descriptor = child
            _close_descriptor_preserving_error(
                previous_descriptor,
                f"the directory ancestor for {path}",
            )
    except BaseException:
        owned_descriptor = descriptor
        descriptor = -1
        _close_descriptor_preserving_error(
            owned_descriptor,
            f"the directory walk for {path}",
        )
        raise
    return descriptor


def pin_directory_beneath(
    root: Path,
    directory: Path,
    *,
    expected_root_identity: PathIdentity,
    expected_directory_identity: PathIdentity,
) -> PinnedDirectoryContext:
    """Pin one journaled directory as reached through its journaled root.

    Both paths are walked without following symlinks.  Once returned, child
    lookups through :attr:`PinnedDirectoryContext.directory_anchor` remain on
    the same filesystem objects even if the public mountpoint is replaced.
    """
    if not root.is_absolute() or not directory.is_absolute():
        raise ValueError("root-scoped directory paths must be absolute")
    try:
        relative = directory.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{directory} is not beneath {root}") from error
    components = relative.parts
    if any(component in ("", ".", "..") for component in components):
        raise ValueError(f"{directory} is not a bounded directory beneath {root}")

    root_descriptor = _open_directory_without_symlinks(root)
    directory_descriptor: int | None = None
    try:
        root_status = os.fstat(root_descriptor)
        if (root_status.st_dev, root_status.st_ino) != expected_root_identity:
            raise PathChangedError(f"{root} is not the journaled filesystem root")
        directory_descriptor = os.dup(root_descriptor)
        flags = os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        for component in components:
            child = os.open(component, flags, dir_fd=directory_descriptor)
            try:
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise PathChangedError(f"{directory} contains a non-directory component")
            except BaseException:
                _close_descriptor_preserving_error(
                    child,
                    f"the source-directory component for {directory}",
                )
                raise
            previous_descriptor = directory_descriptor
            directory_descriptor = child
            _close_descriptor_preserving_error(
                previous_descriptor,
                f"the source-directory ancestor for {directory}",
            )
        directory_status = os.fstat(directory_descriptor)
        if (directory_status.st_dev, directory_status.st_ino) != expected_directory_identity:
            raise PathChangedError(f"{directory} is not the journaled source directory")
        anchored_status = (Path("/proc/self/fd") / str(directory_descriptor)).stat()
        if (anchored_status.st_dev, anchored_status.st_ino) != expected_directory_identity:
            raise OSError(f"cannot address the pinned source directory {directory}")
    except BaseException:
        if directory_descriptor is not None:
            owned_directory_descriptor = directory_descriptor
            directory_descriptor = None
            _close_descriptor_preserving_error(
                owned_directory_descriptor,
                f"the source-directory walk for {directory}",
            )
        owned_root_descriptor = root_descriptor
        root_descriptor = -1
        _close_descriptor_preserving_error(
            owned_root_descriptor,
            f"the pinned source root {root}",
        )
        raise
    return PinnedDirectoryContext(root, directory, root_descriptor, directory_descriptor)


def pin_regular_path_beneath(root: Path, path: Path) -> RootScopedPin:
    """Pin a regular file reached beneath ``root`` without following ancestors.

    A lexical ``Path.is_relative_to`` check does not constrain kernel path
    traversal: a directory between the configured root and a scanned media
    path can be replaced with a symlink before removal is prepared.  Walk from
    the filesystem root with retained ``O_PATH|O_DIRECTORY|O_NOFOLLOW``
    descriptors, then open the final entry relative to its pinned parent.  The
    ordinary logical pathname is checked against that capability before it is
    returned; later destructive helpers repeat the name check while retaining
    this same final-file pin.
    """
    if not root.is_absolute() or not path.is_absolute():
        raise ValueError("root-scoped file paths must be absolute")
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{path} is not beneath {root}") from error
    components = relative.parts
    if not components or any(component in ("", ".", "..") for component in components):
        raise ValueError(f"{path} is not one regular-file descendant of {root}")

    parent_descriptor = _open_directory_without_symlinks(root)
    pinned: PinnedPath | None = None
    root_identity: PathIdentity | None = None
    parent_identity: PathIdentity | None = None
    try:
        root_status = os.fstat(parent_descriptor)
        root_identity = root_status.st_dev, root_status.st_ino
        directory_flags = os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        for component in components[:-1]:
            child = os.open(component, directory_flags, dir_fd=parent_descriptor)
            try:
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise PathChangedError(
                        f"{path} contains a non-directory component beneath {root}"
                    )
            except BaseException:
                _close_descriptor_preserving_error(
                    child,
                    f"the source-file directory component for {path}",
                )
                raise
            previous_descriptor = parent_descriptor
            parent_descriptor = child
            _close_descriptor_preserving_error(
                previous_descriptor,
                f"the source-file directory ancestor for {path}",
            )

        parent_status = os.fstat(parent_descriptor)
        parent_identity = parent_status.st_dev, parent_status.st_ino
        final_name = components[-1]
        descriptor = os.open(
            final_name,
            os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        pinned = PinnedPath(path, descriptor, stat.S_IFREG)
        opened = pinned.status()
        relative_named = os.stat(
            final_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        logical_named = path.lstat()
        if not (
            _same_pinned_entry(opened, relative_named, stat.S_IFREG)
            and _same_pinned_entry(opened, logical_named, stat.S_IFREG)
        ):
            raise PathChangedError(
                f"{path} changed while it was being pinned beneath configured root {root}"
            )
    except BaseException:
        if pinned is not None:
            pinned.close()
        raise
    finally:
        owned_parent_descriptor = parent_descriptor
        parent_descriptor = -1
        _close_descriptor_preserving_error(
            owned_parent_descriptor,
            f"the source-file parent for {path}",
        )
    if (
        pinned is None or root_identity is None or parent_identity is None
    ):  # pragma: no cover - every non-returning branch raises above
        raise AssertionError("root-scoped pin creation did not produce a file capability")
    return RootScopedPin(
        source=pinned,
        root_identity=root_identity,
        parent_identity=parent_identity,
    )


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
    expected_file_type: int = stat.S_IFREG,
    expected_fingerprint: FileFingerprint | None = None,
    pinned_source: PinnedPath | None = None,
    externally_pinned: bool = False,
) -> None:
    """Atomically move exactly one expected entry to an unused pathname.

    The source is checked before the syscall and, critically, the destination
    is checked after it.  If a replacement wins the gap between those checks,
    that replacement is atomically restored without overwriting a newer source
    entry.  When restoration cannot be done safely it remains preserved at the
    destination and is reported through :class:`PathChangedError`.

    ``expected_file_type`` is the exact ``stat.S_IFMT`` value captured by the
    caller.  The source is also held through an ``O_PATH`` descriptor across
    the rename and destination verification. That live reference prevents
    same-type inode reuse, while ``expected_fingerprint`` can bind a regular
    file to an earlier durable observation. Directories are never accepted:
    this API has no unforgeable way to represent independent relocation
    authority, and a first pin obtained after creation is access-only.

    ``externally_pinned`` is a narrow escape hatch for a caller which already
    holds an independent live VFS reference, such as an ``O_PATH`` descriptor,
    to the exact directory-entry inode but cannot supply that descriptor here.
    Normal callers must leave it false and use this helper's O_PATH pin.
    """
    if expected_file_type == 0 or stat.S_IFMT(expected_file_type) != expected_file_type:
        raise ValueError("expected_file_type must be one exact stat type")
    if expected_file_type == stat.S_IFDIR:
        raise ValueError("atomic directory relocation is unsupported")
    if pinned_source is not None and externally_pinned:
        raise ValueError("choose pinned_source or externally_pinned, not both")
    owns_pin = pinned_source is None and not externally_pinned
    if pinned_source is None and not externally_pinned:
        pinned_source = pin_path(
            source,
            expected_file_type=expected_file_type,
            expected_identity=expected_identity,
            expected_fingerprint=expected_fingerprint,
        )
    try:
        if pinned_source is None:
            try:
                before = source.lstat()
            except FileNotFoundError:
                raise
            except OSError as error:
                raise OSError(error.errno, f"cannot inspect {source}: {error}") from error
        else:
            if (
                pinned_source.path != source
                or pinned_source.expected_file_type != expected_file_type
            ):
                raise ValueError("pinned_source does not describe the requested source")
            before = pinned_source.status()
        if (before.st_dev, before.st_ino) != expected_identity:
            raise PathChangedError(f"{source} changed before it could be claimed")
        if not _entry_has_type(before, expected_file_type):
            raise PathChangedError(f"{source} no longer has its expected entry type")
        if expected_fingerprint is not None:
            if expected_file_type != stat.S_IFREG:
                raise ValueError("only a regular file has a persisted fingerprint")
            if file_fingerprint(before) != expected_fingerprint:
                raise PathChangedError(f"{source} changed generation before it could be claimed")
        expected_after_move = (
            _rename_invariant_fingerprint(expected_fingerprint)
            if expected_fingerprint is not None
            else (
                _rename_invariant_fingerprint(file_fingerprint(before))
                if expected_file_type == stat.S_IFREG
                else None
            )
        )

        if pinned_source is not None:
            try:
                named = source.lstat()
            except FileNotFoundError:
                raise
            except OSError as error:
                raise OSError(error.errno, f"cannot inspect {source}: {error}") from error
            if not _same_pinned_entry(before, named, expected_file_type):
                raise PathChangedError(f"{source} changed before it could be claimed")

        _rename_noreplace(source, destination)
        try:
            moved = destination.lstat()
            pinned_after = pinned_source.status() if pinned_source is not None else None
        except OSError as error:
            preserved = _restore_after_failed_verification(source, destination)
            raise PathChangedError(
                f"could not verify {source} after its atomic claim: {error}",
                preserved_path=preserved,
            ) from error
        moved_matches = (
            (moved.st_dev, moved.st_ino) == expected_identity
            and _entry_has_type(moved, expected_file_type)
            if pinned_after is None
            else _same_pinned_entry(pinned_after, moved, expected_file_type)
        )
        if moved_matches and expected_after_move is not None:
            moved_matches = (
                _rename_invariant_fingerprint(file_fingerprint(moved)) == expected_after_move
            )
        if not moved_matches:
            preserved = _restore_after_failed_verification(source, destination)
            raise PathChangedError(
                f"{source} changed while it was being claimed",
                preserved_path=preserved,
            )
    finally:
        if owns_pin and pinned_source is not None:
            pinned_source.close()


@dataclass(slots=True)
class ClaimedPath:
    """An expected regular file held at an unpredictable private pathname."""

    original: Path
    path: Path
    identity: PathIdentity
    _fingerprint: FileFingerprint = field(repr=False)
    _directory: Path = field(repr=False)
    _original_access: Path = field(repr=False)
    _directory_pin: _PinnedClaimDirectory = field(repr=False)
    _pin: PinnedPath = field(repr=False)
    _retained_access_parent: Path | None = field(default=None, repr=False, kw_only=True)
    _retained_logical_parent: Path | None = field(default=None, repr=False, kw_only=True)
    _consumed: bool = field(default=False, init=False, repr=False)

    def _verify(self, *, require_expected_generation: bool = True) -> None:
        if self._consumed:
            raise OSError(f"the claim for {self.original} has already been consumed")
        try:
            pinned = self._pin.status()
            status = self._directory_pin.entry.lstat()
        except OSError as error:
            raise PathChangedError(
                f"the private claim for {self.original} is no longer available",
                preserved_path=self.path if os.path.lexists(self._directory_pin.entry) else None,
            ) from error
        if (
            not _same_pinned_entry(pinned, status, stat.S_IFREG)
            or (
                pinned.st_dev,
                pinned.st_ino,
            )
            != self.identity
        ):
            raise PathChangedError(
                f"the private claim for {self.original} was replaced",
                preserved_path=self.path,
            )
        if require_expected_generation and _rename_invariant_fingerprint(
            file_fingerprint(pinned)
        ) != _rename_invariant_fingerprint(self._fingerprint):
            raise PathChangedError(
                f"the private claim for {self.original} changed generation",
                preserved_path=self.path,
            )

    def _finish(self) -> None:
        self._consumed = True
        try:
            self._pin.close()
        finally:
            # mkdir(2) cannot return a descriptor for the directory it creates.
            # A same-UID writer can therefore replace a freshly allocated name
            # before its first pin, and that pin proves only the current
            # generation -- not who created it.  The directory is an access
            # capability, never cleanup authority; only the independently
            # pinned regular-file claim may be moved or consumed.
            self._directory_pin.close()

    def _open_exact_writable_claim(self) -> tuple[int, os.stat_result]:
        """Open the retained regular generation without trusting its name.

        Linux has no identity-conditional unlink.  A final pathname unlink
        would therefore reintroduce a replacement race even inside the private
        claim directory.  Opening the claim for writing and comparing that new
        descriptor with both retained capabilities gives :meth:`discard` an
        operation it can perform on the exact inode instead.
        """
        self._verify()
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self._directory_pin.entry,
                os.O_WRONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW,
            )
            opened = os.fstat(descriptor)
            pinned = self._pin.status()
            named = self._directory_pin.entry.lstat()
            if not (
                _same_pinned_entry(pinned, opened, stat.S_IFREG)
                and _same_pinned_entry(pinned, named, stat.S_IFREG)
                and (opened.st_dev, opened.st_ino) == self.identity
            ):
                raise PathChangedError(
                    f"the private claim for {self.original} changed before discard",
                    preserved_path=self.path,
                )
        except BaseException:
            if descriptor is not None:
                owned_descriptor = descriptor
                descriptor = None
                _close_descriptor_preserving_error(
                    owned_descriptor,
                    f"the writable private claim for {self.original}",
                )
            raise
        if descriptor is None:  # pragma: no cover - every non-returning branch raises above
            raise AssertionError("claim discard did not retain a writable descriptor")
        return descriptor, opened

    def _retain_discarded_inode(self) -> None:
        """Move inert residue out of a replayable operation claim if possible."""
        retained_parent = self._retained_access_parent or self._original_access.parent
        logical_parent = self._retained_logical_parent or self.original.parent
        retained_pin = PinnedPath(
            self._directory_pin.entry,
            os.dup(self._pin.descriptor),
            stat.S_IFREG,
        )
        try:
            _retain_exact_entry(
                self._directory_pin.entry,
                retained_parent=retained_parent,
                logical_parent=logical_parent,
                expected_identity=self.identity,
                expected_file_type=stat.S_IFREG,
                expected_fingerprint=retained_pin.fingerprint,
                pinned_source=retained_pin,
            )
        finally:
            retained_pin.close()

    def discard(self) -> None:
        """Destroy the exact claimed generation without unlinking by name.

        The public name was already removed atomically when the claim was made.
        For a singly-linked regular file, truncating through a separately
        verified descriptor releases its contents without a final
        check-then-unlink race.  A multiply-linked inode is retained intact so
        deleting this pathname cannot change the contents visible through an
        unrelated hard link.  In both cases an inert hidden claim entry may
        remain: safe retention is preferable to unlinking an unproven
        replacement.
        """
        descriptor, opened = self._open_exact_writable_claim()
        truncated = False
        marker_stamped = False
        try:
            if opened.st_nlink == 1:
                # Persist the original ctime in a timestamp field which
                # ftruncate does not alter. It is exact evidence already
                # representable by this filesystem, unlike an invented magic
                # timestamp which the filesystem might round. Read it back,
                # recheck the named generation and link count, and fail before
                # destruction if the marker cannot be represented exactly.
                os.utime(
                    descriptor,
                    ns=(self._fingerprint[4], opened.st_mtime_ns),
                )
                marker_stamped = True
                marked = os.fstat(descriptor)
                pinned = self._pin.status()
                named = self._directory_pin.entry.lstat()
                if not (
                    _same_pinned_entry(pinned, marked, stat.S_IFREG)
                    and _same_pinned_entry(pinned, named, stat.S_IFREG)
                    and marked.st_nlink == 1
                    and marked.st_atime_ns == self._fingerprint[4]
                    and _rename_invariant_fingerprint(file_fingerprint(marked))
                    == _rename_invariant_fingerprint(self._fingerprint)
                ):
                    raise PathChangedError(
                        f"the private claim for {self.original} could not retain its "
                        "discard marker",
                        preserved_path=self.path,
                    )
                # Make the marker independently durable. A crash before the
                # following truncate then replays as the still-nonzero
                # journaled generation; a crash after the final fsync below
                # replays as the proven consumed tombstone.
                os.fsync(descriptor)
                os.ftruncate(descriptor, 0)
                truncated = True
                # Once truncation succeeds the destructive commit cannot be
                # rolled back.  A sync failure must not make replay restore an
                # empty file or retry this already-consumed claim forever.
                with contextlib.suppress(OSError):
                    os.fsync(descriptor)
        except BaseException:
            if marker_stamped and not truncated:
                # A pre-destruction failure may still be restored publicly.
                # Put back only atime; retain the current mtime so a concurrent
                # writer cannot have its mutation timestamp hidden.
                with contextlib.suppress(OSError):
                    current = os.fstat(descriptor)
                    os.utime(
                        descriptor,
                        ns=(opened.st_atime_ns, current.st_mtime_ns),
                    )
            raise
        finally:
            _close_descriptor_preserving_error(
                descriptor,
                f"the writable private claim for {self.original}",
            )
        if not truncated and opened.st_nlink <= 0:  # pragma: no cover - defensive stat invariant
            raise OSError(f"the private claim for {self.original} has no filesystem links")
        # The public removal is already committed. Failure to centralise inert
        # residue must not make replay restore an empty file or keep retrying an
        # irreversible discard; `_finish` simply leaves it in this claim.
        with contextlib.suppress(OSError, ValueError):
            self._retain_discarded_inode()
        self._finish()

    def is_present(self) -> bool:
        """Whether the exact retained claim entry remains in its pinned directory."""
        return not self._consumed and os.path.lexists(self._directory_pin.entry)

    def restore(self) -> bool:
        """Durably restore without replacement; leave it preserved on conflict."""
        # A writer which already had the inode open may have changed its
        # contents after the claim. Restoring that same pinned inode is safe
        # and discoverable; discarding it is not.
        destination_parent_pin = _pin_directory_for_durability(self._original_access.parent)
        destination_access = (
            Path("/proc/self/fd")
            / str(destination_parent_pin.descriptor)
            / self._original_access.name
        )
        restore_pin = PinnedPath(
            self._directory_pin.entry,
            os.dup(self._pin.descriptor),
            stat.S_IFREG,
        )
        try:
            try:
                atomic_move_no_replace(
                    self._directory_pin.entry,
                    destination_access,
                    expected_identity=self.identity,
                    expected_fingerprint=self._pin.fingerprint,
                    pinned_source=restore_pin,
                )
            finally:
                restore_pin.close()
        except FileExistsError:
            destination_parent_pin.close()
            return False
        except PathChangedError as error:
            destination_parent_pin.close()
            raise PathChangedError(
                str(error)
                .replace(str(self._directory_pin.entry), str(self.path))
                .replace(str(destination_access), str(self.original)),
                preserved_path=self.original if error.preserved_path is not None else None,
            ) from error
        except OSError as error:
            destination_parent_pin.close()
            raise OSError(
                error.errno,
                f"could not restore the private claim at {self.path} to {self.original}: "
                f"{error.strerror or error}",
            ) from error
        try:
            # Persist the new public link before the private-link withdrawal.
            # If the second barrier fails, a replay may retain both hard links,
            # but it can never lose the only durable link or truncate through
            # the multiply-linked claim.
            _fsync_directory_capability(
                destination_parent_pin.descriptor,
                expected_identity=destination_parent_pin.identity,
                logical_path=self.original.parent,
            )
            _fsync_directory_capability(
                self._directory_pin.descriptor,
                expected_identity=self._directory_pin.identity,
                logical_path=self._directory,
            )
        except OSError as error:
            self._finish()
            raise PathChangedError(
                f"the restoration of {self.original} could not be made durable: {error}",
                preserved_path=self.original,
            ) from error
        finally:
            destination_parent_pin.close()
        self._finish()
        return True

    def close(self) -> None:
        """Release the inode pin without consuming a crash-replayable claim."""
        try:
            self._pin.close()
        finally:
            self._directory_pin.close()

    def __del__(self) -> None:
        with contextlib.suppress(OSError):
            self.close()


def claim_for_deletion(
    path: Path,
    *,
    expected_identity: PathIdentity,
    operation_token: str | None = None,
    expected_fingerprint: FileFingerprint | None = None,
    pinned_source: PinnedPath | None = None,
    logical_path: Path | None = None,
    retained_parent: Path | None = None,
    logical_retained_parent: Path | None = None,
) -> ClaimedPath:
    """Atomically take deletion authority over one expected regular file.

    A durable token uses its discoverable mode-0700 directory beside the
    source. A transient claim starts inside the shared retained namespace so
    its deliberately unretired container is already centralised. Directory
    pins are access capabilities only: Linux cannot atomically create and pin
    a directory, so no later cleanup infers ownership from the post-creation
    pin. Callers either :meth:`ClaimedPath.discard` the independently pinned
    file or restore it with no-replace semantics after a pre-commit failure.
    """
    public_path = path if logical_path is None else logical_path
    retained_access_parent = path.parent if retained_parent is None else retained_parent
    retained_public_parent = (
        public_path.parent if logical_retained_parent is None else logical_retained_parent
    )
    if operation_token is None:
        retained_access_directory = retained_access_parent / RETAINED_ENTRY_DIRECTORY
        retained_public_directory = retained_public_parent / RETAINED_ENTRY_DIRECTORY
        with contextlib.suppress(FileExistsError):
            retained_access_directory.mkdir(mode=0o700)
        retained_directory_pin = _pin_private_claim_directory(retained_access_directory)
        try:
            retained_parent_pin = _pin_directory_for_durability(retained_access_parent)
            try:
                # Persist the shared retained namespace before a transient
                # claim relies on a newly-created child of it.
                _fsync_directory_capability(
                    retained_parent_pin.descriptor,
                    expected_identity=retained_parent_pin.identity,
                    logical_path=retained_public_parent,
                )
            finally:
                retained_parent_pin.close()
            access_directory = Path(
                tempfile.mkdtemp(
                    prefix=RETAINED_ENTRY_PREFIX,
                    dir=retained_directory_pin.anchor,
                )
            )
            directory = retained_public_directory / access_directory.name
            directory_pin = _pin_private_claim_directory(access_directory)
            try:
                # Persist the random claim-directory name before moving the
                # only public link to the file beneath it.
                _fsync_directory_capability(
                    retained_directory_pin.descriptor,
                    expected_identity=retained_directory_pin.identity,
                    logical_path=retained_public_directory,
                )
            except BaseException:
                directory_pin.close()
                raise
        finally:
            retained_directory_pin.close()
    else:
        access_directory = deletion_claim_directory(path, operation_token)
        directory = deletion_claim_directory(public_path, operation_token)
        access_directory.mkdir(mode=0o700)
        directory_pin = _pin_private_claim_directory(access_directory)
    claimed = directory / "entry"
    owns_pin = pinned_source is None
    try:
        source_parent_pin = _pin_directory_for_durability(path.parent)
        try:
            if pinned_source is None:
                pinned_source = pin_regular_path(
                    path,
                    expected_identity=expected_identity,
                    expected_fingerprint=expected_fingerprint,
                )
            claim_fingerprint = expected_fingerprint or pinned_source.fingerprint
            source_access = Path("/proc/self/fd") / str(source_parent_pin.descriptor) / path.name
            move_pin = PinnedPath(
                source_access,
                os.dup(pinned_source.descriptor),
                stat.S_IFREG,
            )
            try:
                # Use the same parent capability which will be synced below,
                # so an ancestor rebind cannot redirect the rename into a
                # different, unsynced directory.
                atomic_move_no_replace(
                    source_access,
                    directory_pin.entry,
                    expected_identity=expected_identity,
                    expected_fingerprint=claim_fingerprint,
                    pinned_source=move_pin,
                )
            finally:
                # This duplicate is access-only. Its close cannot undo the
                # rename and must not skip the durability barriers below.
                with contextlib.suppress(OSError):
                    move_pin.close()
            try:
                # A successful rename is not yet a power-loss-safe deletion
                # claim. Persist the new private link first, then the
                # withdrawal of the original link, before any caller may
                # truncate the inode.
                _fsync_directory_capability(
                    directory_pin.descriptor,
                    expected_identity=directory_pin.identity,
                    logical_path=directory,
                )
                _fsync_directory_capability(
                    source_parent_pin.descriptor,
                    expected_identity=source_parent_pin.identity,
                    logical_path=public_path.parent,
                )
            except OSError as error:
                raise PathChangedError(
                    f"the atomic claim for {public_path} could not be made durable: {error}",
                    preserved_path=directory_pin.entry,
                ) from error
        finally:
            # Closing an already-synced directory capability cannot make its
            # committed rename less durable. Mark-and-close is non-retrying;
            # do not turn a close anomaly into a false failed claim.
            with contextlib.suppress(OSError):
                source_parent_pin.close()
    except PathChangedError as error:
        directory_anchor = directory_pin.anchor
        try:
            if owns_pin and pinned_source is not None:
                pinned_source.close()
        finally:
            directory_pin.close()
        detail = str(error).replace(str(directory_anchor), str(directory))
        detail = detail.replace(str(path), str(public_path))
        preserved = claimed if error.preserved_path is not None else None
        raise PathChangedError(detail, preserved_path=preserved) from error
    except OSError, ValueError:
        try:
            if owns_pin and pinned_source is not None:
                pinned_source.close()
        finally:
            directory_pin.close()
        raise
    return ClaimedPath(
        public_path,
        claimed,
        expected_identity,
        claim_fingerprint,
        directory,
        path,
        directory_pin,
        pinned_source,
        _retained_access_parent=retained_access_parent,
        _retained_logical_parent=retained_public_parent,
    )


def deletion_claim_directory(path: Path, operation_token: str) -> Path:
    """The replayable private claim location for one durable removal intent."""
    if len(operation_token) != 32 or any(
        character not in "0123456789abcdef" for character in operation_token
    ):
        raise ValueError("removal operation token must be 32 lowercase hexadecimal characters")
    return path.parent / f"{DELETION_CLAIM_PREFIX}{operation_token}"


@dataclass(slots=True)
class _PinnedClaimDirectory:
    """One private directory reached only through its live access capability.

    The pin proves the currently named generation and protects subsequent
    child traversal. It deliberately does not prove that this process created
    the directory, so callers must never use it as directory cleanup authority.
    """

    path: Path
    descriptor: int = field(repr=False)
    identity: PathIdentity
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def anchor(self) -> Path:
        if self._closed:
            raise OSError(f"the pinned removal claim {self.path} is closed")
        return Path("/proc/self/fd") / str(self.descriptor)

    @property
    def entry(self) -> Path:
        return self.anchor / "entry"

    def close(self) -> None:
        if not self._closed:
            descriptor = self.descriptor
            self._closed = True
            self.descriptor = -1
            _close_descriptor_preserving_error(
                descriptor,
                f"the pinned removal claim {self.path}",
            )

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            with contextlib.suppress(OSError):
                self.close()


def _pin_private_claim_directory(directory: Path) -> _PinnedClaimDirectory:
    """Open and verify a private claim directory without following its name."""
    descriptor: int | None = None
    try:
        descriptor = os.open(
            directory,
            os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        opened = os.fstat(descriptor)
        named = directory.lstat()
    except FileNotFoundError:
        if descriptor is not None:
            owned_descriptor = descriptor
            descriptor = None
            _close_descriptor_preserving_error(
                owned_descriptor,
                f"the removal claim {directory}",
            )
        raise
    except OSError as error:
        if descriptor is not None:
            owned_descriptor = descriptor
            descriptor = None
            _close_descriptor_preserving_error(
                owned_descriptor,
                f"the removal claim {directory}",
            )
        raise OSError(error.errno, f"cannot inspect removal claim {directory}: {error}") from error
    try:
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_uid != os.getuid()
            or named.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
            or stat.S_IMODE(named.st_mode) != 0o700
        ):
            raise OSError(
                f"removal claim is not a private directory owned by this user: {directory}"
            )
        identity = opened.st_dev, opened.st_ino
        anchor = Path("/proc/self/fd") / str(descriptor)
        anchored = anchor.stat()
        if (anchored.st_dev, anchored.st_ino) != identity:
            raise OSError(f"cannot address pinned removal claim {directory}")
    except BaseException:
        owned_descriptor = descriptor
        descriptor = None
        _close_descriptor_preserving_error(
            owned_descriptor,
            f"the removal claim {directory}",
        )
        raise
    if descriptor is None:  # pragma: no cover - every non-returning branch raises above
        raise AssertionError("removal claim pinning did not retain its directory")
    return _PinnedClaimDirectory(directory, descriptor, identity)


def _retain_exact_entry(
    path: Path,
    *,
    expected_identity: PathIdentity,
    expected_file_type: int,
    expected_fingerprint: FileFingerprint | None = None,
    pinned_source: PinnedPath | None = None,
    externally_pinned: bool = False,
    retained_parent: Path | None = None,
    logical_parent: Path | None = None,
    logical_path: Path | None = None,
) -> Path:
    """Move one exact inode into a shared inert-residue namespace.

    This is the safe terminal operation when Linux cannot condition an unlink
    on inode identity.  One mode-0700 directory per filesystem parent avoids a
    claim directory for every cleanup while random child names prevent one
    retained generation from replacing another.
    """
    access_parent = path.parent if retained_parent is None else retained_parent
    public_parent = access_parent if logical_parent is None else logical_parent
    access_directory = access_parent / RETAINED_ENTRY_DIRECTORY
    public_directory = public_parent / RETAINED_ENTRY_DIRECTORY
    with contextlib.suppress(FileExistsError):
        access_directory.mkdir(mode=0o700)
    directory_pin = _pin_private_claim_directory(access_directory)
    public_source = path if logical_path is None else logical_path
    try:
        for _attempt in range(_RETAINED_ENTRY_ATTEMPTS):
            name = f"{RETAINED_ENTRY_PREFIX}{secrets.token_hex(16)}"
            destination = directory_pin.anchor / name
            public_destination = public_directory / name
            try:
                atomic_move_no_replace(
                    path,
                    destination,
                    expected_identity=expected_identity,
                    expected_file_type=expected_file_type,
                    expected_fingerprint=expected_fingerprint,
                    pinned_source=pinned_source,
                    externally_pinned=externally_pinned,
                )
            except FileExistsError:
                continue
            except PathChangedError as error:
                detail = (
                    str(error)
                    .replace(str(destination), str(public_destination))
                    .replace(str(path), str(public_source))
                )
                raise PathChangedError(
                    detail,
                    preserved_path=(
                        public_destination if error.preserved_path is not None else None
                    ),
                ) from error
            return public_destination
    finally:
        directory_pin.close()
    raise OSError("could not allocate an unused retained-entry name")


def recover_deletion_claim(
    path: Path,
    *,
    expected_identity: PathIdentity,
    operation_token: str,
    expected_fingerprint: FileFingerprint | None = None,
    logical_path: Path | None = None,
    read_only: bool = False,
) -> ClaimedPath | None:
    """Recover an exact inode left claimed by process death during deletion.

    The strong journal token makes the location discoverable without scanning
    arbitrary hidden files. An empty claim means death happened before the
    move or after exact residue was centralised and is an idempotent no-op;
    its container is left inert because a post-creation pin is not proof that
    this process created the directory. Only exact regular-file entries are
    ever consumed or moved.
    A journal-marked zero tombstone proves descriptor truncation committed;
    unexpected contents are preserved and fail closed.

    ``read_only`` still pins and validates the bounded claim namespace, but it
    leaves a proven consumed tombstone at its exact pathname. This is the mode
    for status commands, whose observations must never perform recovery.
    """
    public_path = path if logical_path is None else logical_path
    access_directory = deletion_claim_directory(path, operation_token)
    directory = deletion_claim_directory(public_path, operation_token)
    try:
        directory_pin = _pin_private_claim_directory(access_directory)
    except FileNotFoundError:
        return None
    claimed = directory / "entry"
    try:
        directory_pin.entry.lstat()
    except FileNotFoundError:
        try:
            with os.scandir(directory_pin.anchor) as iterator:
                populated = next(iterator, None) is not None
        except OSError as error:
            directory_pin.close()
            raise OSError(f"cannot inspect empty removal claim {directory}: {error}") from error
        if populated:
            directory_pin.close()
            raise PathChangedError(
                f"removal claim {directory} contains an unexpected entry",
                preserved_path=directory,
            ) from None
        directory_pin.close()
        return None
    except OSError as error:
        directory_pin.close()
        raise OSError(f"cannot inspect removal claim entry {claimed}: {error}") from error
    if expected_fingerprint is None:
        directory_pin.close()
        raise PathChangedError(
            f"removal claim {claimed} predates regular-file generation verification",
            preserved_path=claimed,
        )
    try:
        pin = pin_regular_path(
            directory_pin.entry,
            expected_identity=expected_identity,
        )
    except OSError as error:
        directory_pin.close()
        raise PathChangedError(
            f"removal claim {claimed} does not contain the journaled file",
            preserved_path=claimed,
        ) from error
    try:
        recovered_status = pin.status()
        recovered = file_fingerprint(recovered_status)
    except Exception:
        pin.close()
        directory_pin.close()
        raise
    # Rename advances ctime, so the prepare-time ctime cannot be compared
    # after a crash. Device, inode, size and mtime are invariant across the
    # claim move; the live pin plus the mode-0700 token directory keep the
    # referenced inode from incidental recycling while replay consumes it.
    if _rename_invariant_fingerprint(recovered) != _rename_invariant_fingerprint(
        expected_fingerprint
    ):
        if _is_consumed_regular_tombstone(recovered_status, expected_fingerprint):
            try:
                # The destructive commit already happened. Centralisation is
                # best effort: failure leaves the exact zero inode (or an
                # unproven private replacement) preserved, but must not make a
                # durable journal retry an irreversible truncation forever.
                if not read_only:
                    with contextlib.suppress(OSError, ValueError):
                        _retain_exact_entry(
                            directory_pin.entry,
                            expected_identity=expected_identity,
                            expected_file_type=stat.S_IFREG,
                            expected_fingerprint=recovered,
                            pinned_source=pin,
                            retained_parent=path.parent,
                            logical_parent=public_path.parent,
                            logical_path=claimed,
                        )
            finally:
                pin.close()
                directory_pin.close()
            return None
        pin.close()
        directory_pin.close()
        raise PathChangedError(
            f"removal claim {claimed} does not contain the journaled file generation",
            preserved_path=claimed,
        )
    return ClaimedPath(
        public_path,
        claimed,
        expected_identity,
        expected_fingerprint,
        directory,
        path,
        directory_pin,
        pin,
    )


def discard_regular_if_same(
    path: Path,
    *,
    expected_identity: PathIdentity,
    expected_fingerprint: FileFingerprint | None = None,
    pinned_source: PinnedPath | None = None,
    retained_parent: Path | None = None,
    logical_retained_parent: Path | None = None,
) -> bool:
    """Best-effort logical deletion of one known regular directory entry.

    ``True`` means the expected generation left its public name and its private
    capability was consumed.  The exact inode may remain as an inert hidden
    claim because Linux cannot condition an unlink on inode identity. Absence,
    replacement, or a failure with a safe restore return ``False``. An unsafe
    restore conflict raises :class:`PathChangedError` with ``preserved_path``.
    """
    try:
        claim = claim_for_deletion(
            path,
            expected_identity=expected_identity,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
            retained_parent=retained_parent,
            logical_retained_parent=logical_retained_parent,
        )
    except PathChangedError as error:
        if error.preserved_path is not None:
            raise
        return False
    except OSError:
        return False
    try:
        claim.discard()
    except OSError as error:
        try:
            restored = claim.restore()
        except PathChangedError as restore_error:
            if restore_error.preserved_path is not None:
                raise
            restored = False
        except OSError:
            restored = False
        if (
            not restored
            and isinstance(error, PathChangedError)
            and error.preserved_path is not None
        ):
            raise
        if not restored and os.path.lexists(claim.path):
            raise PathChangedError(
                f"the private claim for {path} could not be restored",
                preserved_path=claim.path,
            ) from error
        return False
    finally:
        with contextlib.suppress(OSError):
            claim.close()
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


def read_pinned_regular_bytes(
    pinned: PinnedPath,
    maximum_bytes: int,
    *,
    expected_fingerprint: FileFingerprint | None = None,
) -> bytes:
    """Read the exact regular inode retained by an ``O_PATH`` capability."""
    before = pinned.status()
    try:
        fingerprint = file_fingerprint(before)
    except ValueError as error:
        raise FileReadError(f"{pinned.path} is not a regular file") from error
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise FileReadError(f"{pinned.path} changed before its retained read")
    if before.st_size > maximum_bytes:
        raise FileReadError(f"{pinned.path} exceeds its {maximum_bytes}-byte limit")

    access = Path("/proc/self/fd") / str(pinned.descriptor)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor = os.open(access, flags)
    except OSError as error:
        raise FileReadError(error.errno, f"cannot open retained {pinned.path}: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if file_fingerprint(opened) != fingerprint:
            raise FileReadError(f"{pinned.path} changed while its retained inode was opened")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if (
            file_fingerprint(os.fstat(descriptor)) != fingerprint
            or pinned.fingerprint != fingerprint
        ):
            raise FileReadError(f"{pinned.path} changed while its retained inode was read")
    except OSError as error:
        if isinstance(error, FileReadError):
            raise
        raise FileReadError(error.errno, f"cannot read retained {pinned.path}: {error}") from error
    finally:
        os.close(descriptor)

    data = b"".join(chunks)
    if len(data) > maximum_bytes:
        raise FileReadError(f"{pinned.path} exceeds its {maximum_bytes}-byte limit")
    return data


def hash_pinned_regular(
    pinned: PinnedPath,
    *,
    expected_fingerprint: FileFingerprint | None = None,
    maximum_bytes: int | None = None,
) -> tuple[int, str]:
    """Hash the exact regular inode retained by an ``O_PATH`` capability.

    Provider provenance is deletion authority, so a pathname hash is not
    sufficient: another actor could replace the name between opening and the
    destructive operation.  The retained descriptor prevents inode reuse,
    while full fingerprints before and after the read reject in-place writes
    (including same-size writes whose mtime is restored).
    """
    before = pinned.status()
    try:
        fingerprint = file_fingerprint(before)
    except ValueError as error:
        raise FileReadError(f"{pinned.path} is not a regular file") from error
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise FileReadError(f"{pinned.path} changed before its retained hash")
    if maximum_bytes is not None:
        if maximum_bytes < 0:
            raise ValueError("a retained hash byte ceiling cannot be negative")
        if before.st_size > maximum_bytes:
            raise FileReadError(f"{pinned.path} exceeds its {maximum_bytes}-byte hash limit")

    access = Path("/proc/self/fd") / str(pinned.descriptor)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor = os.open(access, flags)
    except OSError as error:
        raise FileReadError(error.errno, f"cannot open retained {pinned.path}: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if file_fingerprint(opened) != fingerprint:
            raise FileReadError(f"{pinned.path} changed while its retained inode was opened")
        digest = hashlib.sha256()
        size = 0
        while True:
            read_size = 1024 * 1024
            if maximum_bytes is not None:
                # Read at most one byte beyond the ceiling. That proves growth
                # without allowing an in-place appender to stream forever.
                read_size = min(read_size, maximum_bytes - size + 1)
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if maximum_bytes is not None and size > maximum_bytes:
                raise FileReadError(
                    f"{pinned.path} grew beyond its {maximum_bytes}-byte hash limit"
                )
        if (
            file_fingerprint(os.fstat(descriptor)) != fingerprint
            or pinned.fingerprint != fingerprint
        ):
            raise FileReadError(f"{pinned.path} changed while its retained inode was hashed")
    except OSError as error:
        if isinstance(error, FileReadError):
            raise
        raise FileReadError(error.errno, f"cannot hash retained {pinned.path}: {error}") from error
    finally:
        _close_descriptor_preserving_error(
            descriptor,
            f"the retained hashing stream for {pinned.path}",
        )
    return size, digest.hexdigest()


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
