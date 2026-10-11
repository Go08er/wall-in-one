"""A removal whose sync fails right after the media was claimed: what is said, and what is left.

Found by the disk-full probes (`tests/test_disk_full.py`). The removal has
moved the media into its claim folder; the sync that follows fails with
ENOSPC. Today:

- the refusal says the file "changed while removal claimed it; an unverified
  entry was preserved at <claim>, and its pending removal record was retained
  for recovery; its download record was kept at <claim>": nothing changed, a
  sync failed;
- the next start then finishes the removal without a word: the claimed media
  and the claimed download record are gone, the journal is empty;
- and the still and the video's selector stay behind, with no video.

What should hold instead is asserted below, so this fails until the removal
follow-up lands: the refusal gives the real reason, and a start that finishes
the removal either takes the still and the selector with it or says so.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_removals as removals
from tests.test_released_round_trip import REMOVAL, SOURCE

CLAIMED = "changed while removal claimed it"


@pytest.mark.xfail(strict=True, reason="removal follow-up on r5-ac12b")
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
    met = [(job, made) for job, made in zip(jobs, produced, strict=True) if CLAIMED in made.output]
    assert met, "no sync failure after the claim was reported as a change: the first half is fixed"

    problems: list[str] = []
    for job, made in met:
        where = f"ENOSPC at sync {job.plan['fail']}"
        said = made.output.replace(str(job.base), "")
        # 1. The message: a failed sync, not a file that changed.
        problems.append(f"{where}: reported as a change: {said[:200]}")
        consumer = removals._Consumer("remove")
        media = consumer.media(job.base)
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            started = consumer._start(job.base).replace(str(job.base), "")
        left = sorted(
            str(entry.path.relative_to(job.base))
            for entry in faults.tree(job.base).entries
            if entry.size
            and (
                entry.path.name.startswith(media.name + ".")
                or entry.path.parent.name == "Automatic Stills"
            )
        )
        # 2. The next start: finished without a word, companions left behind.
        if not media.exists() and started.endswith("nothing to say") and left:
            problems.append(f"{where}: the next start finished it silently and left {left}")
    assert not problems, "\n".join(problems)
