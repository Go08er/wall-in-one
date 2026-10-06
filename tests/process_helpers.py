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
#: A stand-in's sleeper, for a ``sh`` stand-in called with the handoff path as
#: ``$3``: starts ``sleep 120`` in the stand-in's process group (so it holds
#: the stand-in's pipes) and writes ``"<leader> <sleeper>"`` to ``$3.tmp``,
#: for the stand-in to ``mv`` into place.
#:
#: The sleeper is started from a subshell that exits at once, so it is
#: orphaned, and adopted by the test (its subreaper), before the handoff
#: exists. Started by the stand-in shell itself, it would be the shell's
#: child: a shell blocked in ``waitpid`` (for ``mv``, say) when the test
#: kills the group can reap a sleeper that died first, before its own
#: SIGKILL lands, and the test would then find no child to reap and a pid it
#: may no longer signal. (``$$`` is the stand-in's own pid in the subshell.)
SLEEPER_STARTER: Final = '( sleep 120 & echo "$$ $!" > "$3.tmp" )'
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


def is_own_child(pid: int) -> bool:
    """Whether ``pid`` is this process's unreaped child, asked without reaping it."""
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False
    return True


def kill_and_reap(pid: int) -> None:
    """Clean up a sleeper the test started and has not reaped yet.

    Only while it is still this process's unreaped child: then its pid is
    reserved and the signal reaches only that process. One that is no longer
    ours was reaped elsewhere, and its pid may already name someone else.
    """
    if not is_own_child(pid):
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)
