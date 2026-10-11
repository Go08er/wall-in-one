"""A download's stages are locked for as long as it uses them; nothing takes a locked one.

The sweep judged "abandoned" by age alone, and a clock a day ahead (a jump, or
a machine suspended mid-download with a second download then started beside
it) makes a live download's stage look a day old: the sweep took it, Tidy up
archived its commit-named file, and the download was lost. A producer now
holds an advisory lock on every stage from the moment it exists until its pin
is closed, and the sweep and Tidy up leave a locked file alone.

Here a real download runs in a second process and is held at each stage, while
this process looks at the folder with its clock set ahead (never the system
clock: the sweep and Tidy up are asked what they would do then).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from wall_in_one import file_io, tidy
from wall_in_one.providers import download
from wall_in_one.providers.base import ProviderError

MEDIA = b"validated media bytes"
#: Two days: past the sweep's day and Tidy up's hour, by mtime and by ctime.
AHEAD = 2 * download.STAGING_MAX_AGE_SECONDS

#: A download as `http.download` and `install` make one, held at each stage
#: until this test says "go" (anything else, and it dies there: a crash).
_LIVE_DOWNLOAD = r"""
import hashlib
import os
import sys
from pathlib import Path

from wall_in_one.providers import download, http
from wall_in_one.providers.base import ProviderError

MEDIA = b"validated media bytes"


def held(point):
    print("AT", point, flush=True)
    if sys.stdin.readline().strip() != "go":
        os._exit(3)


location = download.MOTIONBGS_LOCATION
directory, _marker = download.managed_directory(Path(sys.argv[1]), location)
# As `http.download` stages a body: created (and locked), pinned, then written.
descriptor, staged = download._mkstemp(prefix=download.MEDIA_STAGING_PREFIX, directory=directory)
pin = download._pin_created_temporary(descriptor, staged)
with download._fdopen_owned(descriptor, "wb") as sink:
    sink.write(MEDIA[:8])
    sink.flush()
    held("the media stage, half written")
    sink.write(MEDIA[8:])
    sink.flush()
    os.fsync(sink.fileno())
transfer = http.Transfer(
    url="https://motionbgs.com/media/42/aurora.mp4",
    status=200,
    content_type="video/mp4",
    size=len(MEDIA),
    path=staged,
    _staged_pin=pin,
    _staged_fingerprint=pin.fingerprint,
)
held("the media stage, written")

to_commit_name = download._to_commit_name
pin_created_temporary = download._pin_created_temporary


def commit_named(path, pinned, fingerprint, anchor=None):
    named = to_commit_name(path, pinned, fingerprint, anchor)
    held("a commit name: " + ("the media" if "media" in path.name else "its record"))
    return named


def pinned_then_held(descriptor, path):
    made = pin_created_temporary(descriptor, path)
    held("the record's stage")
    return made


download._to_commit_name = commit_named
download._pin_created_temporary = pinned_then_held
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
try:
    with transfer:
        download.install(transfer.path, destination, location.sidecar_suffix, provenance)
    print("DONE", flush=True)
except (ProviderError, OSError) as error:
    print("REFUSED", error, flush=True)
"""

POINTS = [
    "the media stage, half written",
    "the media stage, written",
    "a commit name: the media",
    "the record's stage",
    "a commit name: its record",
]


class _Download:
    """The second process: a download in progress, held where it says it is."""

    def __init__(self, root: Path) -> None:
        repository = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(repository / "src") + (
            os.pathsep + existing if existing else ""
        )
        self.process = subprocess.Popen(
            [sys.executable, "-c", _LIVE_DOWNLOAD, str(root)],
            cwd=repository,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def said(self) -> str:
        assert self.process.stdout is not None
        line = self.process.stdout.readline().strip()
        if not line:
            assert self.process.stderr is not None
            raise AssertionError("the download ended early: " + self.process.stderr.read()[-2000:])
        return line

    def tell(self, word: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(word + "\n")
        self.process.stdin.flush()

    def end(self) -> int:
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        return self.process.wait(timeout=30)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    made = tmp_path / "wallpapers"
    made.mkdir()
    return made


@pytest.fixture
def downloading(root: Path) -> Iterator[_Download]:
    started = _Download(root)
    try:
        yield started
    finally:
        if started.process.poll() is None:
            started.process.kill()  # this test's own child, by its handle
        started.end()


def _folder(root: Path) -> Path:
    return root / download.MANAGED_PARENT / download.MOTIONBGS_LOCATION.directory_name


def _temporaries(root: Path) -> dict[Path, bytes]:
    """The download's files under names of its own: v2 stages and commit names."""
    return {
        entry: entry.read_bytes()
        for entry in _folder(root).iterdir()
        if entry.is_file() and entry.name.startswith((".wall-in-one-v2-", download.COMMIT_PREFIX))
    }


def _a_day_ahead(monkeypatch: pytest.MonkeyPatch) -> float:
    """Tidy up reads the time itself when it applies: that reading, set ahead too."""
    later = time.time() + AHEAD
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )
    return later


def _swept(root: Path, later: float) -> tuple[Path, ...]:
    directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
    return download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=later)


def test_nothing_takes_a_live_downloads_files_with_the_clock_a_day_ahead(
    root: Path, downloading: _Download, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At every stage of a download held alive in a second process, this process
    sweeps the folder and runs Tidy up's plan and apply with its clock two days
    ahead. Nothing is taken: every one of the download's files is where it was,
    whole; the commit-named one is listed as kept because a download is using
    it; and the download then completes."""
    later = _a_day_ahead(monkeypatch)
    met: list[str] = []
    while (line := downloading.said()).startswith("AT "):
        met.append(line.removeprefix("AT "))
        live = _temporaries(root)
        assert live, f"nothing of the download's to protect at {met[-1]}"

        # Failed on bf1b4b6: the sweep took the live stage at the first of these.
        assert _swept(root, later) == (), met[-1]
        found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
        offered = {change.path for change in found.changes}
        assert not offered & set(live), met[-1]
        for path in live:
            if path.name.startswith(download.COMMIT_PREFIX):
                # Failed on bf1b4b6: offered, and archived by the apply below.
                assert _reasons(found)[path] == "a download is using this file", met[-1]
        if found.changes:
            tidy.apply(tidy.LEFTOVERS, found, roots=(root,))
        assert _temporaries(root) == live, f"taken or changed at {met[-1]}"
        downloading.tell("go")

    assert met == POINTS
    assert line == "DONE", line
    media = _folder(root) / "aurora.mp4"
    record = media.with_name(media.name + download.MOTIONBGS_LOCATION.sidecar_suffix)
    assert media.read_bytes() == MEDIA
    assert json.loads(record.read_bytes())["sha256"] == hashlib.sha256(MEDIA).hexdigest()
    assert _temporaries(root) == {} and downloading.end() == 0


def _reasons(plan: tidy.ActionPlan) -> dict[Path, str]:
    return {kept.path: kept.reason for kept in plan.kept}


@pytest.mark.parametrize("died_at", ["the media stage, written", "a commit name: the media"])
def test_a_download_that_died_holds_nothing_and_its_files_are_still_tidied(
    root: Path, downloading: _Download, monkeypatch: pytest.MonkeyPatch, died_at: str
) -> None:
    """The lock ends with the process. A stage a crash left is reclaimed by the
    sweep a day later, as before, and a commit-named file a crash left is offered
    by Tidy up and archived whole."""
    later = _a_day_ahead(monkeypatch)
    while downloading.said().removeprefix("AT ") != died_at:
        downloading.tell("go")
    (left,) = _temporaries(root)
    assert _swept(root, later) == (), "held while the download lives"
    downloading.tell("die")
    assert downloading.end() == 3

    if died_at == "the media stage, written":
        assert _swept(root, later) == (left,)
        assert _temporaries(root) == {}
    else:
        assert _swept(root, later) == (), "a commit name is never the sweep's"
        found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
        assert left in {change.path for change in found.changes}
        archive = tidy.apply(tidy.LEFTOVERS, found, roots=(root,)).archive
        assert archive is not None and not left.exists()
        items = json.loads((archive / "manifest.json").read_text())["items"]
        (record,) = (item for item in items if item["original"] == str(left))
        assert (archive / record["archived"]).read_bytes() == MEDIA


# -- creating a stage and locking it are two steps -------------------------------------


def test_a_sweep_between_a_stages_creation_and_its_lock_makes_the_download_refuse(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep whose clock is a day ahead runs in the moment between a stage's
    creation and its lock: the stage is new, empty and unlocked, and the sweep
    takes it. The producer then locks an inode that is no longer at its name;
    the pin that follows compares the two and refuses. Nothing was staged."""
    directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
    later = time.time() + AHEAD
    flock = fcntl.flock
    swept: list[Path] = []
    between: list[bool] = []

    def swept_first(descriptor: int, operation: int) -> None:
        if not between:
            between.append(True)
            swept.extend(
                download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=later)
            )
        flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", swept_first)

    descriptor, staged = download._mkstemp(
        prefix=download.MEDIA_STAGING_PREFIX, directory=directory
    )
    with pytest.raises(OSError):
        download._pin_created_temporary(descriptor, staged)

    assert swept == [Path(os.fspath(staged))], "the sweep took the new, empty stage"
    assert _temporaries(root) == {}
    with pytest.raises(OSError):
        os.fstat(descriptor)  # the refusal closed the creation descriptor


def test_a_stage_a_cleanup_holds_as_it_is_created_is_refused_before_anything_is_written(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same moment: the sweep has locked the new stage (its
    liveness test, `_abandoned`) and is about to claim it when the producer tries
    to lock. The producer does not wait and does not write: it refuses."""
    directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
    flock = fcntl.flock
    held: list[int] = []
    asked: list[bool] = []

    def a_cleanup_locks_first(descriptor: int, operation: int) -> None:
        if not asked:
            asked.append(True)
            (new,) = _temporaries(root)
            unheld, liveness = download._abandoned(new, new.stat())
            assert unheld
            held.append(liveness)
        flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", a_cleanup_locks_first)
    try:
        with pytest.raises(ProviderError) as refused:
            download._mkstemp(prefix=download.MEDIA_STAGING_PREFIX, directory=directory)
    finally:
        for liveness in held:
            os.close(liveness)

    (left,) = _temporaries(root).items()
    assert left[1] == b"", "nothing was written to it"
    assert str(refused.value) == (
        f"local-io: a cleanup took the new staging file {left[0]} before it could be locked; "
        "nothing was written to it"
    )


# -- Tidy up: at the plan, and again at the apply ---------------------------------------


def _held_at_a_commit_name(root: Path) -> tuple[Path, file_io.PinnedPath]:
    """A download's file at its commit name, made and held as a download holds it."""
    directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
    descriptor, staged = download._mkstemp(
        prefix=download.MEDIA_STAGING_PREFIX, directory=directory
    )
    pin = download._pin_created_temporary(descriptor, staged)
    with download._fdopen_owned(descriptor, "wb") as sink:
        sink.write(MEDIA)
    commit, _fingerprint = download._to_commit_name(staged, pin, pin.fingerprint)
    return Path(os.fspath(commit)), pin


def test_tidy_lists_a_file_a_download_holds_as_kept_until_it_lets_go(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock follows the file from its staging name to its commit name (it
    belongs to the open file, not the name), and outlives the creation
    descriptor. While it is held, Tidy up lists the file as kept, however old
    the clock makes it look; once the pin is closed it is offered, and archived."""
    later = _a_day_ahead(monkeypatch)
    commit, pin = _held_at_a_commit_name(root)

    found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)

    # Failed on bf1b4b6: offered for archiving.
    assert _reasons(found)[commit] == "a download is using this file"
    assert commit not in {change.path for change in found.changes}

    pin.close()
    found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
    assert commit in {change.path for change in found.changes}
    assert tidy.apply(tidy.LEFTOVERS, found, roots=(root,)).changed and not commit.exists()


def test_tidy_checks_the_lock_again_when_it_applies(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview offered the file; by the time Apply is chosen, a download holds
    it. Apply surveys again under its own lock, finds the file held, and moves
    nothing: the preview no longer matches."""
    later = _a_day_ahead(monkeypatch)
    commit, pin = _held_at_a_commit_name(root)
    pin.close()
    preview = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
    assert commit in {change.path for change in preview.changes}
    holder = os.open(commit, os.O_RDONLY | os.O_CLOEXEC)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)  # as its producer holds it

        # Failed on bf1b4b6: archived.
        with pytest.raises(tidy.TidyChangedError):
            tidy.apply(tidy.LEFTOVERS, preview, roots=(root,))
    finally:
        os.close(holder)

    assert commit.read_bytes() == MEDIA
