"""Worker-owned child processes cannot retain interpreter shutdown."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final, cast

import pytest

from tests.process_helpers import kill_and_reap, reaped, subreaper
from wall_in_one import worker_processes


def _wait_until_started(cancellation: worker_processes.Cancellation) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with cancellation._lock:
            if cancellation._active:
                return
        time.sleep(0.01)
    pytest.fail("worker child did not start")


def test_cancel_kills_a_running_process_group_within_the_quit_budget() -> None:
    cancellation = worker_processes.Cancellation()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        cancellation.run,
        [sys.executable, "-c", "import time; time.sleep(60)"],
        timeout=60.0,
    )
    _wait_until_started(cancellation)

    started = time.monotonic()
    cancellation.cancel()
    with pytest.raises(worker_processes.ProcessCancelledError):
        future.result(timeout=2)
    pool.shutdown(wait=True, cancel_futures=True)

    assert time.monotonic() - started < 1.5


def test_polling_for_cancellation_does_not_shorten_the_command_timeout() -> None:
    cancellation = worker_processes.Cancellation()

    completed = cancellation.run(
        [sys.executable, "-c", "import time; time.sleep(0.25); print('done')"],
        timeout=1.0,
    )

    assert completed.returncode == 0
    assert completed.stdout == b"done\n"


def test_cancel_before_submission_refuses_to_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    cancellation = worker_processes.Cancellation()
    cancellation.cancel()
    spawned = False

    def refuse(*_arguments: object, **_keywords: object) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr("wall_in_one.worker_processes.subprocess.Popen", refuse)

    with pytest.raises(worker_processes.ProcessCancelledError):
        cancellation.run(["never-started"], timeout=60.0)

    assert not spawned


# -- a group whose leader already exited ------------------------------------------------

#: The leader starts ``sleep`` in its own process group (inheriting the captured
#: pipes), writes the sleeper's pid where the test can read it, and exits.
_LEADER: Final = """
import subprocess, sys
sleeper = subprocess.Popen(["sleep", "120"])
with open(sys.argv[1], "w") as handle:
    handle.write(str(sleeper.pid))
"""


def test_a_group_member_left_holding_the_pipes_by_an_exited_leader_is_ended(
    tmp_path: Path,
) -> None:
    """The audit's B-3: the leader exits first while its same-group child keeps the
    output pipes. That child used to survive until a cancellation reached it; the
    leader's exit now ends the run and the group with it, before the reap."""
    subreaper(True)
    sleeper: int | None = None
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        cancellation = worker_processes.Cancellation()
        handoff = tmp_path / "sleeper.pid"
        future = pool.submit(
            cancellation.run,
            [sys.executable, "-c", _LEADER, str(handoff)],
            timeout=60.0,
        )
        # Known before the run ends, so a regression fails here, not after 60s.
        deadline = time.monotonic() + 5.0
        while not (handoff.exists() and handoff.read_text()) and time.monotonic() < deadline:
            time.sleep(0.01)
        sleeper = int(handoff.read_text())

        completed = future.result(timeout=10)

        assert completed.returncode == 0
        status = reaped(sleeper, 3.0)
        assert status is not None, "the sleeper outlived its leader's run"
        assert os.WIFSIGNALED(status)
        sleeper = None
    finally:
        if sleeper is not None:
            # Only the exact sleeper this test started, killed and reaped here;
            # that also frees a run still waiting on its pipes.
            kill_and_reap(sleeper)
        pool.shutdown(wait=True, cancel_futures=True)
        subreaper(False)


def test_a_cancel_that_races_the_reap_never_signals_a_reaped_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sweep 3 S-6: the canceller passes its ownership check, then is delayed at
    the signal while the worker finishes communicate() and reaps the leader.
    Its group id would then name nothing it owns. The signal is dry here and
    the canceller waits at that boundary until reaping could have won."""
    cancellation = worker_processes.Cancellation()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        cancellation.run,
        [sys.executable, "-c", "import time; time.sleep(0.3)"],
        timeout=10.0,
    )
    _wait_until_started(cancellation)
    with cancellation._lock:
        (owned,) = cancellation._active.values()
    # Before the fix the registry held the Popen itself.
    held: object = owned
    process = cast("subprocess.Popen[bytes]", getattr(held, "process", held))
    canceller = threading.Thread(target=cancellation.cancel)
    signalled: list[tuple[str, int | None]] = []

    def dry_killpg(_group: int, _signal: int) -> None:
        if threading.current_thread() is canceller:
            deadline = time.monotonic() + 1.5
            while process.returncode is None and time.monotonic() < deadline:
                time.sleep(0.01)
        # Never sent: only what the leader's state was at the signal is recorded.
        signalled.append((threading.current_thread().name, process.returncode))

    monkeypatch.setattr(os, "killpg", dry_killpg)
    canceller.start()
    canceller.join(timeout=5)
    with contextlib.suppress(worker_processes.ProcessCancelledError):
        future.result(timeout=5)
    pool.shutdown(wait=True, cancel_futures=True)

    assert not canceller.is_alive()
    assert signalled, "the cancellation tried to signal the group"
    assert all(returncode is None for _name, returncode in signalled), (
        f"a group was signalled after its leader was reaped: {signalled}"
    )
    assert process.returncode == 0, "the worker reaped its own child afterwards"


#: The leader starts ``sleep`` in its process group with every stdio stream
#: closed (so nothing holds the captured pipes), records its pid, and exits 0.
_DETACHED_LEADER: Final = """
import subprocess, sys
sleeper = subprocess.Popen(
    ["sleep", "120"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
with open(sys.argv[1], "w") as handle:
    handle.write(str(sleeper.pid))
print("done")
"""


def test_a_successful_run_ends_what_its_leader_left_in_the_group(tmp_path: Path) -> None:
    """Follow-up to sweep 4: communicate() used to reap the leader the moment its
    pipes closed, so a same-group child that had let go of them outlived an
    ordinary, successful run."""
    subreaper(True)
    sleeper: int | None = None
    handoff = tmp_path / "sleeper.pid"
    try:
        completed = worker_processes.Cancellation().run(
            [sys.executable, "-c", _DETACHED_LEADER, str(handoff)], timeout=10.0
        )

        assert completed.returncode == 0 and completed.stdout == b"done\n"
        sleeper = int(handoff.read_text())
        status = reaped(sleeper, 3.0)
        assert status is not None, "the leader's child outlived the run"
        assert os.WIFSIGNALED(status)
        sleeper = None
    finally:
        if sleeper is not None:
            kill_and_reap(sleeper)
        subreaper(False)
