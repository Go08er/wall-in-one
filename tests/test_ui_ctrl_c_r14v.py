"""Review R14V's findings on the any-key countdown, adopted: each failed at 06b7a39.

* **L3:** with the terminal's output stopped (Ctrl-S), the countdown's line, written in place
  once a second, waited for Ctrl-Q on GTK's thread, and the whole application with it: a key
  pressed meanwhile didn't cancel until Ctrl-Q. The line is left out while the terminal can't
  take it.
* **A paste:** a key is one read of up to 64 bytes, and the rest of a longer paste stayed in the
  terminal's input, for the shell to read and run once the application had exited. What comes
  with the key is discarded.

The real countdown runs in a child of this test, on a pty that is its controlling terminal,
with standard error on it too. Children are signalled only by the exact pid this file started.
"""

from __future__ import annotations

import io
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wall_in_one.ui import quit_countdown

pytestmark = pytest.mark.gui

REPO = Path(__file__).resolve().parents[1]

CHILD = r"""
import fcntl, os, sys, termios, time
linger = sys.argv[1:] == ["linger"]
fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # the pty is this new session's terminal
from gi.repository import GLib
from wall_in_one.ui import quit_countdown
loop = GLib.MainLoop()
t0 = time.monotonic()
def done():
    os.write(1, f"done {time.monotonic() - t0:.2f}\n".encode()); loop.quit()
def cancelled():
    os.write(1, f"cancelled {time.monotonic() - t0:.2f}\n".encode())
    # As the application does after a cancel, ``linger`` goes on running a while.
    GLib.timeout_add(3000, loop.quit) if linger else loop.quit()
    return None
countdown = quit_countdown.Countdown(1, 10, 300, done=done, cancelled=cancelled)
countdown.start()
os.write(1, b"keys\n" if countdown.keys else b"no-keys\n")
loop.run()
"""


def _start(slave: int, *arguments: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, *arguments],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=slave,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        cwd=REPO,
    )


def _line(child: subprocess.Popen[bytes], seconds: float) -> str | None:
    assert child.stdout is not None
    ready, _, _ = select.select([child.stdout], [], [], seconds)
    return child.stdout.readline().decode().strip() if ready else None


def _drain(master: int) -> bytes:
    """Read what the child said on the terminal, so its output never fills; what it said."""
    said = b""
    while select.select([master], [], [], 0)[0]:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        said += chunk
    return said


@pytest.mark.parametrize("stopped", ["ctrl-s", "control-not-stopped"])
def test_r14v_a_key_cancels_while_the_terminal_output_is_stopped(stopped: str) -> None:
    """L3: Ctrl-S, then a key: the countdown is cancelled at once, not after Ctrl-Q. The
    control: the same key with the output flowing."""
    master, slave = os.openpty()
    child = _start(slave)
    try:
        assert _line(child, 30) == "keys"
        if stopped == "ctrl-s":
            os.write(master, b"\x13")  # Ctrl-S: the terminal stops its output
        else:
            _drain(master)
        time.sleep(0.7)  # past two of the countdown's 300 ms steps, each a line in place
        os.write(master, b"x")
        said = _line(child, 3.0)
        if stopped == "ctrl-s":
            os.write(master, b"\x11")  # Ctrl-Q, so the child can finish whatever it waits on
        _drain(master)
        child.wait(10)
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)  # the exact PID this test started
            child.wait(10)
        if child.stdout is not None:
            child.stdout.close()
        os.close(slave)
        os.close(master)
    assert said is not None and said.startswith("cancelled"), (
        f"{stopped}: a key didn't cancel within 3 s: {said!r}"
    )


@pytest.mark.parametrize("pasted", [200, 2])
def test_r14v_the_rest_of_a_paste_is_not_left_for_the_shell(pasted: int) -> None:
    """A paste of ``pasted`` bytes cancels the countdown, and none of it is left in the
    terminal's input once the child has gone. The control: a key and Enter, one read, which
    never left any."""
    master, slave = os.openpty()
    child = _start(slave)
    try:
        assert _line(child, 30) == "keys"
        # Ending in a newline, so what is left is a line the terminal, cooked again, hands on.
        os.write(master, b"a" * (pasted - 1) + b"\n")
        said = _line(child, 5.0)
        child.wait(10)
        _drain(master)
        left = select.select([slave], [], [], 0.2)[0]
        leftover = os.read(slave, 4096) if left else b""
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)  # the exact PID this test started
            child.wait(10)
        if child.stdout is not None:
            child.stdout.close()
        os.close(slave)
        os.close(master)
    assert said is not None and said.startswith("cancelled"), said
    assert leftover == b"", f"{len(leftover)} bytes of the paste left for the shell"


def test_r14w_the_last_words_wait_for_the_terminal_to_take_them() -> None:
    """R14W L1: Ctrl-S, a key: the countdown is cancelled at once, and "Quitting cancelled." is
    said once the terminal takes output again (Ctrl-Q), while the application goes on running,
    not lost."""
    master, slave = os.openpty()
    child = _start(slave, "linger")
    try:
        assert _line(child, 30) == "keys"
        os.write(master, b"\x13")  # Ctrl-S
        time.sleep(0.4)
        os.write(master, b"x")
        said = _line(child, 3.0)
        time.sleep(0.3)
        stopped = _drain(master)
        os.write(master, b"\x11")  # Ctrl-Q
        time.sleep(1.0)
        resumed = _drain(master)
        child.wait(10)
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)  # the exact PID this test started
            child.wait(10)
        if child.stdout is not None:
            child.stdout.close()
        os.close(slave)
        os.close(master)
    assert said is not None and said.startswith("cancelled"), said
    assert quit_countdown.CANCELLED.encode() not in stopped, stopped
    assert quit_countdown.CANCELLED.encode() in resumed, resumed


def test_r14w_a_stream_without_a_descriptor_is_written() -> None:
    """R14W N1: the terminal check doesn't leave out a stream that has no descriptor."""
    out = io.StringIO()
    assert quit_countdown._write(out, "said\n")
    assert out.getvalue() == "said\n"


def test_r14w_a_line_left_out_is_not_rewound_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """R14W N2: a second's line the terminal couldn't take isn't counted as shown, so the next
    one rewinds to the last shown, not over what came before it."""
    countdown = quit_countdown.Countdown(1, 5, 1000, done=lambda: None, cancelled=lambda: None)
    countdown._in_place = True
    monkeypatch.setattr(quit_countdown, "_write", lambda _out, _text: False)
    countdown._say()
    assert countdown._shown == ""
