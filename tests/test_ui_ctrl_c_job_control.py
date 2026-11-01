"""Codex R41-5, adopted: a countdown that loses its terminal's foreground doesn't read it.

The countdown starts in the foreground and watches for Esc. Sent to the background since
(Ctrl-Z, then ``bg``), a read of the terminal would stop the process (SIGTTIN), held
changes and all, once a line is typed there. The harness is a shell-like session leader on a
pty of the test's own; its countdown job is a separate, non-orphaned group, so a background
read has a terminal job's SIGTTIN semantics. Controls: input in the foreground, a job in the
background from the start (no watch), and losing the foreground with no input. Children are
signalled only by the PIDs this file started (a pidfd for the job).
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

HARNESS = r"""
import fcntl, json, os, select, signal, sys, termios, time
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
signal.signal(signal.SIGTTIN, signal.SIG_DFL)
signal.signal(signal.SIGTSTP, signal.SIG_DFL)
shell_mode = termios.tcgetattr(0)
variant = sys.argv[1]
go_r, go_w = os.pipe()
ready_r, ready_w = os.pipe()
job = os.fork()
if job == 0:
    os.setpgid(0, 0)
    os.close(go_w)
    os.close(ready_r)
    os.read(go_r, 1)
    from gi.repository import GLib
    from wall_in_one.ui.quit_countdown import Countdown
    loop = GLib.MainLoop()
    countdown = Countdown(1, 5, 1000, done=loop.quit, cancelled=loop.quit)
    countdown.start()
    os.write(ready_w, b"1")
    loop.run()
    countdown.stop()
    os._exit(0)
os.close(go_r)
os.close(ready_w)
print(f"SPAWNED {job}", flush=True)
status = None
stopped = None
try:
    os.setpgid(job, job)
    if variant != "background-from-start":
        os.tcsetpgrp(0, job)
    os.write(go_w, b"1")
    readable, _, _ = select.select([ready_r], [], [], 10)
    if not readable:
        raise RuntimeError("job never became ready")
    os.read(ready_r, 1)
    if variant in ("background-input", "background-no-input"):
        os.kill(job, signal.SIGTSTP)
        _, paused = os.waitpid(job, os.WUNTRACED)
        assert os.WIFSTOPPED(paused)
        os.tcsetpgrp(0, os.getpgrp())
        termios.tcsetattr(0, termios.TCSANOW, shell_mode)
        os.kill(job, signal.SIGCONT)
    print("READY", flush=True)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        found, value = os.waitpid(job, os.WNOHANG | os.WUNTRACED)
        if found:
            if os.WIFSTOPPED(value):
                stopped = os.WSTOPSIG(value)
                os.tcsetpgrp(0, job)
                os.kill(job, signal.SIGCONT)
                os.kill(job, signal.SIGTERM)
                _, status = os.waitpid(job, 0)
            else:
                status = value
            break
        time.sleep(0.01)
    if status is None:
        raise RuntimeError("job did not finish")
finally:
    if status is None:
        os.tcsetpgrp(0, job)
        os.kill(job, signal.SIGCONT)
        os.kill(job, signal.SIGKILL)
        os.waitpid(job, 0)
    os.tcsetpgrp(0, os.getpgrp())
print(json.dumps({"stopped": stopped, "status": status}), flush=True)
"""


@pytest.mark.parametrize(
    "variant",
    ["background-input", "foreground-input", "background-from-start", "background-no-input"],
)
def test_countdown_does_not_read_after_losing_foreground(variant: str) -> None:
    master, slave = os.openpty()
    child = subprocess.Popen(
        [sys.executable, "-c", HARNESS, variant],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=0,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    job_fd = None
    try:
        assert child.stdout is not None
        ready, _, _ = select.select([child.stdout], [], [], 15)
        assert ready, "the owned harness did not become ready"
        line = child.stdout.readline()
        assert line.startswith(b"SPAWNED "), (line, child.poll())
        job_fd = os.pidfd_open(int(line.split()[1]))
        ready, _, _ = select.select([child.stdout], [], [], 15)
        assert ready, "the owned job did not become ready"
        line = child.stdout.readline()
        assert line == b"READY\n", (line, child.poll())
        if variant != "background-no-input":
            os.write(master, b"input\n")
        out, err = child.communicate(timeout=15)
        assert child.returncode == 0, err.decode()
        result: dict[str, Any] = json.loads(out.decode().strip())
        assert result["stopped"] is None, (
            f"{variant}: background terminal read stopped the countdown "
            f"with signal {result['stopped']} (SIGTTIN={signal.SIGTTIN})"
        )
        assert result["status"] == 0, result
    finally:
        if child.poll() is None:
            if job_fd is not None:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(job_fd, signal.SIGKILL)
            child.kill()
            child.wait(10)
        if job_fd is not None:
            os.close(job_fd)
        os.close(slave)
        os.close(master)
