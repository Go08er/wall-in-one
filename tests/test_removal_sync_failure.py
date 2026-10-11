"""A removal whose sync fails right after a move into a claim: what is said, and what is left.

Found by the disk-full probes (`tests/test_disk_full.py`). A removal moves
the wallpaper's download record into a claim, then the media into another;
the sync that follows either move can fail with ENOSPC. Before 97d97ef:

- the refusal said the file "changed while removal claimed it; an unverified
  entry was preserved at <claim>": nothing had changed, a sync had failed;
- the next start then finished the removal without a word and left the still
  and the video's selector behind, with no video;
- and when it was the record's own claim that could not be made durable, the
  pending removal record was cancelled, so the record stayed in its claim for
  good and the wallpaper was a local file from then on.

What holds now, at every sync after a rename in a removal:

- no refusal says anything changed: one whose claim could not be made
  durable says that, with the system's reason;
- the media's claim: the next start finishes the removal whole, the still
  and the selector with it;
- the record's claim: the media was never touched, and the next start puts
  the record back at its name.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_removals as removals
from tests.test_released_round_trip import REMOVAL, SOURCE

CLAIMED = "changed while removal claimed it"
CHANGED = "changed before removal"
NOT_DURABLE = "could not be made durable"


def test_a_sync_that_fails_after_the_claim_is_reported_as_that_and_finished_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    plain = faults.Job(tmp_path / "plain", {}, ("remove",))
    removals._prepare(plain.base)
    (trace,) = faults.run(SOURCE, REMOVAL, [plain])
    assert trace.status == 0 and trace.output.startswith("DONE"), trace.output
    # The fault point: ENOSPC at one of the three syncs after a rename.
    renames = [call.index for call in trace.changes if call.name == "_rename_noreplace"]
    syncs = [call.index for call in trace.calls if call.kind == "sync"]
    points = sorted({index for moved in renames for index in [s for s in syncs if s > moved][:3]})
    jobs = [
        faults.Job(tmp_path / f"a{number:05d}", {"fail": index, "errno": "ENOSPC"}, ("remove",))
        for number, index in enumerate(points)
    ]
    for job in jobs:
        removals._prepare(job.base)
    produced = faults.run(SOURCE, REMOVAL, jobs)

    problems: list[str] = []
    finished: list[str] = []
    put_back: list[str] = []
    for job, made in zip(jobs, produced, strict=True):
        where = f"ENOSPC at sync {job.plan['fail']}"
        said = made.output.replace(str(job.base), "")
        # 1. The message: a failed sync is never a file that changed.
        if CLAIMED in said or CHANGED in said:
            problems.append(f"{where}: reported as a change: {said[:200]}")
        if NOT_DURABLE not in said or not said.startswith("REFUSED"):
            continue
        consumer = removals._Consumer("remove")
        media = consumer.media(job.base)
        record = media.with_name(media.name + ".motionbgs.json")
        untouched = media.exists()
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            started = consumer._start(job.base).replace(str(job.base), "")
        entries = [entry for entry in faults.tree(job.base).entries if entry.size]
        claimed = sorted(
            str(entry.path.relative_to(job.base))
            for entry in entries
            if ".wall-in-one-removal-" in str(entry.path)
        )
        if claimed:
            problems.append(f"{where}: the next start left {claimed} in a claim ({started})")
        if untouched:
            # 2. The record's own claim: the removal never happened.
            put_back.append(where)
            if not (media.exists() and record.exists() and record.stat().st_size):
                problems.append(f"{where}: the record was not put back beside its media")
            continue
        # 3. The media's claim: the next start finishes it whole.
        finished.append(where)
        left = sorted(
            str(entry.path.relative_to(job.base))
            for entry in entries
            if entry.path.name.startswith(media.name + ".")
            or entry.path.parent.name == "Automatic Stills"
        )
        if media.exists():
            problems.append(f"{where}: the next start did not finish the removal ({started})")
        elif left and started.endswith("nothing to say"):
            problems.append(f"{where}: the next start finished it silently and left {left}")
    assert not problems, "\n".join(problems)
    # Neither half is vacuous: both claims' syncs were reached.
    assert finished, "no fault reached the sync after the media's claim"
    assert put_back, "no fault reached the sync after the record's claim"
