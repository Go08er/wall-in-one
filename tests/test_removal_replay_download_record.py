"""A removal that dies or is interrupted, and what its replay does with the download record.

A removal withdraws the wallpaper's download record whole before it touches
the media. If the process then dies, nothing in it is left to put the record
back or destroy it: the pending-removal journal's replay has to. The record's
claim is therefore named from the removal's own token, which the journal
already holds, so the replay finds it without any field of its own:

- the media is exactly as prepared (the removal never touched it): the record
  goes back to its name, and the wallpaper is the download it was;
- the media is gone, or the replay finishes destroying it: the record goes too.

The deaths here are real: a child process is killed (``os._exit``) at a point
in a real removal, and this process then replays what it left on disk.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests import released
from tests.released import released_source  # noqa: F401
from tests.test_download_legacy_staging import _folder, _produce
from tests.test_manage import downloaded
from tests.test_session import FakeRenderer
from wall_in_one import config, file_io
from wall_in_one.library import favourites, manage, pairings, playlists, removals, scan, stills
from wall_in_one.session import Session
from wall_in_one.wallpaper.applier import Applier

#: A real removal in a child, killed at ``point`` (its exit status is 77).
_DIES_AT = r"""
import os
import sys
from pathlib import Path

from wall_in_one import file_io
from wall_in_one.library import manage, removals, scan

root, media, journal, point = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
(item,) = (found for found in scan.scan((root,)).items if found.path == media)
intent = removals.Store.open(journal).prepare(item, (root,))
if point == "before-media-claim":
    claim_for_deletion = file_io.claim_for_deletion

    def claim(path, **options):
        if options.get("operation_token") == intent.token:
            os._exit(77)
        return claim_for_deletion(path, **options)

    file_io.claim_for_deletion = claim
elif point == "before-media-discard":
    discard = file_io.ClaimedPath.discard

    def dying(claimed):
        if claimed.original == media:
            os._exit(77)
        discard(claimed)

    file_io.ClaimedPath.discard = dying
elif point == "before-consume":
    manage._WithdrawnAuthority.consume = lambda self: os._exit(77)
else:
    raise SystemExit(f"unknown point {point}")
manage.remove(
    item,
    (root,),
    expected_source=intent.source_identity,
    expected_fingerprint=intent.source_fingerprint,
    operation_token=intent.token,
)
os._exit(0)
"""

#: The same, run by the released version: its own journal, its own removal.
_RELEASED_DIES_AT = r"""
import os
from pathlib import Path

from wall_in_one import file_io
from wall_in_one.library import manage, removals, scan

root, media, journal, point = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
(item,) = (found for found in scan.scan((root,)).items if found.path == media)
intent = removals.Store.open(journal).prepare(item, (root,))
print(intent.token, flush=True)
if point == "after-prepare":
    os._exit(77)
discard = file_io.ClaimedPath.discard


def dying(claimed):
    if claimed.original == media:
        os._exit(77)
    discard(claimed)


file_io.ClaimedPath.discard = dying
manage.remove(
    item,
    (root,),
    expected_source=intent.source_identity,
    expected_fingerprint=intent.source_fingerprint,
    operation_token=intent.token,
)
os._exit(0)
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "wallpapers"
    directory.mkdir()
    return directory


def _die(root: Path, media: Path, journal: Path, point: str) -> None:
    """Run this checkout's removal in a child that is killed at ``point``."""
    project = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(project / "src") + (os.pathsep + existing if existing else "")
    done = subprocess.run(
        [sys.executable, "-c", _DIES_AT, str(root), str(media), str(journal), point],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 77, done.stderr


def _replay(tmp_path: Path, root: Path, journal: Path) -> tuple[str, ...]:
    """What the next start does: a fresh session replays the pending removals."""
    session = Session(
        replace(config.Settings(), roots=(root,)).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        favourite_store=favourites.Store.open(tmp_path / "favourites.json"),
        pairing_store=pairings.Store.open(tmp_path / "pairings.json"),
        playlist_store=playlists.Store.open(tmp_path / "playlists.json"),
        removal_store=removals.Store.open(journal),
    )
    try:
        return session.retry_removals()
    finally:
        session.shutdown()


def _whole_copies(folder: Path, written: bytes) -> list[Path]:
    """Every claim entry in ``folder`` that still holds the record's bytes."""
    claims = [
        *folder.glob(f"{file_io.DELETION_CLAIM_PREFIX}*/entry"),
        *folder.glob(f"{file_io.RETAINED_ENTRY_DIRECTORY}/*/entry"),
    ]
    return [entry for entry in claims if entry.read_bytes() == written]


def _pending(journal: Path) -> int:
    return len(removals.Store.open(journal).records)


def test_a_removal_that_dies_before_claiming_the_media_gets_its_record_back(
    tmp_path: Path, root: Path
) -> None:
    """Killed with the record withdrawn and the media untouched. The replay finds
    the media exactly as prepared, so the removal never happened: the record goes
    back to its name, and the wallpaper is the download it was."""
    item = downloaded(root)
    media = item.path
    record = media.with_name(media.name + ".wallhaven.json")
    written = record.read_bytes()
    generation = file_io.regular_file_fingerprint(media)
    journal = tmp_path / "pending-removals.json"

    _die(root, media, journal, "before-media-claim")
    assert media.is_file() and not record.exists(), "withdrawn, and then it died"
    assert len(_whole_copies(media.parent, written)) == 1 and _pending(journal) == 1

    assert _replay(tmp_path, root, journal) == ()

    # Failed on 28ea870: the record stayed in a nameless claim nothing could
    # find, and the wallpaper, untouched, was no longer a download.
    assert record.read_bytes() == written
    assert file_io.regular_file_fingerprint(media) == generation
    assert scan.has_download_sidecar(media), "still a download"
    assert _whole_copies(media.parent, written) == [] and _pending(journal) == 0
    assert sorted(path.name for path in media.parent.glob(".wall-in-one-re*")) == []


@pytest.mark.parametrize("point", ["before-media-discard", "before-consume"])
def test_a_removal_that_dies_with_the_media_claimed_or_gone_loses_its_record_too(
    tmp_path: Path, root: Path, point: str
) -> None:
    """Killed with the media in its claim (not yet destroyed), or destroyed with
    the record not yet consumed. The replay finishes the removal, and the record
    goes with the media instead of staying whole in a claim for good."""
    item = downloaded(root)
    media = item.path
    record = media.with_name(media.name + ".wallhaven.json")
    written = record.read_bytes()
    journal = tmp_path / "pending-removals.json"

    _die(root, media, journal, point)
    assert not media.exists() and not record.exists()
    assert len(_whole_copies(media.parent, written)) == 1 and _pending(journal) == 1

    assert _replay(tmp_path, root, journal) == ()

    assert not media.exists() and not record.exists()
    # Failed on 28ea870: one whole copy, in a claim no replay could find.
    assert _whole_copies(media.parent, written) == []
    assert _pending(journal) == 0
    assert _replay(tmp_path, root, journal) == (), "and a second start finds nothing to do"


def test_an_interrupt_with_the_media_in_its_claim_keeps_the_record_with_it(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C arrives while the claimed media is being destroyed. It is not an
    error of a kind that says where the media is; the media is in its claim, the
    removal is pending, and so the record stays whole in its own claim. Put back
    at its name, it would have been left beside nothing once the replay finished
    the removal."""
    item = downloaded(root)
    media = item.path
    record = media.with_name(media.name + ".wallhaven.json")
    written = record.read_bytes()
    journal_path = tmp_path / "pending-removals.json"
    journal = removals.Store.open(journal_path)
    intent = journal.prepare(item, (root,))
    discard = file_io.ClaimedPath.discard
    interrupts: list[bool] = []

    def interrupted_once(claimed: file_io.ClaimedPath) -> None:
        if claimed.original == media and not interrupts:
            interrupts.append(True)
            raise KeyboardInterrupt
        discard(claimed)

    monkeypatch.setattr(file_io.ClaimedPath, "discard", interrupted_once)
    with pytest.raises(KeyboardInterrupt):
        manage.remove(
            item,
            (root,),
            expected_source=intent.source_identity,
            expected_fingerprint=intent.source_fingerprint,
            operation_token=intent.token,
        )
    journal.close()

    kept = file_io.deletion_claim_directory(
        record, manage._record_claim_token(intent.token, ".wallhaven.json")
    )
    assert (file_io.deletion_claim_directory(media, intent.token) / "entry").is_file()
    # Failed on 28ea870: the record was put back at its name, beside no media.
    assert not media.exists() and not record.exists()
    assert (kept / "entry").read_bytes() == written

    assert _replay(tmp_path, root, journal_path) == ()

    assert not media.exists() and not record.exists()
    assert _whole_copies(media.parent, written) == [] and _pending(journal_path) == 0


def test_a_removal_finished_at_the_next_start_takes_the_wallpapers_selector_with_it(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The removal stopped with the media in its claim, before it reached the
    wallpaper's own files (here the disk was full at the sync after the move).
    The next start finishes it. What it finishes is the whole removal: the
    selector goes too, or is named as left, never left without a word beside
    no wallpaper."""
    item = downloaded(root)
    media = item.path
    other = root / "other.png"
    other.write_bytes(b"another picture")
    selector = stills.write_sidecar(media, other)
    assert selector in manage.pairing_artifact_paths(
        item, (root,), legacy_selected_still=None, adopted_authority=None
    )
    journal_path = tmp_path / "pending-removals.json"
    journal = removals.Store.open(journal_path)
    intent = journal.prepare(item, (root,))
    claim = file_io.deletion_claim_directory(media, intent.token)
    sync = file_io._fsync_directory_capability
    full: list[bool] = []

    def no_room_once(descriptor: int, **options: Any) -> None:
        if not full and options.get("logical_path") == claim and not media.exists():
            full.append(True)
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        sync(descriptor, **options)

    monkeypatch.setattr(file_io, "_fsync_directory_capability", no_room_once)
    with pytest.raises(manage.ManageError) as stopped:
        manage.remove(
            item,
            (root,),
            expected_source=intent.source_identity,
            expected_fingerprint=intent.source_fingerprint,
            operation_token=intent.token,
        )
    journal.close()
    assert stopped.value.retain_intent, "pending, with the media in its claim"
    assert (claim / "entry").is_file() and selector.is_file()

    left = _replay(tmp_path, root, journal_path)

    assert not media.exists() and not (claim / "entry").exists()
    # Failed on 651e5f1: nothing was said and the selector stayed.
    assert not selector.exists() or any(selector.name in said for said in left), left
    assert other.read_bytes() == b"another picture"
    if not left:
        assert _pending(journal_path) == 0


def _released_dies(source: Path, root: Path, media: Path, journal: Path, point: str) -> str:
    """0.2.2 prepares and starts a removal, and is killed at ``point``; its token."""
    done = released.run(source, _RELEASED_DIES_AT, str(root), str(media), str(journal), point)
    assert done.returncode == 77, done.stderr
    return done.stdout.split()[0]


@pytest.mark.parametrize("point", ["after-prepare", "before-media-discard"])
def test_a_removal_the_released_version_left_replays_as_it_always_did(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    point: str,
) -> None:
    """Rule 4. The download, the journal entry and the half-done removal are all
    0.2.2's own. It has no claim named from its token for this checkout to find:
    its record was destroyed up front, or not yet touched. The replay does what
    it did before, with no error and no claim of its own made or expected."""
    root = tmp_path / "wallpapers"
    _produce(released_source, root, "install")
    media = _folder(root) / "aurora.mp4"
    record = Path(str(media) + ".motionbgs.json")
    written = record.read_bytes()
    journal = tmp_path / "pending-removals.json"

    token = _released_dies(released_source, root, media, journal, point)
    named = manage._record_claim_token(token, ".motionbgs.json")
    assert not file_io.deletion_claim_directory(record, named).exists()
    assert _pending(journal) == 1

    assert _replay(tmp_path, root, journal) == ()

    assert _pending(journal) == 0
    assert not file_io.deletion_claim_directory(record, named).exists()
    if point == "after-prepare":
        # Never touched: cancelled, and still the download it was.
        assert record.read_bytes() == written
        assert scan.has_download_sidecar(media)
    else:
        # 0.2.2 had destroyed the record and claimed the media: finished.
        assert not media.exists() and not record.exists()
        assert _whole_copies(media.parent, written) == []
    assert released.VERSION == "0.2.2"
