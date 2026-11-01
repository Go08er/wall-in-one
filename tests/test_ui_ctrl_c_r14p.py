"""Review R14P's probes of the Ctrl-C countdown, adopted: each failed at 5cc9291, its control not.

* **L1:** a first Ctrl-C with none of the person's changes waiting waited silently for the
  application's own tails (a publication, a scan). It quits at once again, as at 55f26e1
  (`test_r14p_nothing_waiting_quits_at_once`, `…_during_a_scan`).
* **L2:** after ``ctl quit`` in ``--service``, a first Ctrl-C dropped what was waiting. It is a
  first Ctrl-C again: ``quit()``, which hands the number and the Settings on.
* **L3:** the count took in the application's own queued work (a scan's reconciliation).
* **L4:** SIGTERM during the countdown left the terminal in cbreak mode; SIGHUP and SIGQUIT too.
  A child of the test, on a pty that is its controlling terminal, runs the real countdown.
* **N1:** an exception in the countdown's tick held the lane for ever.
* **N2:** Esc pressed twice, read at once, wasn't Esc (any key cancels now, R14V).
* **N3:** a Ctrl-C after the countdown, while its careful quit saves, dropped what was queued
  without a word.
* **N4:** a real first SIGINT, sent to this process as the terminal sends it, through the
  handler production installs (it passed at 5cc9291 too: what the suite didn't exercise).

Real Application; the service is the suite's fake runtime. Children are signalled by the exact
pid this file started; the test process only by the one SIGINT in N4.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib  # noqa: E402

import wall_in_one.ui.app as app_module  # noqa: E402
from tests.test_ui_ctrl_c_countdown import (  # noqa: E402
    _counting,
    _ctrl_c,
    _Lane,
    _names,
    _quick_countdown,
    _run_to_the_end,
)
from tests.test_ui_next_window import application_lanes, run_application  # noqa: E402
from tests.test_ui_renderer_dials import _own, toolkit  # noqa: E402,F401
from tests.test_ui_renderer_settle_quit import _after, _rows, _scanned  # noqa: E402
from tests.test_ui_renderer_settle_shutdown import (  # noqa: E402
    _LANES,
    GIVE_UP,
    HANG,
    _join_lanes,
    _setup,
)
from wall_in_one import config  # noqa: E402
from wall_in_one.library.model import Kind  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

#: What a first Ctrl-C says while ``ctl quit`` saves the person's changes (the owner's words).
SAVING = (
    "Saving {changes} before quitting\u2026 press Ctrl-C again to quit immediately without them."
)


def _record(name: str, **facts: Any) -> None:
    """What a probe saw, in its output (``-s``): the review's own record."""
    print(f"\nR14P {name}: " + ", ".join(f"{key}={value!r}" for key, value in facts.items()))


# -- P1: `ctl quit` (request_quit) in --service, then the person's first Ctrl-C ------------------


@pytest.mark.parametrize("press", ["one-ctrl-c", "control-no-ctrl-c"])
def test_r14p_ctl_quit_then_a_first_ctrl_c_keeps_numbers_and_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    press: str,
) -> None:
    """--service. A save in progress; behind it a playlist edit, a Settings change and a
    renderer number. ``ctl quit`` (request_quit) waits for them; the person then presses Ctrl-C
    once in the --service terminal. At 55f26e1 that press was PyGObject's quit(), which handed
    the number and the Settings on (R41-2). Since the owner's "save first" (R14Q Q-L1) the
    quit ``ctl quit`` asked for goes on saving all three, and the press says so once, with no
    countdown (``tests/test_ui_ctrl_c_r14q.py`` has the press after it)."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=True, settle_seconds=2.0
    )
    lane = _Lane(application)
    shuffled = config.load_strict().shuffle
    seen: dict[str, Any] = {}

    def scenario() -> Iterator[Any]:
        yield "the first scan", _scanned(application, clip)
        speed, _brightness = _rows(application, clip)
        assert lane.create("In flight", held=True)
        yield "the save in progress", lane.started.is_set
        assert lane.create("Queued")
        assert application.update_settings_async(shuffle=not shuffled)
        speed.set_value(1.5)
        _LANES.extend(application_lanes(application))
        application.request_quit()  # what `ctl quit` does in --service (GLib.idle_add)
        yield "a moment", _after(0.2)
        seen["quit requested"] = application._quit_requested
        if press == "one-ctrl-c":
            _ctrl_c()
            seen["counting"] = _counting(application)
        lane.release_in(0.3)

    try:
        interrupted = _run_to_the_end(application, scenario(), service=True)
    finally:
        lane.close()
    err = capfd.readouterr().err
    speed = _own(clip, Kind.VIDEO).dial("video", "speed")
    shuffle_saved = config.load_strict().shuffle is not shuffled
    _record(
        f"P1[{press}]",
        seen=seen,
        interrupted=interrupted,
        speed=speed,
        shuffle_saved=shuffle_saved,
        names=sorted(_names()),
        line="Saving" in err,
        without_them="Quitting immediately without them." in err,
    )
    assert seen["quit requested"]
    assert speed == 1.5, "the number accepted before the person's first Ctrl-C was dropped"
    assert shuffle_saved, "the Settings change accepted before the first Ctrl-C was dropped"
    assert "Quitting" not in err and "Saving 3 changes before quitting in" not in err, err
    assert not interrupted
    if press == "one-ctrl-c":
        assert "Queued" in _names(), _names()
        assert err.count(SAVING.format(changes="3 changes")) == 1, err
    else:
        assert "Saving" not in err, err


# -- P3: a real SIGINT, sent as the terminal sends it, while the loop has nothing to do ----------


def test_r14p_a_real_sigint_while_the_loop_idles_quits_promptly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Nothing waiting. The scenario ends (the test's 10 ms tick goes), then a real SIGINT to
    this process from another thread, as the existing r13f tests send one, once the handler
    installed is known not to be Python's default. It must reach the handler promptly (the
    loop is woken), and quit at once, with no line."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=2.0
    )
    marks: dict[str, Any] = {}
    failures: list[str] = []
    original = getattr(application, "_ctrl_c", None)
    if original is not None:

        def timed() -> bool:
            marks.setdefault("handled", time.monotonic())
            return bool(original())

        monkeypatch.setattr(application, "_ctrl_c", timed)

    def send() -> None:
        handler = signal.getsignal(signal.SIGINT)
        marks["handler"] = getattr(handler, "__qualname__", repr(handler))
        if handler is signal.default_int_handler or not callable(handler):
            failures.append(f"not sent: the handler is {handler!r}")
            return
        marks["sent"] = time.monotonic()
        os.kill(os.getpid(), signal.SIGINT)  # as the terminal sends it: to the process

    timer = threading.Timer(0.6, send)

    def scenario() -> Iterator[Any]:
        yield "the first scan", _scanned(application, clip)
        _LANES.extend(application_lanes(application))
        timer.start()  # the scenario then ends: nothing of the test's wakes the loop

    _LANES.clear()
    interrupted = False
    try:
        try:
            run_application(application, scenario())
        except KeyboardInterrupt:
            interrupted = True
        marks["ended"] = time.monotonic()
    finally:
        timer.cancel()
        timer.join(GIVE_UP)
        _join_lanes()
    err = capfd.readouterr().err
    _record(
        "P3",
        handler=marks.get("handler"),
        to_handler=round(marks["handled"] - marks["sent"], 3) if "handled" in marks else None,
        to_end=round(marks["ended"] - marks["sent"], 3) if "sent" in marks else None,
        interrupted=interrupted,
        line="Saving" in err,
    )
    assert not failures, failures
    assert marks["handler"] == "Application._on_sigint", marks["handler"]
    assert "handled" in marks, "the handler never asked the loop"
    assert marks["ended"] - marks["sent"] < 1.0, "run() didn't end promptly after the SIGINT"
    assert interrupted, "a Ctrl-C that quits at once ends run() as a Ctrl-C ends it"
    assert "Saving" not in err


# -- P4: the count ("Saving N changes") counts the application's own work ------------------------


@pytest.mark.parametrize("variant", ["a-rescan-behind-the-save", "control-no-rescan"])
def test_r14p_the_count_is_only_the_persons_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    """The person's only change is being written (never counted). A library refresh ends
    meanwhile, and its reconciliation queues behind that write: the application's own work,
    which the count's docstring says is never counted. Then one Ctrl-C."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=2.0
    )
    _quick_countdown(monkeypatch)
    lane = _Lane(application)
    seen: dict[str, Any] = {}

    def scenario() -> Iterator[Any]:
        yield "the first scan", _scanned(application, clip)
        assert lane.create("In flight", held=True)
        yield "the save in progress", lane.started.is_set
        if variant != "control-no-rescan":
            application.refresh_library()
            yield "its reconciliation queued", lambda: len(application._authoring_queue) == 1
            task = application._authoring_queue[0]
            seen["queued"] = getattr(task.work, "__qualname__", repr(task.work))
        _LANES.extend(application_lanes(application))
        seen["count"] = application._changes_a_quit_would_drop()
        lane.release_in(0.3)
        _ctrl_c()

    try:
        _run_to_the_end(application, scenario(), service=False)
    finally:
        lane.close()
    err = capfd.readouterr().err
    lines = [line for line in err.splitlines() if line.startswith("Saving")]
    _record(f"P4[{variant}]", seen=seen, lines=lines)
    assert not lines, f"a countdown for changes the person never made: {lines}"


# -- P5: an exception inside the countdown's own timer -----------------------------------------


@pytest.mark.parametrize("variant", ["the-tick-raises", "control-no-fault"])
def test_r14p_an_exception_in_the_countdown_tick_doesnt_hold_for_ever(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    """A fault injected in the line's second rewrite (the first tick). With 20 ms steps the
    countdown is over in 0.1 s; 1.5 s later the hold must be gone and the quit under way."""
    from wall_in_one.ui import quit_countdown

    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=2.0
    )
    _quick_countdown(monkeypatch)
    init = quit_countdown.Countdown.__init__

    def in_place(self: Any, *arguments: Any, **keywords: Any) -> None:
        init(self, *arguments, **keywords)
        self._in_place = True  # standard error is the test's capture; a tick rewrites the line

    monkeypatch.setattr(quit_countdown.Countdown, "__init__", in_place)
    if variant == "the-tick-raises":
        say = quit_countdown.Countdown._say
        calls: list[int] = []

        def faulty(self: Any) -> None:
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("injected: the countdown's tick failed")
            say(self)

        monkeypatch.setattr(quit_countdown.Countdown, "_say", faulty)
    lane = _Lane(application)
    seen: dict[str, Any] = {}

    def scenario() -> Iterator[Any]:
        yield "the first scan", _scanned(application, clip)
        assert lane.create("In flight", held=True)
        yield "the save in progress", lane.started.is_set
        assert lane.create("Queued")
        _LANES.extend(application_lanes(application))
        lane.release_in(0.3)
        _ctrl_c()
        yield "well past the countdown", _after(1.5)
        seen["held"] = application._authoring_held
        seen["counting"] = _counting(application)
        seen["quit requested"] = application._quit_requested
        seen["written"] = list(lane.written)
        if application._window is not None and not application._quit_requested:
            _ctrl_c()  # out, the only way left

    try:
        interrupted = _run_to_the_end(application, scenario(), service=False)
    finally:
        lane.close()
    capfd.readouterr()
    _record(f"P5[{variant}]", seen=seen, interrupted=interrupted, names=sorted(_names()))
    assert not seen.get("held", False), "the hold was never let go"
    assert "Queued" in _names(), "the held edit was never saved"


# -- P6: a first Ctrl-C with nothing waiting, while the app's own publication runs --------------


@pytest.mark.parametrize("variant", ["a-publication-hangs", "control-no-hang"])
def test_r14p_nothing_waiting_quits_at_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    """A Settings change saved; its publication's reload hangs (``HANG`` s). Nothing the
    person changed is waiting, so the owner's spec says: quit at once, with no line."""
    hang = variant == "a-publication-hangs"
    application, clip, slow, _spans = _setup(
        monkeypatch,
        tmp_path,
        ui="classic",
        service=False,
        settle_seconds=2.0,
        reload_seconds=HANG if hang else 0.0,
    )
    lane = _Lane(application)
    marks: dict[str, Any] = {}

    def scenario() -> Iterator[Any]:
        yield "the first scan", _scanned(application, clip)
        slow.armed = True
        shuffled = config.load_strict().shuffle
        assert application.update_settings_async(shuffle=not shuffled)
        yield "its publication's reload", lambda: bool(slow.on_gtk)
        yield (
            "it saved",
            lambda: (
                config.load_strict().shuffle is not shuffled
                and not application._authoring_queue
                and not application._settings_authoring_running
            ),
        )
        _LANES.extend(application_lanes(application))
        marks["count"] = (
            application._changes_a_quit_would_drop()
            if hasattr(application, "_changes_a_quit_would_drop")
            else None
        )
        marks["pressed"] = time.monotonic()
        _ctrl_c()

    _LANES.clear()
    interrupted = False
    try:
        try:
            run_application(application, scenario())
        except KeyboardInterrupt:
            interrupted = True
        marks["ended"] = time.monotonic()
    finally:
        lane.close()
        _join_lanes()
    err = capfd.readouterr().err
    took = marks["ended"] - marks["pressed"]
    _record(
        f"P6[{variant}]",
        count=marks["count"],
        took=round(took, 2),
        interrupted=interrupted,
        line=[line for line in err.splitlines() if "Saving" in line or "Quitting" in line],
    )
    assert took < 1.0, f"a Ctrl-C with nothing waiting took {took:.2f}s to quit, saying nothing"


# -- P7: Esc pressed twice, read at once (any key cancels since R14V) --------------------------------------------------------


@pytest.mark.parametrize("pressed", [b"\x1b\x1b", b"\x1b"])
def test_r14p_esc_pressed_twice_still_cancels(
    monkeypatch: pytest.MonkeyPatch, pressed: bytes
) -> None:
    """The countdown on its own, its standard input a pty of the test's (as the fixer's
    ``terminal`` fixture). Two Esc presses that reach one read (the loop was busy for a moment)
    are still the person asking to cancel. Control: one Esc."""
    from wall_in_one.ui import quit_countdown

    master, slave = os.openpty()
    monkeypatch.setattr(quit_countdown, "keys_fd", lambda: slave)
    monkeypatch.setattr(quit_countdown, "keys_usable", lambda fd: fd == slave)
    ended: list[str] = []
    context = GLib.MainContext.default()
    try:
        running = quit_countdown.Countdown(
            1,
            5,
            100,
            done=lambda: ended.append("done"),
            cancelled=lambda: ended.append("cancelled"),
        )
        running.start()
        assert running.keys
        os.write(master, pressed)
        deadline = time.monotonic() + 5
        while not ended and time.monotonic() < deadline:
            context.iteration(True)
    finally:
        quit_countdown._restore(slave)
        os.close(slave)
        os.close(master)
    _record(f"P7[{pressed!r}]", ended=ended)
    assert ended == ["cancelled"], ended


# -- P2: the terminal after SIGTERM during the countdown (a child on a pty of its own) ----------

CHILD = r"""
import ctypes, fcntl, os, sys, termios, time
mode = sys.argv[1]
ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: SIGQUIT leaves no core
fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # the pty is this new session's terminal
if mode == "background":
    parent = os.getpid()
    other = os.fork()
    if other == 0:
        os.setpgid(0, 0)
        while os.getppid() == parent:
            time.sleep(0.05)
        os._exit(0)
    try:
        os.setpgid(other, other)
    except OSError:
        pass
    os.tcsetpgrp(0, other)  # another group of this session has the foreground now
from gi.repository import GLib
from wall_in_one.ui import quit_countdown
loop = GLib.MainLoop()
seconds = 1 if mode == "ends" else 30
countdown = quit_countdown.Countdown(1, seconds, 200, done=loop.quit, cancelled=loop.quit)
countdown.start()
print("keys" if countdown.keys else "no-keys", flush=True)
loop.run()
"""


def _cbreak(attributes: list[Any]) -> bool:
    lflag = attributes[3]
    return not lflag & termios.ICANON and not lflag & termios.ECHO


FATAL = {"sigterm": signal.SIGTERM, "sighup": signal.SIGHUP, "sigquit": signal.SIGQUIT}


@pytest.mark.parametrize(
    "ended_by", ["sigterm", "sighup", "sigquit", "control-sigint", "control-ends", "background"]
)
def test_r14p_the_terminal_after_the_process_ends_during_the_countdown(ended_by: str) -> None:
    """A child of this test, on a pty that is its controlling terminal (so the real
    ``keys_usable`` runs), starts a countdown and loops. It is then sent SIGTERM (``kill``,
    a session's end, ``systemctl --user stop``), SIGHUP (the terminal closed) or SIGQUIT
    (Ctrl-\\), and must still end by it; or, the controls, SIGINT (Python's exit runs, and
    atexit with it), or the countdown runs to its end; ``background``: another process group
    has the terminal's foreground, so a key must not be read."""
    master, slave = os.openpty()
    before = termios.tcgetattr(slave)
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    mode = "ends" if ended_by == "control-ends" else ended_by
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, mode],
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
        if ended_by in FATAL:
            os.kill(child.pid, FATAL[ended_by])  # the exact PID this test started
        elif ended_by in ("control-sigint", "background"):
            os.kill(child.pid, signal.SIGINT)
        facts["status"] = child.wait(30)
        facts["after"] = _cbreak(termios.tcgetattr(slave))
        facts["restored"] = termios.tcgetattr(slave) == before
        facts["stderr"] = child.stderr.read().decode()[-300:]  # type: ignore[union-attr]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(30)
        os.close(slave)
        os.close(master)
    _record(f"P2[{ended_by}]", **facts)
    if ended_by == "background":
        assert facts["keys"] == "no-keys" and not facts["during"]
        return
    assert facts["keys"] == "keys" and facts["during"], facts
    assert facts["restored"], f"the terminal was left in cbreak mode, echo off: {facts}"
    if ended_by in FATAL:
        assert facts["status"] == -FATAL[ended_by], f"it didn't end by that signal: {facts}"


# -- P6b: a first Ctrl-C with nothing waiting, during a library scan -----------------------------


@pytest.mark.parametrize("variant", ["a-slow-rescan", "control-quick-rescan"])
def test_r14p_nothing_waiting_quits_at_once_during_a_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    """The library folders change (a source-changing scan: the runtime's scan barrier), and
    the scan takes 6 s (a large library, a slow disk). Nothing the person changed is waiting
    to be saved once the Settings landed; one Ctrl-C during the scan."""
    from wall_in_one.session import LibraryRefreshPlan

    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=2.0
    )
    walk = LibraryRefreshPlan.scan
    began = threading.Event()
    armed: list[bool] = []

    def slow_walk(self: Any) -> Any:
        if armed:
            began.set()
            time.sleep(6.0 if variant == "a-slow-rescan" else 0.0)
        return walk(self)

    monkeypatch.setattr(LibraryRefreshPlan, "scan", slow_walk)
    marks: dict[str, Any] = {}

    def scenario() -> Iterator[Any]:
        yield "the first scan", _scanned(application, clip)
        armed.append(True)
        other = tmp_path / "more"
        other.mkdir()
        roots = [*config.load_strict().roots, other]
        assert application.update_settings_async(roots=roots)
        yield "the rescan", began.is_set
        yield (
            "the roots saved",
            lambda: (
                not application._settings_authoring_running and not application._authoring_queue
            ),
        )
        marks["scan held"] = application._runtime_library_scan_held
        marks["count"] = (
            application._changes_a_quit_would_drop()
            if hasattr(application, "_changes_a_quit_would_drop")
            else None
        )
        _LANES.extend(application_lanes(application))
        marks["pressed"] = time.monotonic()
        _ctrl_c()

    _LANES.clear()
    interrupted = False
    try:
        try:
            run_application(application, scenario())
        except KeyboardInterrupt:
            interrupted = True
        marks["ended"] = time.monotonic()
    finally:
        _join_lanes()
    err = capfd.readouterr().err
    took = marks["ended"] - marks["pressed"]
    _record(
        f"P6b[{variant}]",
        scan_held=marks["scan held"],
        count=marks["count"],
        took=round(took, 2),
        interrupted=interrupted,
        line=[line for line in err.splitlines() if "Saving" in line or "Quitting" in line],
    )
    assert took < 1.0, f"a Ctrl-C with nothing waiting took {took:.2f}s to quit, saying nothing"


# -- L4: the fatal signals are handled only while the countdown holds the terminal -------------


def test_the_fatal_signals_are_handled_only_while_the_terminal_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In this process, never raised: the handlers for SIGTERM, SIGHUP and SIGQUIT are the
    countdown's while it holds a terminal, and what they were once it stops (the application
    has none otherwise, review r13c N-9); with no terminal to hold, none is installed."""
    from wall_in_one.ui import quit_countdown

    signals = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
    before = {number: signal.getsignal(number) for number in signals}
    master, slave = os.openpty()
    try:
        for held in (True, False):
            monkeypatch.setattr(
                quit_countdown, "keys_fd", lambda held=held: slave if held else None
            )
            monkeypatch.setattr(quit_countdown, "keys_usable", lambda fd: fd == slave)
            running = quit_countdown.Countdown(
                1, 5, 1000, done=lambda: None, cancelled=lambda: None
            )
            running.start()
            during = {number: signal.getsignal(number) for number in signals}
            running.stop()
            after = {number: signal.getsignal(number) for number in signals}
            assert after == before, (held, after)
            changed = {number for number in signals if during[number] is not before[number]}
            # Ignored, or not installed from Python: left as they are.
            kept = {number for number in signals if before[number] in (signal.SIG_IGN, None)}
            assert changed == (set(signals) - kept if held else set()), (held, during)
    finally:
        quit_countdown._restore(slave)
        for number, handler in before.items():
            if handler is not None and signal.getsignal(number) is not handler:
                signal.signal(number, handler)
        os.close(slave)
        os.close(master)


# -- N3: a Ctrl-C after the countdown says what it drops --------------------------------------


@pytest.mark.parametrize("press", ["after-the-countdown", "control-no-second-press"])
def test_a_ctrl_c_after_the_countdown_says_it_quits_without_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    press: str,
) -> None:
    """A playlist edit behind a save in progress, a first Ctrl-C, its countdown (shortened) runs
    out, and the careful quit waits for that save. A Ctrl-C then drops the edit, as Ctrl-C again
    does, and says so. Before (5cc9291) it said nothing. The control lets the save end."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=2.0
    )
    _quick_countdown(monkeypatch)
    lane = _Lane(application)

    def scenario() -> Iterator[Any]:
        yield "the first scan", _scanned(application, clip)
        assert lane.create("In flight", held=True)
        yield "the save in progress", lane.started.is_set
        assert lane.create("Queued")
        _LANES.extend(application_lanes(application))
        _ctrl_c()
        yield "the countdown to run out", lambda: application._quit_requested
        assert not _counting(application) and not application._authoring_held
        if press == "after-the-countdown":
            _ctrl_c()
        lane.release_in(0.3)

    try:
        interrupted = _run_to_the_end(application, scenario(), service=False)
    finally:
        lane.close()
    err = capfd.readouterr().err
    if press == "after-the-countdown":
        assert interrupted
        assert "Queued" not in _names(), _names()
        assert err.count(app_module.QUIT_WITHOUT_THEM) == 1, err
        return
    assert not interrupted
    assert "Queued" in _names(), _names()
    assert app_module.QUIT_WITHOUT_THEM not in err, err
