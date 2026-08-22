"""Shared, fail-closed reading for the app's small authoring documents.

The interactive stores recover to an empty or partial value so the GUI can
still open.  ``fault`` is the other half of that contract: unattended runtime
configuration publication sees it and preserves the last-known-good file.
Keeping the filesystem checks here prevents one store from accidentally
treating a symlink, FIFO, directory, or truncated JSON document as a clean
first run while the others fail closed.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


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


def preserve_faulted(path: Path) -> Path:
    """Move an unreadable state object aside without replacing any backup.

    A hard link followed by unlink gives regular files, symlinks, and other
    linkable objects a same-filesystem, no-replace move.  Directories cannot be
    hard-linked and therefore fail safely: the caller must never recursively
    relocate an unexpected directory just to make room for a JSON document.
    """
    for index in range(10_000):
        suffix = ".broken" if index == 0 else f".broken.{index}"
        backup = path.with_name(path.name + suffix)
        try:
            os.link(path, backup, follow_symlinks=False)
        except FileExistsError:
            continue
        try:
            path.unlink()
            fsync_parent(path)
        except OSError:
            # The no-replace backup is already a faithful recovery copy. Keep
            # it too; deleting it would turn a failed move into data loss.
            raise
        return backup
    raise OSError(f"could not preserve {path}: too many recovery backups")
