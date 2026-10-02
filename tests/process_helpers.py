"""Exact-pid helpers for the tests that own process groups.

Those tests start a stand-in whose child stays in its process group, make the
test process a child subreaper so that the orphan is adopted, and then reap
that exact pid themselves. Nothing here signals anything but the pid it is
given, and the tests only give it their own children, whose pids stay
reserved until they are reaped here.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import time
from pathlib import Path
from typing import Final

import pytest

_PR_SET_CHILD_SUBREAPER: Final = 36
#: ``/proc/<pid>/stat`` states of a process that has exited: zombie or dead.
_EXITED: Final = frozenset({"Z", "X", "x"})


def subreaper(enabled: bool) -> None:
    """Adopt orphaned descendants, so the test can reap the exact sleeper itself."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, int(enabled), 0, 0, 0) != 0:
        pytest.skip("this kernel can't make the test a child subreaper")


def is_alive(pid: int) -> bool:
    """Whether ``pid`` exists and has not exited.

    Every state but zombie and dead counts as alive: a process in
    uninterruptible sleep (``D``), as it can be under build load, or stopped
    (``T``) has not exited any more than one that is sleeping or running.
    Signal 0 sends nothing; it only asks whether the pid exists.
    """
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError, ProcessLookupError:
        return False
    # The state follows the command name, which may itself contain ")".
    fields = stat.rsplit(")", 1)[-1].split()
    return bool(fields) and fields[0] not in _EXITED


def handed_off(handoff: Path, seconds: float = 3.0) -> tuple[int, int]:
    """The ``"<leader> <sleeper>"`` pids a stand-in wrote (atomically) to ``handoff``."""
    deadline = time.monotonic() + seconds
    while not handoff.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    leader, sleeper = handoff.read_text().split()
    return int(leader), int(sleeper)


def reaped(pid: int, seconds: float) -> int | None:
    """Reap the test's own child ``pid``: its wait status, or None if it
    is still running after ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        found, status = os.waitpid(pid, os.WNOHANG)
        if found == pid:
            return status
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def kill_and_reap(pid: int) -> None:
    """Clean up a sleeper the test started and has not reaped yet. Its pid is
    still reserved, so the signal reaches only that process."""
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)
