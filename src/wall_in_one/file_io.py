"""Small-file reads that cannot follow a link or block on a special file.

Settings, manifests and sidecars are all user-writable inputs.  A convenient
``Path.read_bytes()`` follows symbolic links and can wait forever when the path
has been replaced with a FIFO.  The helpers here make the common contract
explicit: one bounded regular file, opened without following links, and still
the same inode after it is opened.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class FileReadError(OSError):
    """A present path was not a safely readable bounded regular file."""


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
