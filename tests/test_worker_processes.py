"""Worker-owned child processes cannot retain interpreter shutdown."""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final, cast

import pytest

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

_PR_SET_CHILD_SUBREAPER: Final = 36
#: The leader starts ``sleep`` in its own process group (inheriting the captured
#: pipes), writes the sleeper's pid where the test can read it, and exits.
_LEADER: Final = """
import subprocess, sys
sleeper = subprocess.Popen(["sleep", "120"])
with open(sys.argv[1], "w") as handle:
    handle.write(str(sleeper.pid))
"""


def _subreaper(enabled: bool) -> None:
    """Adopt orphaned descendants, so the test can reap the exact sleeper itself."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, int(enabled), 0, 0, 0) != 0:
        pytest.skip("this kernel can't make the test a child subreaper")


def _state(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return ""
    return stat.rsplit(")", 1)[1].split()[0]


def test_cancel_kills_a_group_member_whose_leader_already_exited(tmp_path: Path) -> None:
    """The audit's B-3: the leader exits first; its same-group child keeps the
    output pipes. Cancelling must still kill that child."""
    _subreaper(True)
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
        _wait_until_started(cancellation)
        with cancellation._lock:
            (leader,) = cancellation._active
        deadline = time.monotonic() + 5.0
        while _state(leader) != "Z" or not handoff.exists() or not handoff.read_text():
            assert time.monotonic() < deadline, "the leader never exited"
            time.sleep(0.01)
        sleeper = int(handoff.read_text())
        assert os.getpgid(sleeper) == leader, "the sleeper stayed in the leader's group"
        assert _state(sleeper) in ("S", "R")

        cancellation.cancel()
        with pytest.raises(worker_processes.ProcessCancelledError):
            future.result(timeout=5)

        deadline = time.monotonic() + 3.0
        reaped, status = os.waitpid(sleeper, os.WNOHANG)
        while reaped == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
            reaped, status = os.waitpid(sleeper, os.WNOHANG)
        assert reaped == sleeper, "the sleeper outlived the cancellation"
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        sleeper = None
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        if sleeper is not None:
            # Only the exact sleeper this test started, killed and reaped here.
            with contextlib.suppress(ProcessLookupError):
                os.kill(sleeper, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(sleeper, 0)
        _subreaper(False)


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
