"""Download cleanup when a deletion claim can be neither verified nor undone.

The claim's folder loses its search permission right after the entry is moved
into it (a native EACCES). Since 6f92aa5 that is not read as "the entry moved
back": it is in the claim folder, and each cleanup says where. Kept apart from
``test_provider_download.py``, whose sweep tests are being rewritten.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from wall_in_one import file_io
from wall_in_one.providers import download, http
from wall_in_one.providers.base import ProviderError

PAYLOAD = b"validated media bytes"


def _staged(directory: Path, name: str) -> Path:
    suffix = (name + "00000000")[: download.TEMPORARY_SUFFIX_LENGTH]
    path = directory / f"{download.MEDIA_STAGING_PREFIX}{suffix}"
    path.write_bytes(PAYLOAD)
    path.chmod(0o600)
    return path


def _claim_tail(folder: Path) -> str:
    """The claim entry's place within its managed folder, which each error ends with.

    Only this much: these callers reach the managed folder through a descriptor
    and give no logical parent, so the error says that folder by a stand-in.
    """
    return f"/{file_io.RETAINED_ENTRY_DIRECTORY}/{folder.name}/entry"


@pytest.fixture
def unseen_claims(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, Path]]]:
    """Each first claim loses its folder's search permission as the entry lands.

    Yields (claimed name, claim folder) for the one claim it did that to, and
    gives the folder its permission back afterwards.
    """
    rename = file_io._rename_noreplace
    lost: list[tuple[str, Path]] = []

    def move(old: Path, new: Path) -> None:
        rename(old, new)
        if not lost and new.name == "entry":
            # The claim reaches its folder through a descriptor: say its real path.
            folder = Path(os.readlink(new.parent))
            lost.append((old.name, folder))
            folder.chmod(0o000)

    monkeypatch.setattr(file_io, "_rename_noreplace", move)
    try:
        yield lost
    finally:
        for _name, folder in lost:
            folder.chmod(0o700)


def test_discarding_a_transfer_says_where_an_unseen_claim_left_it(
    tmp_path: Path, unseen_claims: list[tuple[str, Path]]
) -> None:
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory, "transfer")
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )

    # Failed on 59e6a5d: no error, and the staged file sat in the claim folder.
    with pytest.raises(ProviderError) as caught:
        transfer.discard()

    ((name, folder),) = unseen_claims
    folder.chmod(0o700)
    assert name == staged.name and not staged.exists()
    assert (folder / "entry").read_bytes() == PAYLOAD
    assert caught.value.kind == "local-io"
    assert "transfer cleanup preserved a changed entry at " in str(caught.value)
    assert str(caught.value).endswith(_claim_tail(folder)), str(caught.value)


def test_discarding_an_owned_temporary_says_where_an_unseen_claim_left_it(
    tmp_path: Path, unseen_claims: list[tuple[str, Path]]
) -> None:
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    temporary = _staged(directory, "owned")
    fingerprint = file_io.regular_file_fingerprint(temporary)

    # Failed on 59e6a5d: no error, and the temporary sat in the claim folder.
    with pytest.raises(ProviderError) as caught:
        download._discard_owned(temporary, expected_fingerprint=fingerprint)

    ((name, folder),) = unseen_claims
    folder.chmod(0o700)
    assert name == temporary.name and not temporary.exists()
    assert (folder / "entry").read_bytes() == PAYLOAD
    assert caught.value.kind == "local-io"
    assert "provider cleanup preserved a changed entry at " in str(caught.value)
    assert str(caught.value).endswith(_claim_tail(folder)), str(caught.value)


def test_recovery_stops_and_says_where_an_unseen_claim_left_a_staged_file(
    tmp_path: Path, unseen_claims: list[tuple[str, Path]]
) -> None:
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1
    stale = {path.name: path for path in (_staged(directory, "one"), _staged(directory, "two"))}
    for path in stale.values():
        os.utime(path, (old, old))

    # Failed on 59e6a5d: the sweep went on as if the first had been put back.
    with pytest.raises(ProviderError) as caught:
        download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=now)

    ((name, folder),) = unseen_claims
    folder.chmod(0o700)
    claimed = stale.pop(name)
    (other,) = stale.values()
    assert not claimed.exists() and (folder / "entry").read_bytes() == PAYLOAD
    assert other.read_bytes() == PAYLOAD, "left for the next sweep"
    assert caught.value.kind == "local-io"
    assert "provider recovery preserved a changed entry at " in str(caught.value)
    assert str(caught.value).endswith(_claim_tail(folder)), str(caught.value)
