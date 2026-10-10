"""What the released 0.2.2 left under its staging names is never swept (AB-1).

This version's producers publish from commit names, so one of *their* staging
names was never public. That says nothing about files 0.2.2 left: it published
straight from its staging names and moved an entry back to them when its
publication could not be verified, so such a file may have been public, and
edited while it was. Astra's FULL5 review ran the released download module as
the producer and then this app as the consumer, and the ordinary 24-hour sweep
truncated the edited file.

Here the released module (`tests.released`, the real 0.2.2 code in a child)
is the producer in each of the review's cases, and this checkout's code the
consumer. Native fine timestamps; real faults: an in-place edit of the same
inode and length while the entry is public, a one-shot EIO on the verification
read, and a hard exit for the crash-left cases.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import released
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import file_io, tidy
from wall_in_one.providers import download

MEDIA = b"validated media bytes"
#: A day and an hour on: past the sweep's wait, and Tidy up's.
LATER_SECONDS = download.STAGING_MAX_AGE_SECONDS + 3600

#: The released producer. ``argv``: the wallpaper root, and the case.
#:
#: Whichever entry the case publishes (its move from a staging name to its
#: public name) is edited in place there, same inode and length; the
#: verification read that follows fails once, so the released code moves it
#: back to its staging name. With ``-crash`` the process dies right then, as a
#: power cut would; otherwise the released code carries on through its own
#: error path, `Transfer`'s discard included.
PRODUCER = r"""
import errno
import hashlib
import os
from pathlib import Path

from wall_in_one import file_io
from wall_in_one.providers import download, http
from wall_in_one.providers.base import ProviderError

root, case = Path(sys.argv[1]), sys.argv[2]
crash = case.endswith("-crash")
entry = case.removesuffix("-crash")
location = download.MOTIONBGS_LOCATION
MEDIA = b"validated media bytes"
publishing = {
    "media": (download.MEDIA_STAGING_PREFIX, "aurora.mp4"),
    "provenance": (download.SIDECAR_STAGING_PREFIX, "aurora.mp4" + location.sidecar_suffix),
    "marker": (download.MARKER_STAGING_PREFIX, location.marker_name),
}[entry]
real_rename = file_io._rename_noreplace
real_lstat = Path.lstat
published = None
failing = None


def rename(source, destination):
    global published, failing
    real_rename(source, destination)
    source, destination = Path(source), Path(destination)
    if published is None:
        if source.name.startswith(publishing[0]) and destination.name == publishing[1]:
            published = (source, destination)
            data = destination.read_bytes()
            with destination.open("r+b") as handle:
                handle.write(bytes([data[0] ^ 0x20]) + data[1:])
                handle.flush()
                os.fsync(handle.fileno())
            failing = destination
    elif (source, destination) == (published[1], published[0]):
        print("MOVED-BACK", destination.name, flush=True)
        if crash:
            os._exit(77)


def lstat(path):
    global failing
    if failing is not None and path == failing:
        failing = None
        raise OSError(errno.EIO, "injected one-shot verification read failure")
    return real_lstat(path)


file_io._rename_noreplace = rename
Path.lstat = lstat
if entry == "marker":
    # The marker is written by the released managed_directory itself.
    try:
        download.managed_directory(root, location)
    except ProviderError as error:
        print("REFUSED", error, flush=True)
    raise SystemExit(0)

directory, _marker = download.managed_directory(root, location)
descriptor, staged = download._mkstemp(prefix=download.MEDIA_STAGING_PREFIX, directory=directory)
with os.fdopen(descriptor, "wb") as sink:
    sink.write(MEDIA)
    sink.flush()
    os.fsync(sink.fileno())
destination = directory / "aurora.mp4"
provenance = download.encode_sidecar(
    {
        "schema": 1,
        "plugin": "goober/wall-in-one",
        "provider": "MotionBGS",
        "path": str(Path(os.fspath(destination))),
        "bytes": len(MEDIA),
        "sha256": hashlib.sha256(MEDIA).hexdigest(),
    }
)
transfer = http.Transfer(
    url="https://motionbgs.com/media/42/aurora.mp4",
    status=200,
    content_type="video/mp4",
    size=len(MEDIA),
    path=staged,
)
try:
    with transfer:
        download.install(transfer.path, destination, location.sidecar_suffix, provenance)
except ProviderError as error:
    print("REFUSED", error, flush=True)
"""


def _produce(source: Path, root: Path, case: str) -> subprocess.CompletedProcess[str]:
    root.mkdir(exist_ok=True)
    done = released.run(source, PRODUCER, str(root), case)
    assert done.returncode == (77 if case.endswith("-crash") else 0), done.stderr
    assert "MOVED-BACK" in done.stdout, done.stdout + done.stderr
    return done


def _folder(root: Path) -> Path:
    return root / download.MANAGED_PARENT / download.MOTIONBGS_LOCATION.directory_name


def _left_under(folder: Path, prefix: str) -> Path:
    (left,) = (path for path in folder.iterdir() if path.name.startswith(prefix))
    return left


def _aged(path: Path) -> None:
    """As if untouched for a day and an hour (its bytes, that is: its mtime)."""
    old = time.time() - LATER_SECONDS
    os.utime(path, (old, old))


def _emptied_residue(folder: Path) -> list[Path]:
    retained = folder / file_io.RETAINED_ENTRY_DIRECTORY
    if not retained.is_dir():
        return []
    return [path for path in retained.rglob("*") if path.is_file() and path.stat().st_size == 0]


def _edited(original: bytes) -> bytes:
    return bytes([original[0] ^ 0x20]) + original[1:]


#: case -> (the released staging prefix its entry is left under, its bytes before the edit)
_MARKER = download.encode_sidecar(download.MOTIONBGS_LOCATION.marker_payload)
CASES = {
    "media": (".wall-in-one-media-stage-", MEDIA),
    "media-crash": (".wall-in-one-media-stage-", MEDIA),
    "provenance-crash": (".wall-in-one-sidecar-stage-", None),
    "marker-crash": (".wall-in-one-marker-stage-", _MARKER),
}


@pytest.mark.parametrize("case", list(CASES))
def test_an_edited_file_the_released_version_left_survives_the_new_app(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    case: str,
) -> None:
    """The review's cases: the released code publishes, the entry is edited while
    public, its verification read fails once and it is moved back to its staging
    name ("media": the released install then returns through its own error path,
    and its `Transfer` refuses to discard the changed file; "-crash": the process
    dies there instead). A day later this app opens the folder as it ordinarily
    does. The edited bytes are still there, at the same name; nothing was
    emptied; and Tidy up lists the file, offering to archive it."""
    root = tmp_path / "wallpapers"
    prefix, original = CASES[case]
    _produce(released_source, root, case)
    folder = _folder(root)
    left = _left_under(folder, prefix)
    surviving = left.read_bytes()
    if original is not None:
        assert surviving == _edited(original), "the released code left the edited bytes"
    assert surviving, "and they are not empty"
    assert stat_mode(left) == 0o600 and left.stat().st_nlink == 1
    _aged(left)

    download.managed_directory(root, download.MOTIONBGS_LOCATION)  # this app, as usual

    # Failed on 59e6a5d: the sweep claimed the name and truncated this very inode.
    assert left.read_bytes() == surviving, "the edited bytes survive, where they were"
    assert _emptied_residue(folder) == [], "nothing was emptied"
    found = tidy.plan(roots=(root,), now=time.time() + LATER_SECONDS).action(tidy.LEFTOVERS)
    change = next(change for change in found.changes if change.path == left)
    assert "an older version" in change.detail and change.size == len(surviving)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_an_explicit_recovery_leaves_what_the_released_version_left(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The same edited media file, and `recover_abandoned` called outright (as the
    review did): nothing under a released staging name is removed or emptied."""
    root = tmp_path / "wallpapers"
    _produce(released_source, root, "media")
    folder = _folder(root)
    left = _left_under(folder, ".wall-in-one-media-stage-")
    surviving = left.read_bytes()
    directory, _marker_path = download.managed_directory(root, download.MOTIONBGS_LOCATION)

    removed = download.recover_abandoned(
        directory, download.MOTIONBGS_LOCATION, now=time.time() + LATER_SECONDS
    )

    # Failed on 59e6a5d: removed == (left,), its inode emptied.
    assert removed == ()
    assert left.read_bytes() == surviving == _edited(MEDIA)
    assert _emptied_residue(folder) == []


def test_tidy_archives_a_released_staging_file_intact_and_keeps_what_is_not_one(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tidy up's offer for such a file is to archive it: its bytes move into the
    archive, and Undo moves them back. A name that only looks like one, a recent
    one, and one that isn't private are kept, with the reason."""
    import types

    root = tmp_path / "wallpapers"
    _produce(released_source, root, "media")
    folder = _folder(root)
    left = _left_under(folder, ".wall-in-one-media-stage-")
    surviving = left.read_bytes()
    lookalike = folder / ".wall-in-one-media-stage-family-video"
    lookalike.write_bytes(b"not ours")
    lookalike.chmod(0o600)
    shared = folder / ".wall-in-one-staged-abcdefgh"
    shared.write_bytes(b"two names")
    shared.chmod(0o600)
    os.link(shared, tmp_path / "another-name")
    public_mode = folder / ".wall-in-one-tmp-abcdefgh"
    public_mode.write_bytes(b"made public")
    public_mode.chmod(0o644)

    now = tidy.plan(roots=(root,)).action(tidy.LEFTOVERS)
    assert left not in {change.path for change in now.changes}, "too recent to archive"
    assert "older version's download in progress" in _reasons(now)[left]

    later = time.time() + LATER_SECONDS
    found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
    # (The released code's own refused discard also left empty claim folders
    # in the retained folder, which Tidy up archives as it always did.)
    assert {change.path for change in found.changes if change.path.parent == folder} == {left}
    reasons = _reasons(found)
    assert "doesn't recognize" in reasons[lookalike]
    assert "another name still links" in reasons[shared]
    assert "private ones Wall-in-One gives" in reasons[public_mode]

    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )
    result = tidy.apply(tidy.LEFTOVERS, found, roots=(root,))

    assert result.changed and result.archive is not None and not left.exists()
    archived = [path for path in (result.archive / "items").iterdir() if path.is_file()]
    assert [path.read_bytes() for path in archived] == [surviving]
    for kept in (lookalike, shared, public_mode):
        assert kept.exists(), kept


def _reasons(plan: tidy.ActionPlan) -> dict[Path, str]:
    return {kept.path: kept.reason for kept in plan.kept}


# -- this version's own staging names are still reclaimed -----------------------------

_CRASH_WHILE_DOWNLOADING = r"""
import os
import sys
from pathlib import Path

from wall_in_one.providers import download

directory, _marker = download.managed_directory(Path(sys.argv[1]), download.MOTIONBGS_LOCATION)
descriptor, staged = download._mkstemp(prefix=download.MEDIA_STAGING_PREFIX, directory=directory)
os.write(descriptor, b"half of a vid")
os.fsync(descriptor)
print(staged.name, flush=True)
os._exit(77)
"""


def test_a_partial_download_this_version_crash_left_is_still_reclaimed(tmp_path: Path) -> None:
    """This version stages under its own (v2) names, which were never public: a
    partial download left by a crash is reclaimed a day later, as before."""
    root = tmp_path / "wallpapers"
    root.mkdir()
    repository = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(repository / "src") + (
        os.pathsep + existing if existing else ""
    )
    done = subprocess.run(
        [sys.executable, "-c", _CRASH_WHILE_DOWNLOADING, str(root)],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 77, done.stderr
    folder = _folder(root)
    partial = folder / done.stdout.strip()
    assert partial.name.startswith(".wall-in-one-v2-media-stage-")
    assert partial.read_bytes() == b"half of a vid"
    _aged(partial)

    download.managed_directory(root, download.MOTIONBGS_LOCATION)

    assert not partial.exists(), "reclaimed"
    assert [path.name for path in folder.iterdir() if path.name.startswith(".wall-in-one-v2")] == []


# -- neither version's sweep takes the other's names ----------------------------------


def test_no_staging_or_commit_name_of_one_version_matches_the_other() -> None:
    """Prefix by prefix, in both directions: no v2 or commit name starts with a
    released staging prefix, and no released staging name starts with a v2 or
    commit prefix. So neither sweep's ``startswith`` can ever take the other's."""
    ours = (
        download.MEDIA_STAGING_PREFIX,
        download.SIDECAR_STAGING_PREFIX,
        download.MARKER_STAGING_PREFIX,
        download.COMMIT_PREFIX,
    )
    assert download.LEGACY_STAGING_PREFIXES == (
        ".wall-in-one-media-stage-",
        ".wall-in-one-sidecar-stage-",
        ".wall-in-one-marker-stage-",
        ".wall-in-one-staged-",
        ".wall-in-one-tmp-",
    )
    for new in ours:
        for old in download.LEGACY_STAGING_PREFIXES:
            assert not new.startswith(old) and not old.startswith(new), (new, old)
    assert tidy._LEGACY_STAGING_PREFIXES == download.LEGACY_STAGING_PREFIXES


#: The released *consumer*: its ordinary sweep, a day and an hour on.
_RELEASED_SWEEP = r"""
import time
from pathlib import Path

from wall_in_one.providers import download

directory, _marker = download.managed_directory(Path(sys.argv[1]), download.MOTIONBGS_LOCATION)
removed = download.recover_abandoned(
    directory, download.MOTIONBGS_LOCATION, now=time.time() + float(sys.argv[2])
)
print(len(removed), flush=True)
"""


def test_going_back_the_released_sweep_leaves_this_versions_names(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Going back to 0.2.2: its sweep (the released code, run for real) takes
    neither a v2 staging name nor a commit name, however old. (Its scanner skips
    every dot-name, ``library/scan.py`` ``_walk``, and its Tidy up only looks at
    removal folders, so they are invisible there, and stay.)"""
    root = tmp_path / "wallpapers"
    root.mkdir()
    directory, _marker_path = download.managed_directory(root, download.MOTIONBGS_LOCATION)
    folder = _folder(root)
    descriptor, staged = download._mkstemp(
        prefix=download.MEDIA_STAGING_PREFIX, directory=directory
    )
    os.write(descriptor, b"this version's partial download")
    os.close(descriptor)
    commit = folder / f"{download.COMMIT_PREFIX}0123456789abcdef"
    commit.write_bytes(b"this version's once-public file")
    commit.chmod(0o600)
    partial = folder / staged.name
    _aged(partial)
    _aged(commit)

    done = released.run(released_source, _RELEASED_SWEEP, str(root), str(LATER_SECONDS))

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "0", "the released sweep removed nothing"
    assert partial.read_bytes() == b"this version's partial download"
    assert commit.read_bytes() == b"this version's once-public file"
