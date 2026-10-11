"""The clock jumps a day forward, or back, while downloads and their leftovers are in a folder.

The download sweep and Tidy up compare a file's time with "now". Here "now"
is moved (the time source the code reads; never the system clock):

- Forward a day while a download is in progress in a second process (ctl
  beside the window), held alive at each of its changes
  (`released_faults.paused`): this process opens the folder, which sweeps.
  Whatever the sweep does to the live download's stages, nothing short or
  half-made may reach a public name, and the download must end by saying so.
- Back a day: a stage written while the clock was ahead looks recent. It is
  kept for now, and reclaimed once the clock has passed it by a day: nothing
  is "recent" for ever.
- An edit restarts the day: a stage of this version's that a person edited is
  reclaimed a day after the edit, whatever its modification time is set to.
"""

from __future__ import annotations

import json
import os
import time
import types
from collections import Counter
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_download as downloads
from tests.turns import LATER_SECONDS, SOURCE, _root
from wall_in_one import tidy
from wall_in_one.providers import download

DAY = 24 * 3600
MEDIA = b"validated media bytes"


def _clock(monkeypatch: pytest.MonkeyPatch, ahead: float) -> None:
    """What `providers.download` reads as the time, ``ahead`` seconds from now."""
    names = {name: getattr(time, name) for name in dir(time) if not name.startswith("_")}
    names["time"] = lambda: time.time() + ahead
    monkeypatch.setattr(download, "time", types.SimpleNamespace(**names))


def _temporaries(root: Path) -> dict[Path, bytes]:
    return {
        entry.path: entry.data
        for entry in faults.tree(root).entries
        if entry.path.name.startswith(".wall-in-one-") and "managed" not in entry.path.name
    }


def _a_day_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, act: str
) -> tuple[Counter[str], list[str], int]:
    """A download held alive at each of its changes in a second process while
    this one, its clock a day ahead, sweeps the folder or applies Tidy up.
    What happened by kind, what went wrong, and how often a live stage went."""
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    plain = faults.Job(tmp_path / "plain", {}, ())
    downloads._prepare(plain.base)
    (alone,) = faults.run(SOURCE, downloads.PRODUCER, [plain])
    assert alone.status == 0 and alone.output == "DONE", alone.output + alone.errors[-1000:]
    points = alone.calls if os.environ.get(faults.FULL) else alone.changes
    outcomes: Counter[str] = Counter()
    problems: list[str] = []
    interfered = 0
    for call in points:
        job = faults.Job(tmp_path / f"p{call.index:05d}", {}, ())
        downloads._prepare(job.base)
        root = Path(faults.profile_environment(job.base)["HOME"]) / "Wallpapers"
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            with faults.paused(SOURCE, downloads.PRODUCER, job, call.index) as theirs:
                live = _temporaries(root)
                later = time.time() + DAY + 3600
                try:
                    if act == "sweep":
                        _clock(scoped, DAY + 3600)
                        download.managed_directory(root, download.MOTIONBGS_LOCATION)
                    else:
                        scoped.setattr(
                            tidy,
                            "time",
                            types.SimpleNamespace(
                                time=lambda later=later: later, time_ns=time.time_ns
                            ),
                        )
                        found = tidy.plan(roots=(root,), now=later)
                        for action in found.actions:
                            if action.action in tidy.OFFERED and action.ready:
                                tidy.apply(action.action, action, roots=(root,))
                    did = act
                except Exception as error:
                    did = f"{act} refused ({type(error).__name__})"
                # Taken: gone from its name, or no longer holding what it held.
                # Not "empty": a stage the download has just made and not yet
                # written is empty before anyone looks at it, and the full
                # campaign pauses there too (ten of its 108 points).
                taken = [
                    path for path in live if not path.is_file() or path.read_bytes() != live[path]
                ]
            (made,) = theirs
        media = root / "Wall-in-One" / "MotionBGS" / "aurora.mp4"
        record = media.with_name(media.name + ".motionbgs.json")
        where = f"held at call {call.index} ({call.name})"
        if "WEDGED" in made.errors:
            problems.append(f"WEDGED {where}: the download never ended")
        if media.exists() and media.read_bytes() != MEDIA:
            problems.append(f"{where}: the media at its public name is not whole")
        if record.exists():
            try:
                json.loads(record.read_bytes())
            except ValueError:
                problems.append(f"{where}: the record at its public name does not parse")
        if made.output.startswith("DONE") and not (media.exists() and record.exists()):
            problems.append(f"{where}: it said DONE without both the media and its record")
        interfered += bool(taken) or not made.output.startswith("DONE")
        kinds = sorted(
            "a v2 stage"
            if path.name.startswith(".wall-in-one-v2-")
            else "a commit name"
            if path.name.startswith(download.COMMIT_PREFIX)
            else "an older name"
            for path in taken
        )
        outcomes[
            f"{did}; live stages taken: {len(taken)} of {len(live)} {kinds}; the download then: "
            f"{made.output.replace(str(job.base), '')[:150] or made.status}"
        ] += 1
    print(f"\na day forward while a second process downloads ({act}):")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {count:4d}  {outcome}")
    return outcomes, problems, interfered


@pytest.mark.parametrize("act", ["sweep", "tidy"])
def test_a_day_forward_while_a_second_process_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, act: str
) -> None:
    """Whatever the jump does to the live download, nothing short or half-made
    reaches a public name, and the download ends by saying what happened."""
    outcomes, problems, _interfered = _a_day_forward(tmp_path, monkeypatch, act)
    assert outcomes and not problems, "\n".join(problems[:20])


def test_a_day_forward_does_not_take_a_live_downloads_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the clock a day ahead, the sweep leaves alone a stage that a live
    download in another process still holds its lock on: age is not the only
    test, and the download completes."""
    _outcomes, problems, interfered = _a_day_forward(tmp_path, monkeypatch, "sweep")
    assert not problems and not interfered


def test_a_day_forward_tidy_does_not_archive_a_live_downloads_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the clock a day ahead, Tidy up neither offers nor applies the
    archiving of a file a live download in another process still holds its
    lock on, and the download completes."""
    _outcomes, problems, interfered = _a_day_forward(tmp_path, monkeypatch, "tidy")
    assert not problems and not interfered


def test_a_stage_from_a_day_ahead_is_not_recent_for_ever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clock went forward a day, a download died there, and the clock came
    back: its stage is dated tomorrow. Swept now it is kept as recent; swept
    once the clock has passed it by a day it is reclaimed."""
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    plain = faults.Job(tmp_path / "plain", {}, ())
    downloads._prepare(plain.base)
    (trace,) = faults.run(SOURCE, downloads.PRODUCER, [plain])
    jobs = [
        faults.Job(tmp_path / f"a{number:05d}", {"kill": call.index}, ())
        for number, call in enumerate(trace.changes)
    ]
    for job in jobs:
        downloads._prepare(job.base)
    faults.run(SOURCE, downloads.PRODUCER, jobs)
    tried = 0
    for job in jobs:
        root = Path(faults.profile_environment(job.base)["HOME"]) / "Wallpapers"
        stages = [
            path
            for path, data in _temporaries(root).items()
            if data and path.name.startswith(".wall-in-one-v2-")
        ]
        if not stages:
            continue
        tried += 1
        ahead = time.time() + DAY
        for stage in stages:
            os.utime(stage, (ahead, ahead))
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            download.managed_directory(root, download.MOTIONBGS_LOCATION)
            assert all(stage.stat().st_size for stage in stages), "dated tomorrow: kept for now"
            _clock(scoped, 2 * DAY + 3600)
            download.managed_directory(root, download.MOTIONBGS_LOCATION)
        assert not any(stage.is_file() and stage.stat().st_size for stage in stages)
    assert tried


def test_an_older_name_is_never_swept_whatever_the_clock_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One of 0.2.x's older names may have been public, so no clock makes this
    version's sweep take it: two days ahead or two days back, its bytes stay.
    A v2 stage dated a day ahead is kept as recent meanwhile; whether Tidy
    up's plan names it is printed, not asserted (it did not, when written)."""
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    base = tmp_path / "a00000"
    downloads._prepare(base)
    root = Path(faults.profile_environment(base)["HOME"]) / "Wallpapers"
    with monkeypatch.context() as scoped:
        faults.scratch_profile(scoped, base)
        directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
        older = directory / ".wall-in-one-media-stage-abcdefgh"
        older.write_bytes(b"what 0.2.x left here")
        ahead = directory / ".wall-in-one-v2-media-stage-abcdefgh"
        ahead.write_bytes(b"a stage from when the clock was a day ahead")
        tomorrow = time.time() + DAY
        os.utime(ahead, (tomorrow, tomorrow))
        for jump in (2 * DAY, -2 * DAY):
            _clock(scoped, jump)
            download.managed_directory(root, download.MOTIONBGS_LOCATION)
            assert older.read_bytes() == b"what 0.2.x left here", f"clock {jump:+} s"
        found = tidy.plan(roots=(root,), now=time.time())
        listed = ahead.name in repr(found)
        print(f"\na v2 stage dated a day ahead is named in Tidy up's plan: {listed}")
        assert ahead.is_file() and ahead.stat().st_size, "kept as recent"


def test_a_v2_stage_a_person_edited_is_reclaimed_a_day_after_the_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterisation of a known case, by design: this version's own
    day-old sweep reclaims its v2 and commit names even when a person edited
    one. Such a name is never public by construction (AB-1), which is why the
    sweep may reclaim it; the app keeps no record of what a crash-left partial
    should hold, so it can't tell an edit from its own partial bytes. The
    older names, which may have been public, it never touches (the test
    above).

    This version's download dies leaving a ``.wall-in-one-v2-…-stage-*`` file,
    and a person appends a note to it. The sweep's age is the later of the file's two
    times, and nothing can set its change time back, so the edit restarts the
    day whatever its modification time is then set to: swept now, the edited
    stage is kept; with its mtime set back a day, it is still kept; a day
    after the edit it is reclaimed. Nothing at a public name changes."""
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    plain = faults.Job(tmp_path / "plain", {}, ())
    downloads._prepare(plain.base)
    (trace,) = faults.run(SOURCE, downloads.PRODUCER, [plain])
    jobs = [
        faults.Job(tmp_path / f"a{number:05d}", {"kill": call.index}, ())
        for number, call in enumerate(trace.changes)
    ]
    for job in jobs:
        downloads._prepare(job.base)
    faults.run(SOURCE, downloads.PRODUCER, jobs)
    tried = 0
    for job in jobs:
        root = _root(job.base)
        stages = [
            entry
            for entry in faults.tree(root).entries
            if entry.path.name.startswith(".wall-in-one-v2-") and entry.size
        ]
        if not stages:
            continue
        tried += 1
        stage = stages[0].path
        public = {
            entry.path: entry.data
            for entry in faults.tree(job.base).entries
            if not any(part.startswith(".") for part in entry.path.relative_to(job.base).parts)
        }
        faults.edit_in_place(stage, "extend")
        edited = stage.read_bytes()
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            download.managed_directory(root, download.MOTIONBGS_LOCATION)
            assert stage.read_bytes() == edited, "edited a moment ago: the sweep leaves it"
            old = time.time() - LATER_SECONDS
            os.utime(stage, (old, old))
            directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
            assert stage.read_bytes() == edited, "its mtime set back: still edited a moment ago"
            # A change time can't be set back, so the same sweep is asked what
            # it would do a day and an hour on.
            download.recover_abandoned(
                directory, download.MOTIONBGS_LOCATION, now=time.time() + LATER_SECONDS
            )
        assert not stage.exists() or not stage.stat().st_size, "a day after the edit: reclaimed"
        now = {path: path.read_bytes() for path in public if path.is_file()}
        assert now == public, "nothing at a public name changed"
    assert tried, "at least one kill left a v2 stage holding bytes"
