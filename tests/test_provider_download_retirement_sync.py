"""Download retirements are durable at their new place before their old one (AC-3, AC-4).

A power cut can't be run, so these pin the order of the directory syncs around
each move, and what is said when one of them fails after the move: where the
entry really is, and that it could not be confirmed. The failing sync is the
one thing injected (an EIO from fsync can't be arranged); everything else is
the real filesystem.
"""

from __future__ import annotations

import errno
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from wall_in_one import file_io, paths
from wall_in_one.providers import download
from wall_in_one.providers.base import ProviderError

PAYLOAD = b"validated media bytes"
#: The sweep's clock, two days ahead of the real one: it takes only what was left
#: alone for a day by mtime and by ctime, and a test can't set a ctime back.
NOW = time.time() + 2 * download.STAGING_MAX_AGE_SECONDS
OLD = NOW - download.STAGING_MAX_AGE_SECONDS - 1

Event = tuple[str, file_io.PathIdentity | None]


def _recorded(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: Callable[[file_io.PathIdentity, int], bool] = lambda _identity, _renames: False,
) -> list[Event]:
    """Every rename and directory sync from here, in order: `file_io`'s pinned
    syncs as ("sync", identity), the sweep's own as ("sweep-sync", identity).

    ``fail(identity, renames)`` chooses a pinned sync to fail with EIO, by the
    folder it syncs and how many renames have happened.
    """
    events: list[Event] = []
    real_rename = file_io._rename_noreplace
    real_sync = file_io._fsync_directory_capability
    real_directory_sync = paths.fsync_directory

    def rename(current: Path, target: Path) -> None:
        events.append(("rename", None))
        real_rename(current, target)

    def sync(
        descriptor: int, *, expected_identity: file_io.PathIdentity, logical_path: Path
    ) -> None:
        if fail(expected_identity, events.count(("rename", None))):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        events.append(("sync", expected_identity))
        real_sync(descriptor, expected_identity=expected_identity, logical_path=logical_path)

    def directory_sync(path: Path) -> None:
        found = os.stat(path)  # followed: the sweep names its folder through a descriptor
        events.append(("sweep-sync", (found.st_dev, found.st_ino)))
        real_directory_sync(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    monkeypatch.setattr(file_io, "_fsync_directory_capability", sync)
    monkeypatch.setattr(paths, "fsync_directory", directory_sync)
    return events


def _orphaned_record(directory: Path) -> tuple[Path, bytes]:
    """A provider record whose media is gone, with a note a person added to it."""
    media = directory / "aurora.mp4"
    record = directory / (media.name + download.MOTIONBGS_LOCATION.sidecar_suffix)
    noted = json.dumps(
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": download.MOTIONBGS_LOCATION.provider,
            "path": str(media),
            "notes": "the one from the trip; keep the source link",
        },
        indent=2,
    ).encode()
    record.write_bytes(noted)
    record.chmod(0o600)
    os.utime(record, (OLD, OLD))
    return record, noted


def _kept(directory: Path) -> list[Path]:
    retained = Path(os.fspath(directory)) / file_io.RETAINED_ENTRY_DIRECTORY
    return [entry for entry in retained.iterdir() if entry.is_file()]


def test_the_sweep_makes_an_orphaned_record_durable_where_it_went_before_where_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3. The record, which was public and may hold a person's edits, is moved
    into the retained folder. The folder's own name (the provider folder), then
    the new link, are synced before the provider folder is synced for the old
    one; the sweep's own sync of the provider folder comes after all of it."""
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    record, noted = _orphaned_record(Path(os.fspath(directory)))
    events = _recorded(monkeypatch)

    removed = download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=NOW)

    (kept,) = _kept(directory)
    provider_folder = file_io.path_identity(Path(os.fspath(directory)))
    retained_folder = file_io.path_identity(kept.parent)
    # Failed on 7bdc1a6: [("rename", None), ("sweep-sync", provider folder)]: the old
    # link's withdrawal was made durable, and the new link never was.
    assert events == [
        ("sync", provider_folder),
        ("rename", None),
        ("sync", retained_folder),
        ("sync", provider_folder),
        ("sweep-sync", provider_folder),
    ]
    assert removed == (record,) and not record.exists()
    assert kept.read_bytes() == noted


def test_an_orphaned_record_whose_new_place_cannot_be_made_durable_is_said_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3. The retained folder's sync fails after the move. The sweep stops, the
    error names the retained path and says it could not be confirmed, the provider
    folder is not then synced for the old link, and the record is there, whole."""
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    record, noted = _orphaned_record(Path(os.fspath(directory)))
    provider_folder = file_io.path_identity(Path(os.fspath(directory)))
    events = _recorded(
        monkeypatch, fail=lambda identity, renames: renames == 1 and identity != provider_folder
    )

    # Failed on 7bdc1a6: no error; the record was reported retired with no sync behind it.
    with pytest.raises(ProviderError) as caught:
        download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=NOW)

    (kept,) = _kept(directory)
    assert kept.read_bytes() == noted and not record.exists()
    assert str(caught.value) == (
        "local-io: provider recovery could not finish retiring an orphaned record: "
        f"{record} was moved to {kept}, but that could not be confirmed durable "
        "(Input/output error): after a power cut it may be there, back at its old name, or both"
    )
    assert events == [("sync", provider_folder), ("rename", None)], "nothing synced after it"


@pytest.mark.parametrize("synced", [True, False])
def test_a_once_public_download_is_only_called_retired_once_that_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synced: bool
) -> None:
    """A download that was public for a moment and moved back to its commit name
    is retired intact. "Retired intact to ..." is said with the syncs behind it;
    when the retained folder's sync fails, what is said is where it is and that
    it could not be confirmed."""
    folder = tmp_path / "library"
    folder.mkdir()
    path = folder / f"{download.COMMIT_PREFIX}0123456789abcdef"
    path.write_bytes(PAYLOAD)
    fingerprint = file_io.regular_file_fingerprint(path)
    library = file_io.path_identity(folder)
    events = _recorded(
        monkeypatch,
        fail=lambda identity, renames: not synced and renames == 1 and identity != library,
    )

    with file_io.pin_regular_path(
        path, expected_identity=fingerprint[:2], expected_fingerprint=fingerprint
    ) as pinned:
        said = download._settle_relocated(path, pinned, written=PAYLOAD, what="the download")

    (kept,) = _kept(folder)
    assert kept.read_bytes() == PAYLOAD and not path.exists()
    if synced:
        # Failed on 7bdc1a6: [("rename", None)].
        assert events == [
            ("sync", library),
            ("rename", None),
            ("sync", file_io.path_identity(kept.parent)),
            ("sync", library),
        ]
        assert said.endswith(f"was retired intact to {kept}"), said
    else:
        # Failed on 7bdc1a6: "... was retired intact to <path>".
        assert said == (
            f"; what was at {path} was moved to {kept}, but that could not be confirmed "
            "durable (Input/output error): after a power cut it may be there, back at its "
            "old name, or both"
        )


@pytest.mark.parametrize("fault", ["its folder's sync fails", "its folder can't be looked into"])
def test_an_unverified_entry_put_back_is_said_to_be_back_at_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """AC-4. The sweep claims an old staging file; it is written to right then, so
    the claimed entry is no longer old and is put back. The move back lands, and
    then either the sync of its folder fails, or the folder loses its search
    permission (a native EACCES) so the move can't be verified. The error says
    where the entry is, back at its staging name, and which of the two could not
    be confirmed: not that it could not be restored "at" the claim, which is
    empty by then."""
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    folder = Path(os.fspath(directory))
    staged = folder / f"{download.MEDIA_STAGING_PREFIX}one00000"
    staged.write_bytes(PAYLOAD)
    staged.chmod(0o600)
    os.utime(staged, (OLD, OLD))
    claim_for_deletion = file_io.claim_for_deletion
    claims: list[file_io.ClaimedPath] = []

    def written_to_as_it_is_claimed(path: Path, **expected: object) -> file_io.ClaimedPath:
        claim = claim_for_deletion(path, **expected)  # type: ignore[arg-type]
        os.utime(claim.path, (NOW, NOW))  # a native change: it is not an old file any more
        claims.append(claim)
        return claim

    monkeypatch.setattr(file_io, "claim_for_deletion", written_to_as_it_is_claimed)
    provider_folder = file_io.path_identity(folder)
    sync_fails = fault == "its folder's sync fails"
    events = _recorded(
        monkeypatch,
        fail=lambda identity, renames: sync_fails and renames == 2 and identity == provider_folder,
    )
    if not sync_fails:
        rename = file_io._rename_noreplace

        def unseen_once_back(current: Path, target: Path) -> None:
            rename(current, target)
            if events.count(("rename", None)) == 2:
                folder.chmod(0o000)  # it is back, and can no longer be looked at

        monkeypatch.setattr(file_io, "_rename_noreplace", unseen_once_back)

    try:
        with pytest.raises(ProviderError) as caught:
            download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=NOW)
    finally:
        folder.chmod(0o700)

    (claim,) = claims
    message = str(caught.value)
    assert staged.read_bytes() == PAYLOAD, "it is back at its staging name"
    # Claimed, put back; and, unverified, a move back into the claim that was refused.
    assert events.count(("rename", None)) == (2 if sync_fails else 3)
    unconfirmed = (
        "that could not be confirmed durable (Input/output error)"
        if sync_fails
        else "could not be verified there"
    )
    # Failed on 7bdc1a6: "could not restore an unverified recovery entry at <the claim>".
    assert message == (
        "local-io: provider recovery could not put an unverified entry back; entry is back at "
        f"{staged}, but {unconfirmed}"
    )
    assert str(claim.path) not in message and file_io.RETAINED_ENTRY_DIRECTORY not in message
