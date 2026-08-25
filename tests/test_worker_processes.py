"""Worker-owned child processes cannot retain interpreter shutdown."""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor

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
