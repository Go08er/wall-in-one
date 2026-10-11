"""A removal finished by replay, and the wallpaper's own files (review finding AD-1).

A removal that stops with the media in its claim is finished by the next
library scan or start. Since 959b00f that replay also took the wallpaper's
own files (its selector, its captured still), and it found them by name: it
removed whatever was at those names when it ran. But the wallpaper's path is
vacant while its media waits in the claim, and a download can finish there
in the meantime, with a download record, a still and a selector under the
very same names. The replay of the old removal then deleted the new
wallpaper's files.

What holds now. The replay removes one of those files only when both are
true: nothing is at the wallpaper's path, and the file is exactly what the
removal recorded when it was prepared (its generation and its contents,
`manage.CompanionProof`, kept in the pending removal record). Everything
else that is there is left as it is and named once. A pending removal
record from a version that kept no such proof (the released 0.2.2, and
0.3.0 before this) still replays: its media claim is finished, and nothing
is removed at those names.

The interruptions are real: a child process killed (``os._exit``) with the
media in its claim, or this process's own removal stopped by a failed sync
right after the move. The old versions are run for real as producers
(`tests.released`): 0.2.2 from its vendored modules, and ``a5999e2`` (the
0.3.0 build before the proof) from git wherever that commit is at hand.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import pytest

from tests import released
from tests.released import released_source  # noqa: F401 -- a fixture
from tests.test_session import FakeRenderer
from wall_in_one import config, file_io
from wall_in_one.library import favourites, manage, pairing, pairings, playlists, removals, stills
from wall_in_one.library.model import Library
from wall_in_one.providers import download, http
from wall_in_one.session import LibraryRefreshCancelledError, LibraryRefreshPlan, Session
from wall_in_one.wallpaper.applier import Applier

PNG: Final = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)
OLD: Final = b"the wallpaper that was removed"
NEW: Final = b"another download, finished at the same path while the claim waited"
#: This checkout, and the 0.3.0 build before the proof, as producers.
THIS: Final = Path(__file__).resolve().parents[1] / "src"
BEFORE_THE_PROOF: Final = "a5999e2"
#: Every key of a pending removal record as ``a5999e2`` (and 0.2.2) wrote it.
KEYS_BEFORE_THE_PROOF: Final = frozenset(
    {
        "identity",
        "path",
        "kind",
        "roots",
        "scene",
        "provider",
        "token",
        "external",
        "committed",
        "device",
        "inode",
        "source_size",
        "source_mtime_ns",
        "source_ctime_ns",
        "source_root",
        "root_device",
        "root_inode",
        "parent_device",
        "parent_inode",
    }
)

#: One version, whichever its ``wall_in_one`` is: it downloads a video, takes a
#: still of it (which writes the selector), prepares its removal, and is
#: killed with the media in its claim. Only calls every version here has.
_STOPS_AT_THE_CLAIM: Final = (
    r"""
import hashlib
import os
from pathlib import Path

from wall_in_one import file_io
from wall_in_one.library import manage, removals, scan, stills
from wall_in_one.providers import download, http

root, journal, data = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].encode()
PNG = bytes.fromhex("""
    + repr(PNG.hex())
    + r""")


def _frame(video, target, seek, *, processes=None):
    with open(target, "wb") as sink:
        sink.write(PNG + b"a frame of the old video")
    return ""


stills._run = _frame
stills.is_available = lambda: True
location = download.MOTIONBGS_LOCATION
directory, _marker = download.managed_directory(root, location)
descriptor, staged = download._mkstemp(prefix=download.MEDIA_STAGING_PREFIX, directory=directory)
with os.fdopen(descriptor, "wb") as sink:
    sink.write(data)
media = directory / "aurora.mp4"
provenance = download.encode_sidecar(
    {
        "schema": 1,
        "plugin": "goober/wall-in-one",
        "provider": "MotionBGS",
        "path": str(media),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
)
transfer = http.Transfer(
    url="https://motionbgs.com/media/42/aurora.mp4",
    status=200,
    content_type="video/mp4",
    size=len(data),
    path=staged,
)
with transfer:
    download.install(transfer.path, media, location.sidecar_suffix, provenance)
stills.generate(media, root)
(item,) = (found for found in scan.scan((root,)).items if found.path == media)
intent = removals.Store.open(journal).prepare(item, (root,))
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
)


def stub_capture(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """ffmpeg's part of a capture, and only that; what it draws can be changed."""
    drawn = [b"a frame of the old video"]

    def _frame(_video: Path, target: Path, _seek: float, *, processes: Any = None) -> str:
        target.write_bytes(PNG + drawn[0])
        return ""

    monkeypatch.setattr(stills, "_run", _frame)
    monkeypatch.setattr(stills, "is_available", lambda: True)
    return drawn


@pytest.fixture(autouse=True)
def frame(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    return stub_capture(monkeypatch)


@pytest.fixture(scope="session")
def before_the_proof(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """``a5999e2``'s ``src``, from git; skipped where this copy has no history (the gates)."""
    repository = Path(__file__).resolve().parents[1]
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available here")
    known = subprocess.run(
        [git, "-C", str(repository), "cat-file", "-e", f"{BEFORE_THE_PROOF}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    if known.returncode != 0:
        pytest.skip(f"this copy has no {BEFORE_THE_PROOF} to run")
    destination = tmp_path_factory.mktemp(f"src-{BEFORE_THE_PROOF}")
    archive = subprocess.run(
        [git, "-C", str(repository), "archive", "--format=tar", BEFORE_THE_PROOF, "src"],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as packed:
        packed.extractall(destination, filter="data")
    yield destination / "src"


@dataclass(frozen=True, slots=True)
class _Wallpaper:
    """A downloaded video and what the app keeps for it, by path."""

    root: Path
    media: Path

    @property
    def record(self) -> Path:
        return self.media.with_name(self.media.name + ".motionbgs.json")

    @property
    def selector(self) -> Path:
        return self.media.with_name(self.media.name + pairing.SIDECAR_SUFFIX)

    @property
    def still(self) -> Path:
        return stills.destination(self.media, self.root)

    @property
    def own(self) -> tuple[Path, Path]:
        """The files replay used to find by name: the selector and the still."""
        return self.selector, self.still


def _wallpaper(root: Path) -> _Wallpaper:
    directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
    return _Wallpaper(root, directory / "aurora.mp4")


def _download(root: Path, data: bytes) -> _Wallpaper:
    """This checkout's download, to the end: the media, its record, the marker."""
    location = download.MOTIONBGS_LOCATION
    wallpaper = _wallpaper(root)
    descriptor, staged = download._mkstemp(
        prefix=download.MEDIA_STAGING_PREFIX, directory=wallpaper.media.parent
    )
    with os.fdopen(descriptor, "wb") as sink:
        sink.write(data)
    provenance = download.encode_sidecar(
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": "MotionBGS",
            "path": str(wallpaper.media),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=len(data),
        path=staged,
    )
    with transfer:
        download.install(staged, wallpaper.media, location.sidecar_suffix, provenance)
    return wallpaper


def _session(tmp_path: Path, root: Path, journal: Path, first: Path | None = None) -> Session:
    """A session on ``root``; with ``first``, on that folder and then ``root``."""
    roots = (root,) if first is None else (first, root)
    return Session(
        replace(config.Settings(), roots=roots, scan_workshop=False).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        favourite_store=favourites.Store.open(tmp_path / "favourites.json"),
        pairing_store=pairings.Store.open(tmp_path / "pairings.json"),
        playlist_store=playlists.Store.open(tmp_path / "playlists.json"),
        removal_store=removals.Store.open(journal),
    )


def _replay(
    tmp_path: Path, root: Path, journal: Path, first: Path | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What the next library scan or start does: (what could not finish, what was left).

    The second is read loosely so that, run on a version before the fix, a
    test fails on what was deleted and not on an attribute that is not there.
    """
    session = _session(tmp_path, root, journal, first)
    try:
        failures = session.retry_removals()
        return failures, tuple(getattr(session, "removal_replay_left", ()))
    finally:
        session.shutdown()


def _killed_at_the_claim(source: Path, root: Path, journal: Path) -> _Wallpaper:
    """``source``'s version downloads, captures, starts the removal and dies at the claim."""
    done = released.run(source, _STOPS_AT_THE_CLAIM, str(root), str(journal), OLD.decode())
    assert done.returncode == 77, done.stderr
    return _wallpaper(root)


def _stopped_by_a_failed_sync(
    tmp_path: Path,
    root: Path,
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    first: Path | None = None,
    captured: Callable[[Path], None] = lambda _still: None,
) -> _Wallpaper:
    """This checkout's removal, as the window runs it, stopped by a full disk at the
    sync right after the media moved into its claim. Nothing dies.

    ``first``: another library folder, listed first, where the still is
    captured. ``captured(still)`` runs once the still is there, before the
    removal."""
    wallpaper = _download(root, OLD)
    captured(stills.generate(wallpaper.media, root if first is None else first))
    sync = file_io._fsync_directory_capability
    full: list[bool] = []

    def no_room_once(descriptor: int, **options: Any) -> None:
        if not full and not wallpaper.media.exists():
            full.append(True)
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        sync(descriptor, **options)

    session = _session(tmp_path, root, journal, first)
    try:
        session.refresh()
        (listed,) = (found for found in session.library.items if found.path == wallpaper.media)
        with monkeypatch.context() as patched:
            patched.setattr(file_io, "_fsync_directory_capability", no_room_once)
            result = session.prepare_removal_plan(listed, trash=False).run()
    finally:
        session.shutdown()
    assert full and not result.committed, result
    assert "its pending removal record was retained" in (result.error_message or "")
    return wallpaper


def _stopped(
    how: str, tmp_path: Path, root: Path, journal: Path, monkeypatch: pytest.MonkeyPatch
) -> _Wallpaper:
    root.mkdir(exist_ok=True)
    wallpaper = (
        _killed_at_the_claim(THIS, root, journal)
        if how == "killed"
        else _stopped_by_a_failed_sync(tmp_path, root, journal, monkeypatch)
    )
    _is_stopped_at_the_claim(wallpaper, journal)
    return wallpaper


def _claimed(folder: Path) -> list[bytes]:
    """What each removal claim in ``folder`` still holds."""
    return sorted(
        entry.read_bytes()
        for entry in folder.glob(f"{file_io.DELETION_CLAIM_PREFIX}*/entry")
        if entry.stat().st_size
    )


def _is_stopped_at_the_claim(wallpaper: _Wallpaper, journal: Path) -> None:
    assert not wallpaper.media.exists(), "its path is vacant"
    assert OLD in _claimed(wallpaper.media.parent), "the media is whole in its claim"
    assert all(path.is_file() for path in wallpaper.own), "its own files were not reached"
    assert len(_records(journal)) == 1


def _records(journal: Path) -> list[dict[str, Any]]:
    document = json.loads(journal.read_text(encoding="utf-8"))
    assert document["version"] == 1
    saved: list[dict[str, Any]] = document["removals"]
    return saved


def _snapshot(*paths: Path) -> dict[Path, tuple[bytes | None, int]]:
    """Each file's bytes and inode: what "the same file, byte for byte" means here.

    None for one that is not there, so a deleted file fails a comparison.
    """
    return {
        path: (path.read_bytes(), path.stat().st_ino) if path.is_file() else (None, 0)
        for path in paths
    }


def a_download_finishes(root: Path, frame: list[bytes], *, recaptured: bool) -> list[Path]:
    """`_replacement`, for a campaign: at ``root``'s one downloaded video (`_wallpaper`)."""
    return _replacement(_wallpaper(root), frame, recaptured=recaptured)


def _replacement(wallpaper: _Wallpaper, frame: list[bytes], *, recaptured: bool) -> list[Path]:
    """A download finishes at the vacant path: this checkout's own, for real.

    It finds the removed wallpaper's still and selector at the names it would
    use and keeps them as its own without rewriting either (a capture reuses
    a still that is there, and never writes over a selector). ``recaptured``:
    instead a person had cleared those two out, so the new wallpaper's
    capture writes both itself: new files under the same names, the still
    another picture. Returns everything that is the new wallpaper's.
    """
    before = {path: file_io.regular_file_fingerprint(path) for path in wallpaper.own}
    pictured = wallpaper.still.read_bytes()
    replaced = _download(wallpaper.root, NEW)
    assert replaced.media == wallpaper.media and replaced.media.read_bytes() == NEW
    if recaptured:
        for path in wallpaper.own:
            path.unlink()
        frame[0] = b"a frame of the new video, which is another picture"
    assert stills.generate(replaced.media, wallpaper.root) == wallpaper.still
    after = {path: file_io.regular_file_fingerprint(path) for path in wallpaper.own}
    if recaptured:
        # By its times too: a file system may give a new file an old inode.
        assert all(after[path] != before[path] for path in wallpaper.own), "new files"
        assert wallpaper.still.read_bytes() != pictured, "another picture"
    else:
        assert after == before, "kept as its own, not a byte rewritten"
    return [replaced.media, replaced.record, *wallpaper.own]


def _is_finished(wallpaper: _Wallpaper, journal: Path) -> None:
    """The old removal is over: both claims consumed, and its pending record gone."""
    assert _claimed(wallpaper.media.parent) == [], "the claimed media and record were consumed"
    assert _records(journal) == []


# -- the finding ------------------------------------------------------------------------------


@pytest.mark.parametrize("recaptured", [True, False], ids=["its-own-new-files", "kept-in-place"])
@pytest.mark.parametrize("how", ["killed", "sync-failed"])
def test_a_download_finished_at_the_same_path_keeps_its_files_through_the_old_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frame: list[bytes],
    how: str,
    recaptured: bool,
) -> None:
    """The report's trigger. An old removal waits with its media in the claim; a
    download finishes at the same path, with its record, a selector and a still
    under the same names; then the old removal is replayed.

    The new wallpaper keeps all four files, the same files byte for byte,
    whether its still and selector are new ones or the ones it found in
    place. The old claim is consumed and the journal is empty."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = _stopped(how, tmp_path, root, journal, monkeypatch)
    mine = _snapshot(*_replacement(wallpaper, frame, recaptured=recaptured))

    failures, left = _replay(tmp_path, root, journal)

    # Failed on a5999e2: the record, the selector and the still were deleted
    # (found by name), leaving the new video alone in its folder.
    assert _snapshot(*mine) == mine
    _is_finished(wallpaper, journal)
    assert failures == ()
    # Said once, with why, naming what is there; never the download record,
    # which this removal only ever looks for in its own claim.
    (said,) = left
    assert said.startswith("aurora: another file is at the wallpaper's path now, so what is")
    assert "left as it is" in said and "left untouched" not in said
    assert str(wallpaper.selector) in said and str(wallpaper.still) in said
    assert str(wallpaper.record) not in said
    assert _replay(tmp_path, root, journal) == ((), ()), "and never again"
    assert _snapshot(*mine) == mine


@pytest.mark.parametrize("change", ["edited", "saved-anew"])
@pytest.mark.parametrize("how", ["killed", "sync-failed"])
def test_a_file_changed_while_the_claim_waited_is_left_though_the_path_is_vacant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, change: str
) -> None:
    """A vacant path is not proof. While the claim waits, the still and the
    selector are changed (in place, or saved anew under a new inode), by a
    person, or by a download whose media was then taken away again. Nothing is
    at the wallpaper's path when the replay runs, and what is at its names is
    still not what the removal prepared: left, and named once."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = _stopped(how, tmp_path, root, journal, monkeypatch)
    for path in wallpaper.own:
        if change == "edited":
            with path.open("ab") as sink:
                sink.write(b"\n")
        else:
            fresh = path.with_name(path.name + ".new")
            fresh.write_bytes(path.read_bytes() + b"\n")
            os.replace(fresh, path)
    mine = _snapshot(*wallpaper.own)

    failures, left = _replay(tmp_path, root, journal)

    # Failed on a5999e2: both were deleted.
    assert _snapshot(*mine) == mine
    assert not wallpaper.media.exists()
    _is_finished(wallpaper, journal)
    assert failures == ()
    assert left == (
        "aurora: 2 of the wallpaper's own files are not exactly what this removal prepared, "
        f"so they were left as they are: {wallpaper.selector}, {wallpaper.still}",
    )
    assert _replay(tmp_path, root, journal) == ((), ())


def test_the_recorded_times_with_other_bytes_are_not_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generation is device, inode, size and two times; on a file system
    with coarse times an edit of the same length can leave all five as they
    were. So the contents are part of the proof. Staged here by giving the
    pending record the edited file's own times: everything about the file
    matches its record except its bytes."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = _stopped("sync-failed", tmp_path, root, journal, monkeypatch)
    selector = wallpaper.selector
    selector.write_bytes(selector.read_bytes().replace(b"still_path", b"STILL_PATH"))
    document = json.loads(journal.read_text(encoding="utf-8"))
    (proof,) = (
        proof for proof in document["removals"][0]["companions"] if proof["path"] == str(selector)
    )
    status = selector.stat()
    assert (proof["device"], proof["inode"], proof["size"]) == (
        status.st_dev,
        status.st_ino,
        status.st_size,
    ), "the same file, the same length"
    proof["mtime_ns"], proof["ctime_ns"] = status.st_mtime_ns, status.st_ctime_ns
    journal.write_text(json.dumps(document), encoding="utf-8")
    edited = _snapshot(selector)

    failures, left = _replay(tmp_path, root, journal)

    assert _snapshot(selector) == edited
    assert not wallpaper.still.exists(), "the still was exactly what was recorded"
    assert failures == ()
    assert left == (
        "aurora: one of the wallpaper's own files is not exactly what this removal "
        f"prepared, so it was left as it is: {selector}",
    )
    _is_finished(wallpaper, journal)


def test_a_pending_record_cannot_name_a_file_that_is_not_the_wallpapers_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof says which generation of one of the wallpaper's own names is
    the removal's. It is never a list of paths to delete: a record naming a
    picture of the person's, with that picture's exact generation and
    contents, removes nothing."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = _stopped("sync-failed", tmp_path, root, journal, monkeypatch)
    theirs = root / "holiday.png"
    theirs.write_bytes(PNG + b"a picture of the person's own")
    status = theirs.stat()
    document = json.loads(journal.read_text(encoding="utf-8"))
    document["removals"][0]["companions"].append(
        {
            "path": str(theirs),
            "device": status.st_dev,
            "inode": status.st_ino,
            "size": status.st_size,
            "mtime_ns": status.st_mtime_ns,
            "ctime_ns": status.st_ctime_ns,
            "sha256": hashlib.sha256(theirs.read_bytes()).hexdigest(),
        }
    )
    journal.write_text(json.dumps(document), encoding="utf-8")
    kept = _snapshot(theirs)

    assert _replay(tmp_path, root, journal) == ((), ())

    assert _snapshot(theirs) == kept
    assert not any(path.exists() for path in wallpaper.own)
    _is_finished(wallpaper, journal)


def test_a_path_that_cannot_be_looked_at_is_not_a_vacant_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether anything is at the wallpaper's path could not be told (an I/O
    error on the look). That is not "nothing is there": nothing is removed,
    and it is said as what it is."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = _stopped("sync-failed", tmp_path, root, journal, monkeypatch)
    mine = _snapshot(*wallpaper.own)
    presence = file_io.entry_presence

    def unlookable(path: Path, **options: Any) -> bool | OSError:
        if path.name == wallpaper.media.name:
            return OSError(errno.EIO, os.strerror(errno.EIO))
        return presence(path, **options)

    with monkeypatch.context() as patched:
        patched.setattr(file_io, "entry_presence", unlookable)
        failures, left = _replay(tmp_path, root, journal)

    assert _snapshot(*mine) == mine
    assert failures == ()
    assert left == (
        "aurora: whether another file is at the wallpaper's path now could not be checked, "
        f"so what is there was left as it is: {wallpaper.selector}, {wallpaper.still}",
    )
    _is_finished(wallpaper, journal)


def test_a_still_kept_in_another_library_folder_is_proven_and_goes_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Captured stills go under the first library folder, whichever folder
    the video is in (no selector is written for one kept elsewhere). The
    proof names the still by its own path, and the replay reaches it through
    that folder."""
    first, root, journal = tmp_path / "first", tmp_path / "second", tmp_path / "journal.json"
    first.mkdir()
    root.mkdir()
    wallpaper = _stopped_by_a_failed_sync(tmp_path, root, journal, monkeypatch, first=first)
    still = stills.destination(wallpaper.media, first)
    assert still.is_file() and not wallpaper.media.exists()
    (record,) = _records(journal)
    assert [proof["path"] for proof in record["companions"]] == [str(still)]

    assert _replay(tmp_path, root, journal, first) == ((), ())

    assert not still.exists()
    _is_finished(wallpaper, journal)


def test_a_superseded_scan_still_delivers_what_its_replay_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame: list[bytes]
) -> None:
    """A scan's reconciliation stops when a newer scan supersedes it. One whose
    replay has just finished a removal and left something runs on instead: the
    pending record is gone, so no later replay would say it. With nothing of
    that kind to say, it stops as before."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = _stopped("sync-failed", tmp_path, root, journal, monkeypatch)
    _replacement(wallpaper, frame, recaptured=True)
    session = _session(tmp_path, root, journal)
    retry = Session.retry_removals
    plans: list[LibraryRefreshPlan] = []

    def superseded_meanwhile(replaying: Session, **options: Any) -> tuple[str, ...]:
        try:
            return retry(replaying, **options)
        finally:
            plans[-1].cancel()

    monkeypatch.setattr(Session, "retry_removals", superseded_meanwhile)
    try:
        plans.append(session.prepare_library_refresh())
        result = plans[-1].reconcile(Library((root,), ()))

        (said,) = result.replay_left
        assert str(wallpaper.selector) in said and _records(journal) == []

        plans.append(session.prepare_library_refresh())
        with pytest.raises(LibraryRefreshCancelledError):
            plans[-1].reconcile(Library((root,), ()))
    finally:
        session.shutdown()


# -- what 959b00f fixed stays fixed -----------------------------------------------------------


@pytest.mark.parametrize("how", ["killed", "sync-failed"])
def test_a_removal_nothing_followed_is_finished_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """No download, no edit: the replay finishes the removal and takes the still
    and the selector with it, proven by the record this version prepared. Not
    a word is needed, and the pending record is gone. (As on a5999e2, where
    it found them by name; the proof must not undo that.)"""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = _stopped(how, tmp_path, root, journal, monkeypatch)

    assert _replay(tmp_path, root, journal) == ((), ())

    assert not wallpaper.media.exists() and not wallpaper.record.exists()
    # Failed on 651e5f1 (before 959b00f): both stayed, beside no wallpaper.
    assert not wallpaper.selector.exists() and not wallpaper.still.exists()
    _is_finished(wallpaper, journal)
    assert _replay(tmp_path, root, journal) == ((), ())


# -- what could not be removed, as before: said once, never retried ---------------------------


def _could_not_remove(wallpaper: _Wallpaper, *kept: Path) -> tuple[str, ...]:
    return (f"aurora: pairing files could not be removed: {', '.join(p.name for p in kept)}",)


def test_a_proven_file_that_cannot_be_removed_is_said_once_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The still is exactly what the removal prepared, and it can't be destroyed
    (it is read-only). As for a removal that isn't replayed, that is a failure,
    in the same words. And as before this change (the review's backlog note):
    the replay marked the removal committed first, so the next one clears what
    is saved and does not look for the still again."""
    if os.geteuid() == 0:
        pytest.skip("root writes to a read-only file")
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    root.mkdir()
    wallpaper = _stopped_by_a_failed_sync(
        tmp_path, root, journal, monkeypatch, captured=lambda still: still.chmod(0o444)
    )
    kept = _snapshot(wallpaper.still)

    failures, left = _replay(tmp_path, root, journal)

    assert failures == _could_not_remove(wallpaper, wallpaper.still)
    assert left == () and not wallpaper.selector.exists()
    assert _snapshot(wallpaper.still) == kept
    (pending,) = _records(journal)
    assert pending["committed"] is True
    assert _replay(tmp_path, root, journal) == ((), ())
    assert _snapshot(wallpaper.still) == kept and _records(journal) == []


def test_nothing_is_touched_while_the_files_locks_cannot_be_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture or a download holds the wallpaper's lifecycle locks for longer
    than the replay waits. Nothing of the wallpaper's own is touched then, the
    proven files are said as not removed, and the same holds afterwards as for
    any file a replay could not remove."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = _stopped("sync-failed", tmp_path, root, journal, monkeypatch)
    mine = _snapshot(*wallpaper.own)

    def busy(_path: Path, **_options: Any) -> contextlib.AbstractContextManager[None]:
        raise stills.LifecycleBusy

    with monkeypatch.context() as patched:
        patched.setattr(stills, "media_path_lifecycle_lock", busy)
        failures, left = _replay(tmp_path, root, journal)

    assert _snapshot(*mine) == mine
    assert failures == _could_not_remove(wallpaper, *wallpaper.own) and left == ()
    assert _replay(tmp_path, root, journal) == ((), ())
    assert _snapshot(*mine) == mine and _records(journal) == []


# -- replay's reach is not widened ------------------------------------------------------------


@pytest.mark.parametrize("committed", [False, True], ids=["media-gone", "marked-committed"])
def test_only_the_replay_that_finishes_a_media_claim_touches_the_wallpapers_own_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    """As before this change, and pinned so the proof does not widen it. A
    removal whose media is already destroyed when it is replayed (it stopped
    after the commit point), and one already marked committed, clear what is
    saved about the wallpaper and nothing else: the still and the selector
    are not looked for, though this version's record could prove them."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    root.mkdir()
    wallpaper = _download(root, OLD)
    stills.generate(wallpaper.media, root)
    session = _session(tmp_path, root, journal)
    try:
        session.refresh()
        (listed,) = (found for found in session.library.items if found.path == wallpaper.media)
        store = session.removal_journal
        intent = store.prepare(listed, (root,))

        def interrupted(*_arguments: object) -> manage.Removal:
            raise KeyboardInterrupt

        with monkeypatch.context() as patched:
            # Stopped right after the media was destroyed, before its own files.
            patched.setattr(manage, "_completed_removal_outcome", interrupted)
            with pytest.raises(KeyboardInterrupt):
                manage.remove(
                    listed,
                    (root,),
                    expected_source=intent.source_identity,
                    expected_fingerprint=intent.source_fingerprint,
                    operation_token=intent.token,
                )
        if committed:
            store.mark_committed(intent)
        store.close()
    finally:
        session.shutdown()
    assert not wallpaper.media.exists() and OLD not in _claimed(wallpaper.media.parent)
    mine = _snapshot(*wallpaper.own)

    failures, _left = _replay(tmp_path, root, journal)

    assert failures == ()
    assert _snapshot(*mine) == mine
    assert _records(journal) == []


# -- what older versions left -----------------------------------------------------------------


def _replays_an_older_record(
    tmp_path: Path, source: Path, frame: list[bytes], *, followed: bool
) -> None:
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    root.mkdir()
    wallpaper = _killed_at_the_claim(source, root, journal)
    _is_stopped_at_the_claim(wallpaper, journal)
    (record,) = _records(journal)
    assert record.keys() == KEYS_BEFORE_THE_PROOF, "its own journal, with no proof in it"
    kept = [*wallpaper.own]
    if followed:
        kept = _replacement(wallpaper, frame, recaptured=True)
    mine = _snapshot(*kept)

    failures, left = _replay(tmp_path, root, journal)

    # Failed on a5999e2 itself: its replay deleted these, found by name.
    assert _snapshot(*mine) == mine
    _is_finished(wallpaper, journal)
    assert failures == ()
    (said,) = left
    if followed:
        assert "another file is at the wallpaper's path now" in said
    else:
        assert not wallpaper.media.exists()
        assert (
            "this removal was prepared by an older version, which kept no proof of the "
            "wallpaper's own files, so what is there was left as it is: "
        ) in said
    assert str(wallpaper.selector) in said and str(wallpaper.still) in said
    assert _replay(tmp_path, root, journal) == ((), ())
    assert _snapshot(*mine) == mine


@pytest.mark.parametrize("followed", [False, True], ids=["alone", "then-a-download"])
def test_a_removal_the_released_version_left_at_its_claim_is_finished_without_a_guess(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    frame: list[bytes],
    followed: bool,
) -> None:
    """Rule 4. The download, the still, the selector, the journal and the
    half-done removal are all 0.2.2's own. Its pending record proves nothing
    about the still and the selector, so they stay where they are, named
    once: this version finishes the media claim 0.2.2 left and no more."""
    _replays_an_older_record(tmp_path, released_source, frame, followed=followed)
    assert released.VERSION == "0.2.2"


@pytest.mark.parametrize("followed", [False, True], ids=["alone", "then-a-download"])
def test_a_removal_the_build_before_the_proof_left_at_its_claim_is_finished_without_a_guess(
    before_the_proof: Path, tmp_path: Path, frame: list[bytes], followed: bool
) -> None:
    """The same for ``a5999e2``, the 0.3.0 build whose replay found these files
    by name, run for real from git. Its own record of its own download
    record's claim is honoured as before; the still and the selector stay."""
    _replays_an_older_record(tmp_path, before_the_proof, frame, followed=followed)


def test_a_record_as_the_build_before_the_proof_wrote_it_proves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the gates run in place of the test above, where there is no git:
    this version's own interrupted removal, with its record cut back to
    exactly the keys ``a5999e2`` wrote. Read as that version's, it removes
    neither file, though both are untouched and nothing is at the path."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = _stopped("sync-failed", tmp_path, root, journal, monkeypatch)
    document = json.loads(journal.read_text(encoding="utf-8"))
    (record,) = document["removals"]
    assert record.keys() - KEYS_BEFORE_THE_PROOF == {"companions"}
    del record["companions"]
    journal.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    mine = _snapshot(*wallpaper.own)

    failures, left = _replay(tmp_path, root, journal)

    assert _snapshot(*mine) == mine
    assert not wallpaper.media.exists()
    _is_finished(wallpaper, journal)
    assert failures == ()
    (said,) = left
    assert "prepared by an older version" in said
    assert _replay(tmp_path, root, journal) == ((), ())
