"""The clock jumps a day forward while this process's own download is in progress.

`tests/test_clock_jumps.py` holds a download alive in a *second* process while
this one sweeps. Here there is one process: its download is stopped at each of
its filesystem calls, the time source is moved a day ahead (never the system
clock), and the same process either opens the managed folder again, as it does
when a second download starts or the Store page asks where downloads go
(that sweeps), or plans and applies Tidy up. Then the download carries on.

Whatever that does to the live download's own files (the sweep reclaims a v2
stage; Tidy up archives a commit name, whose bytes it keeps), nothing short
or half-made may reach a public name, the download must end by saying what
happened, and nothing may wedge (one process, so a lock the download holds
is one the sweep meets from inside).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_download as downloads
from tests.test_released_round_trip import SOURCE

MEDIA = b"validated media bytes"

#: Replaces the tracer's ``_pause``: at the planned call the clock is a day
#: and an hour ahead and this same process opens the folder again.
_JUMP = r"""
import json as _json_again
import time as _real_time
import types as _types


def _temporaries(root):
    found = {}
    for directory, _folders, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            if name.startswith(".wall-in-one-") and "managed" not in name:
                if os.path.isfile(path) and not os.path.islink(path):
                    found[path] = os.path.getsize(path)
    return found


def _pause():
    root = Path(os.environ["HOME"]) / "Wallpapers"
    live = _temporaries(root)
    later = _real_time.time() + 25 * 3600
    if _PLAN.get("act") == "tidy":
        from wall_in_one import tidy as _tidy

        _tidy.time = _types.SimpleNamespace(time=lambda: later, time_ns=_real_time.time_ns)
        try:
            found = _tidy.plan(roots=(root,), now=later)
            for action in found.actions:
                if action.action in _tidy.OFFERED and action.ready:
                    _tidy.apply(action.action, action, roots=(root,))
            did = "tidied"
        except Exception as error:
            did = f"Tidy up refused ({type(error).__name__})"
    else:
        names = {name: getattr(_real_time, name) for name in dir(_real_time) if name[:1] != "_"}
        names["time"] = lambda: later
        real = download.time
        download.time = _types.SimpleNamespace(**names)
        try:
            try:
                download.managed_directory(root, download.MOTIONBGS_LOCATION)
                did = "swept"
            except Exception as error:
                did = f"sweep refused ({type(error).__name__})"
        finally:
            download.time = real
    # Gone, or emptied: a stage only just made, and still empty, was not taken.
    taken = sorted(
        os.path.basename(path)
        for path, size in live.items()
        if not os.path.isfile(path) or (size and not os.path.getsize(path))
    )
    print(
        "JUMP "
        + _json_again.dumps(
            {"did": did, "live": sorted(os.path.basename(path) for path in live), "taken": taken}
        ),
        flush=True,
    )


serve(operation)
"""
assert downloads.PRODUCER.rstrip().endswith("serve(operation)")
SAME_PROCESS = downloads.PRODUCER.rstrip().removesuffix("serve(operation)") + _JUMP


def _kind(name: str) -> str:
    if name.startswith(".wall-in-one-v2-"):
        return "a v2 stage"
    if name.startswith(".wall-in-one-commit-"):
        return "a commit name"
    return "an older name"


def _a_day_forward_in_one_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, act: str
) -> tuple[Counter[str], list[str], int]:
    """What happened by kind, what went wrong, and how often the download was disturbed."""
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    plain = faults.Job(tmp_path / "plain", {}, ())
    downloads._prepare(plain.base)
    (alone,) = faults.run(SOURCE, SAME_PROCESS, [plain])
    assert alone.status == 0 and alone.output == "DONE", alone.output + alone.errors[-1000:]
    points = alone.calls if os.environ.get(faults.FULL) else alone.changes
    outcomes: Counter[str] = Counter()
    problems: list[str] = []
    interfered = 0
    for call in points:
        job = faults.Job(
            tmp_path / f"p{call.index:05d}", {"pause": call.index, "gate": "-", "act": act}, ()
        )
        downloads._prepare(job.base)
        root = Path(faults.profile_environment(job.base)["HOME"]) / "Wallpapers"
        where = f"jumped at call {call.index} ({call.name})"
        try:
            # One job a run: a download that never ends is that call's, by name.
            (made,) = faults.run(SOURCE, SAME_PROCESS, [job], timeout=60.0)
        except subprocess.TimeoutExpired:
            problems.append(f"WEDGED {where}: the download never ended")
            continue
        lines = made.output.splitlines()
        jumps = [json.loads(line.removeprefix("JUMP ")) for line in lines if line[:5] == "JUMP "]
        said = next((line for line in lines if line[:5] != "JUMP "), "")
        if made.status != 0 or not said.startswith(("DONE", "REFUSED", "ERROR")):
            problems.append(
                f"{where}: it ended {made.status} without saying what happened: "
                f"{made.output[-300:]} {made.errors[-600:]}"
            )
        if len(jumps) != 1:
            problems.append(f"{where}: the folder was opened {len(jumps)} times, not once")
            continue
        (jump,) = jumps
        media = root / "Wall-in-One" / "MotionBGS" / "aurora.mp4"
        record = media.with_name(media.name + ".motionbgs.json")
        if media.exists() and media.read_bytes() != MEDIA:
            problems.append(f"{where}: the media at its public name is not whole")
        if record.exists():
            try:
                json.loads(record.read_bytes())
            except ValueError:
                problems.append(f"{where}: the record at its public name does not parse")
        if said.startswith("DONE") and not (media.exists() and record.exists()):
            problems.append(f"{where}: it said DONE without both the media and its record")
        if record.exists() and not media.exists():
            problems.append(f"{where}: a record was published with no media beside it")
        interfered += bool(jump["taken"]) or not said.startswith("DONE")
        kinds = sorted(_kind(name) for name in jump["taken"])
        outcomes[
            f"{jump['did']}; live stages taken: {len(jump['taken'])} of {len(jump['live'])} "
            f"{kinds}; the download then: {said.replace(str(job.base), '')[:200]}"
        ] += 1
    print(f"\na day forward while this same process downloads ({act}):")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {count:4d}  {outcome}")
    return outcomes, problems, interfered


@pytest.fixture(scope="module", params=["sweep", "tidy"])
def jumped(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Counter[str], list[str], int]:
    """The campaign, once for the tests that read it: the folder opened again
    (its sweep), or Tidy up planned and applied."""
    with pytest.MonkeyPatch.context() as patch:
        return _a_day_forward_in_one_process(
            tmp_path_factory.mktemp("jumped"), patch, request.param
        )


def test_a_day_forward_while_this_process_downloads(
    jumped: tuple[Counter[str], list[str], int],
) -> None:
    """Nothing short or half-made reaches a public name, nothing wedges, and
    the download ends by saying what happened."""
    outcomes, problems, _interfered = jumped
    assert outcomes and not problems, "\n".join(problems[:20])


def test_a_day_forward_does_not_take_this_processs_own_live_stage(
    jumped: tuple[Counter[str], list[str], int],
) -> None:
    """With the clock a day ahead, a stage's age alone would have it reclaimed
    while this very process's download is still using it. The liveness lock is
    held on the stage's open file, not per process, so the sweep and Tidy up
    here see it too and leave the stage alone."""
    _outcomes, problems, interfered = jumped
    assert not problems and not interfered
