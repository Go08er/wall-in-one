"""The one fault these tests inject: a directory sync that fails.

A real EIO from ``fsync`` can't be arranged, and a power cut can't be run, so
a test that needs a sync to fail replaces that one call; everything else it
does is the real filesystem.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from wall_in_one import file_io


def fail_the_sync_after_a_retirement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once an entry has been moved into a retained folder, that folder's sync fails.

    Installed over whatever a test already put in `file_io._rename_noreplace`,
    so install it last. Only the sync after a retirement fails: a claim made
    earlier in the same operation still syncs the retained folder for itself.
    """
    rename = file_io._rename_noreplace
    sync = file_io._fsync_directory_capability
    retired: list[bool] = []

    def observed(source: Path, destination: Path) -> None:
        rename(source, destination)
        if destination.name.startswith(file_io.RETAINED_ENTRY_PREFIX):
            retired.append(True)

    def failing(
        descriptor: int, *, expected_identity: file_io.PathIdentity, logical_path: Path
    ) -> None:
        if retired and logical_path.name == file_io.RETAINED_ENTRY_DIRECTORY:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        sync(descriptor, expected_identity=expected_identity, logical_path=logical_path)

    monkeypatch.setattr(file_io, "_rename_noreplace", observed)
    monkeypatch.setattr(file_io, "_fsync_directory_capability", failing)


#: How `file_io.UnconfirmedMoveError` says it, after "<what> was moved to <where>".
UNCONFIRMED = (
    ", but that could not be confirmed durable (Input/output error): after a power cut "
    "it may be there, back at its old name, or both"
)
