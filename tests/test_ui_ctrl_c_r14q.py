"""Review R14Q's probes of the R14P round, adopted: each failed at 5df4cf0, and its control didn't.

* **Q-N1:** a fatal signal that lands as the countdown puts the terminal back (inside
  ``_restore``, between taking the saved attributes and setting them), or as it starts (after
  cbreak, before its handlers), left the terminal in cbreak mode. The handlers now come before
  cbreak, the attributes are recorded before the terminal is changed, and ``_restore`` blocks the
  fatal signals from before it takes them until they are set. A child on a pty that is its
  controlling terminal runs the real countdown; where its signal lands is pinned by wrapping
  ``signal.pthread_sigmask`` or ``signal.getsignal`` in the child.
* **Q-N2:** ``ctl quit`` during the countdown, then Esc, said "Quitting cancelled." and quit all
  the same. It says that the quit goes on, and why.

Children are signalled by the exact pid this file started. No real terminal: ptys of the test's
own, and the child is never dumpable (no core from SIGQUIT).
"""

from __future__ import annotations

import importlib
import os
import select
import signal
import subprocess
import sys
import termios
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import wall_in_one.ui.app as app_module  # noqa: E402
from tests.test_ui_ctrl_c_countdown import (  # noqa: E402
    _counting,
    _ctrl_c,
    _Lane,
    _names,
    _run_to_the_end,
)
from tests.test_ui_next_window import application_lanes  # noqa: E402
from tests.test_ui_renderer_dials import toolkit  # noqa: E402,F401
from tests.test_ui_renderer_settle_quit import _after, _scanned  # noqa: E402
from tests.test_ui_renderer_settle_shutdown import _LANES, _setup  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

#: What Esc says when ``ctl quit`` asked to quit during the countdown (the coordinator's words).
GOES_ON = "Quitting goes on: wall-in-one ctl quit asked for it."


def _record(name: str, **facts: Any) -> None:
    """What a probe saw, in its output (``-s``): the review's own record."""
    print(f"\nR14Q {name}: " + ", ".join(f"{key}={value!r}" for key, value in facts.items()))


# -- Q-N2: ``ctl quit`` during the countdown, then Esc ---------------------------------------


@pytest.mark.parametrize("variant", ["ctl-quit-then-esc", "control-esc-alone"])
def test_r14q_ctl_quit_during_the_countdown_then_esc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    """--service, standard input a pty of the test's (the countdown tests' stub). A playlist edit
    behind a save in progress; a first Ctrl-C (countdown, 5 s); ``ctl quit`` arrives; Esc. The
    quit ``ctl quit`` asked for goes on, saving the edit, and Esc says so, never "Quitting
    cancelled.". Before (5df4cf0) it said that, and quit all the same. The control: Esc alone
    cancels, and the application is still running 1.5 s later."""
    countdown = importlib.import_module("wall_in_one.ui.quit_countdown")
    master, slave = os.openpty()
    monkeypatch.setattr(countdown, "keys_fd", lambda: slave)
    monkeypatch.setattr(countdown, "keys_usable", lambda fd: fd == slave)
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=True, settle_seconds=2.0
    )
    lane = _Lane(application)
    marks: dict[str, Any] = {}
    ctl_quit = variant == "ctl-quit-then-esc"

    def scenario() -> Iterator[Any]:
        yield "the first scan", _scanned(application, clip)
        assert lane.create("In flight", held=True)
        yield "the save in progress", lane.started.is_set
        assert lane.create("Queued")
        _LANES.extend(application_lanes(application))
        _ctrl_c()
        yield "the countdown", lambda: _counting(application)
        counting = application._quit_countdown
        assert counting is not None and counting.keys, "Esc isn't offered"
        if ctl_quit:
            application.request_quit()  # `ctl quit` during the countdown
            yield "a moment", _after(0.2)
        os.write(master, b"\x1b")
        yield "the cancel", lambda: not _counting(application)
        marks["quit requested after Esc"] = application._quit_requested
        marks["held after Esc"] = application._authoring_held
        lane.release.set()
        yield "a while after Esc", _after(1.5)
        marks["still running 1.5 s after Esc"] = True
        application.request_quit()  # the test's own end

    try:
        interrupted = _run_to_the_end(application, scenario(), service=True)
    finally:
        lane.close()
        countdown._restore(slave)
        os.close(slave)
        os.close(master)
    err = capfd.readouterr().err
    _record(f"Q3[{variant}]", marks=marks, interrupted=interrupted, names=sorted(_names()))
    assert not marks["held after Esc"], "Esc left the lane held"
    assert not interrupted
    assert "Queued" in _names(), _names()
    if ctl_quit:
        assert marks["quit requested after Esc"]
        assert err.count(GOES_ON) == 1, err
        assert app_module.QUIT_GOES_ON == GOES_ON
        assert countdown.CANCELLED not in err, err
        assert "still running 1.5 s after Esc" not in marks, "the quit ctl quit asked for stopped"
        return
    assert not marks["quit requested after Esc"]
    assert err.count(countdown.CANCELLED) == 1, err
    assert GOES_ON not in err, err
    assert marks.get("still running 1.5 s after Esc"), "Esc alone, and the application quit"


# -- Q-N1: a fatal signal pinned inside ``_restore``, or as the countdown starts ---------------

RACE_CHILD = r"""
import ctypes, fcntl, os, signal, sys, termios
mode = sys.argv[1]
ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: never a core
fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # the pty is this new session's terminal
from gi.repository import GLib
from wall_in_one.ui import quit_countdown
# Where a signal lands, pinned: on `_restore`'s SIG_BLOCK, as it begins to put the terminal
# back (at 5df4cf0 that was between taking the saved attributes and setting them); on its
# SIG_SETMASK, once they are set.
WHEN = {
    "stop-then-term": (signal.SIG_BLOCK, signal.SIGTERM),
    "control-stop-then-term-after": (signal.SIG_SETMASK, signal.SIGTERM),
    "term-then-hup": (signal.SIG_BLOCK, signal.SIGHUP),
    "control-term-then-hup-after": (signal.SIG_SETMASK, signal.SIGHUP),
}
real = signal.pthread_sigmask
fired = []
def pinned(how, mask):
    when = WHEN.get(mode)
    if when is not None and how == when[0] and not fired:
        fired.append(1)
        os.kill(os.getpid(), when[1])
    return real(how, mask)
signal.pthread_sigmask = pinned
real_getsignal = signal.getsignal
armed = []
def pinned_getsignal(number):
    # start-then-term: SIGTERM lands as the countdown starts, on the first getsignal of
    # `_hold_fatal_signals` (at 5df4cf0 that came after cbreak; it comes before it now).
    if mode == "start-then-term" and armed and not fired:
        fired.append(1)
        os.kill(os.getpid(), signal.SIGTERM)
    return real_getsignal(number)
signal.getsignal = pinned_getsignal
loop = GLib.MainLoop()
# stop-*: the countdown runs to its own end (one 200 ms step) and stops; term-*: the test sends
# SIGTERM while it counts.
seconds = 1 if mode.startswith(("stop", "control-stop")) else 30
countdown = quit_countdown.Countdown(1, seconds, 200, done=loop.quit, cancelled=loop.quit)
armed.append(1)
countdown.start()
print("keys" if countdown.keys else "no-keys", flush=True)
loop.run()
print("loop ended", flush=True)
"""


def _cbreak(attributes: list[Any]) -> bool:
    lflag = attributes[3]
    return not lflag & termios.ICANON and not lflag & termios.ECHO


@pytest.mark.parametrize(
    "mode",
    [
        "start-then-term",
        "stop-then-term",
        "control-stop-then-term-after",
        "term-then-hup",
        "control-term-then-hup-after",
    ],
)
def test_r14q_a_fatal_signal_landing_inside_restore(mode: str) -> None:
    """The real countdown in a child on a pty that is its controlling terminal. A fatal signal
    pinned to land inside ``_restore``: as the countdown stops by itself (SIGTERM), or inside the
    SIGTERM handler's own restore (SIGHUP, as systemd sends SIGTERM then SIGHUP to a session's
    processes). Controls: the same signal landing just after ``tcsetattr``."""
    master, slave = os.openpty()
    before = termios.tcgetattr(slave)
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    child = subprocess.Popen(
        [sys.executable, "-c", RACE_CHILD, mode],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
        cwd=REPO,
    )
    facts: dict[str, Any] = {}
    try:
        ready, _, _ = select.select([child.stdout], [], [], 30)
        assert ready, "the child never started its countdown"
        facts["keys"] = child.stdout.readline().decode().strip()  # type: ignore[union-attr]
        facts["during"] = _cbreak(termios.tcgetattr(slave))
        if mode == "start-then-term":
            # It dies inside start(), before its line: keys and during are set here, not read.
            facts["keys"], facts["during"] = "keys", True
            facts["set_not_read"] = "keys, during"
        if mode.startswith(("term", "control-term")):
            os.kill(child.pid, signal.SIGTERM)  # the exact PID this test started
        facts["status"] = child.wait(30)
        facts["after_cbreak"] = _cbreak(termios.tcgetattr(slave))
        facts["restored"] = termios.tcgetattr(slave) == before
        facts["stdout"] = child.stdout.read().decode()  # type: ignore[union-attr]
        facts["stderr"] = child.stderr.read().decode()[-200:]  # type: ignore[union-attr]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(30)
        os.close(slave)
        os.close(master)
    _record(f"Q2race[{mode}]", **facts)
    assert facts["keys"] == "keys" and facts["during"], facts
    assert facts["status"] < 0, f"it didn't end by a signal: {facts}"
    assert facts["restored"], f"the terminal was left in cbreak mode, echo off: {facts}"
