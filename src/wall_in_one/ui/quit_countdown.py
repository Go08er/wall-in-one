"""The countdown a first Ctrl-C starts, in the terminal, before the changes still waiting are saved.

`Application` holds the changes still waiting to be saved while it runs (they
are saved when it ends, and dropped at a second Ctrl-C), and this says so on
standard error: rewritten in place once a second on a terminal, said once
otherwise. When standard input is the terminal this process is in the
foreground of, any other key there cancels the quit (what was typed before the
countdown started is thrown away, so that it doesn't). The terminal is then put
in cbreak mode, with echo off and Ctrl-C still a signal (so it is not a key
here), for as long as the countdown runs, and put back as it was: at the
countdown's end, a second Ctrl-C, a key,
an exception in the countdown's own code (`Countdown.stop` comes first on
each), the end of ``run()``, the process's exit (`_restore_all`), and a
SIGTERM, SIGHUP or SIGQUIT during the countdown, which end the process as
before once it is (`Countdown._hold_fatal_signals`). Not SIGKILL, which
nothing can catch. A terminal left in cbreak mode would keep the next
program from echoing what is typed. Sent to the background meanwhile
(Ctrl-Z, then ``bg``), it stops watching for a key and leaves the terminal as
the shell has it; what to put back is kept, and put back from the
foreground, if it is there again by one of those.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import select
import signal
import sys
import termios
import tty
from collections.abc import Callable
from typing import Any, Final, TextIO

from gi.repository import GLib

LOGGER = logging.getLogger(__name__)

#: The signals that end the process at once, which a terminal in cbreak mode
#: must not outlive: handled only while the countdown holds the terminal. One
#: that lands in the instant its handler is put back (`_let_fatal_signals_go`)
#: can be lost: CPython drops a signal whose Python handler is put back to
#: SIG_DFL while it is pending, and says so on standard error (bpo-43406); the
#: process carries on. Accepted: only never handling them (a terminal left in
#: cbreak) or never putting the handler back (a Python handler never runs while
#: GTK's thread is wedged, R14Q Q-N4) avoids it.
FATAL: Final = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)

#: The line that says the quit was cancelled, by a key.
CANCELLED: Final = "Quitting cancelled."

#: Each terminal put in cbreak mode here, by its descriptor: the attributes to put back,
#: kept until they are put back from the foreground (`_restore`).
_SAVED: dict[int, list[Any]] = {}


def keys_fd() -> int | None:
    """Standard input's descriptor, where a key is read; None when there is none."""
    try:
        return sys.stdin.fileno() if sys.stdin is not None else None
    except OSError, ValueError, AttributeError:
        return None  # closed, or not a file (a test's capture)


def keys_usable(fd: int) -> bool:
    """Whether ``fd`` is a terminal this process may read: one it is in the foreground of.

    A process in the background that reads its terminal is stopped (SIGTTIN),
    and one that isn't its controlling terminal has no foreground to be in.
    """
    try:
        return os.isatty(fd) and os.tcgetpgrp(fd) == os.getpgrp()
    except OSError:
        return False


def _in_the_background(fd: int) -> bool:
    """Whether this process is in the background of ``fd``, its controlling terminal.

    False when that can't be told (``fd`` hung up, or isn't this process's
    controlling terminal): only a shell's job control hands a terminal to
    another group, and then it knows.
    """
    try:
        return os.tcgetpgrp(fd) != os.getpgrp()
    except OSError:
        return False


def _cbreak(fd: int) -> bool:
    """Put the terminal ``fd`` in cbreak mode with echo off; whether it is.

    What to put back is recorded before the terminal is changed, so that a
    fatal signal's handler (`Countdown._hold_fatal_signals`) finds it whenever
    the signal lands.
    """
    try:
        saved = termios.tcgetattr(fd)
        mode = termios.tcgetattr(fd)
    except termios.error, OSError:
        return False
    tty.cfmakecbreak(mode)  # ICANON and ECHO off; ISIG stays, so Ctrl-C is still SIGINT
    fresh = fd not in _SAVED
    if fresh:
        _SAVED[fd] = saved
    try:
        termios.tcsetattr(fd, termios.TCSANOW, mode)
    except termios.error, OSError:
        if fresh:
            _SAVED.pop(fd, None)
        return False
    return True


def _restore(fd: int) -> None:
    """Put the terminal ``fd`` back as it was before `_cbreak`, if it isn't yet. Never raises.

    The attributes are forgotten only once they are set, so a `FATAL`
    signal's handler that runs meanwhile, whichever of the process's threads
    the signal came to, still finds them and sets them itself; setting them
    twice does no harm. (Blocking the signals here would hold them off this
    thread only.) A process sent to the background since (Ctrl-Z, then
    ``bg``) doesn't set them: the shell has the terminal now, in a mode of
    its own, which is not this process's to set (Codex R41-5). It keeps them,
    for a later call once it is in the foreground again (``fg``), at the
    countdown's end or the process's exit: a shell that saves a stopped job's
    mode gives cbreak back then (R14T T-L1). SIGTTOU is
    blocked while they are set, for one sent there in between, which would
    otherwise be stopped by it here. TCSANOW, never a wait for output to
    drain, which a terminal held by Ctrl-S would make last for ever at the
    process's exit.
    """
    saved = _SAVED.get(fd)
    if saved is None:
        return
    if _in_the_background(fd):
        return
    try:
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    except OSError, ValueError:
        blocked = None
    try:
        with contextlib.suppress(termios.error, OSError):
            termios.tcsetattr(fd, termios.TCSANOW, saved)
    finally:
        if blocked is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    _SAVED.pop(fd, None)


def _restore_all() -> None:
    """Every terminal still in cbreak mode, back as it was: at the process's exit, whatever ran."""
    for fd in tuple(_SAVED):
        _restore(fd)


atexit.register(_restore_all)


def say(line: str) -> None:
    """``line`` on standard error, on a line of its own. Never raises."""
    _write(sys.stderr, f"{line}\n")


def _changes(count: int) -> str:
    return "1 change" if count == 1 else f"{count} changes"


def saving(changes: int) -> str:
    """What a first Ctrl-C says when a quit it didn't start is already saving them: no countdown."""
    return (
        f"Saving {_changes(changes)} before quitting\u2026 press Ctrl-C again to quit "
        "immediately without them."
    )


def _write(out: TextIO | None, text: str) -> bool:
    """Say ``text`` on ``out``; False when it is a terminal that can't take it now.

    A terminal whose output was stopped (Ctrl-S) takes nothing until Ctrl-Q,
    and a write would wait for that on GTK's thread, the whole application
    with it: a key's cancel, and a SIGINT sent with ``kill`` (R14V L3; a
    Ctrl-C typed there starts the output again). The text is left out then,
    and the caller says whether it is said later (`Countdown._finish`). A
    stream with no descriptor of its own is written as before.
    """
    if not text or out is None:
        return True
    try:
        try:
            fd: int | None = out.fileno()
        except OSError, ValueError:
            fd = None
        if fd is not None and os.isatty(fd):
            _, ready, _ = select.select([], [fd], [], 0)
            if not ready:
                return False
        out.write(text)
        out.flush()
    except OSError, ValueError:
        pass  # standard error closed: nothing to say it on
    return True


class Countdown:
    """One countdown: its line, its timer, and, when there is one, its watch of the terminal.

    ``changes``: how many changes a second Ctrl-C would drop, said in the line.
    ``done`` runs once the last second has passed, ``cancelled`` once a key was
    pressed; the countdown has stopped (`stop`) before either runs. What
    ``cancelled`` returns is said in place of `CANCELLED`, when it isn't
    None. GTK's thread only.
    """

    def __init__(
        self,
        changes: int,
        seconds: int,
        tick_ms: int,
        *,
        done: Callable[[], None],
        cancelled: Callable[[], str | None],
    ) -> None:
        self._changes = changes
        self._left = seconds
        self._tick_ms = tick_ms
        self._done = done
        self._cancelled = cancelled
        self._out = sys.stderr
        try:
            self._in_place = bool(self._out is not None and self._out.isatty())
        except OSError, ValueError, AttributeError:
            self._in_place = False
        self._fd: int | None = None
        self._timer = 0
        self._watch = 0
        #: The line as last written in place, while it isn't finished yet.
        self._shown = ""
        #: The countdown's last words, while the terminal can't take them (`_finish`), and
        #: the watch that says them once it can.
        self._later = ""
        self._later_watch = 0
        #: Each fatal signal handled while the terminal is held: its handler, and what it replaced.
        self._fatal: dict[int, tuple[Callable[[int, Any], None], Any]] = {}

    @property
    def keys(self) -> bool:
        """Whether a key is being watched for: the line offers it only then."""
        return bool(self._watch)

    def start(self) -> None:
        fd = keys_fd()
        held = False
        if fd is not None and keys_usable(fd):
            # Before the terminal is changed: a fatal signal from here on puts it back.
            self._hold_fatal_signals()
            held = _cbreak(fd)
            if not held:
                self._let_fatal_signals_go()
            else:
                # What was typed before the countdown started isn't a key pressed for it.
                with contextlib.suppress(termios.error, OSError):
                    termios.tcflush(fd, termios.TCIFLUSH)
        if held and fd is not None:
            self._fd = fd
            channel = GLib.IOChannel.unix_new(fd)
            self._watch = GLib.io_add_watch(
                channel,
                GLib.PRIORITY_DEFAULT,
                GLib.IOCondition.IN
                | GLib.IOCondition.HUP
                | GLib.IOCondition.ERR
                | GLib.IOCondition.NVAL,
                self._key,
            )
        self._say()
        self._timer = GLib.timeout_add(self._tick_ms, self._tick)

    def stop(self, said: str = "") -> None:
        """Stop: the timer, the watch and the terminal's mode; the line finished, then ``said``.

        Idempotent, and it never raises: every way the countdown ends comes here.
        """
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        if self._fd is not None:
            _restore(self._fd)
            self._fd = None
        self._let_fatal_signals_go()
        text = "\n" if self._shown else ""
        self._shown = ""
        self._finish(text + (f"{said}\n" if said else ""))

    def _hold_fatal_signals(self) -> None:
        """While the terminal is in cbreak mode: SIGTERM, SIGHUP and SIGQUIT put it back first.

        Each then puts back what it replaced and sends itself the signal
        again, so that the process ends by it as it would have. One that is
        ignored stays ignored, and one not installed from Python is left as
        it is. The application has none of its own otherwise (review r13c,
        N-9): `_let_fatal_signals_go` takes them out as the countdown stops.
        """
        for number in FATAL:
            try:
                previous = signal.getsignal(number)
            except ValueError, OSError:
                continue
            if previous is None or previous == signal.SIG_IGN:
                continue

            def fatal(caught: int, _frame: Any, previous: Any = previous) -> None:
                _restore_all()
                with contextlib.suppress(ValueError, OSError, TypeError):
                    signal.signal(caught, previous)
                os.kill(os.getpid(), caught)

            try:
                signal.signal(number, fatal)
            except ValueError, OSError:
                continue  # off the main thread: nothing installed
            self._fatal[number] = (fatal, previous)

    def _let_fatal_signals_go(self) -> None:
        """Put back what `_hold_fatal_signals` replaced, where its handler is still installed."""
        fatal, self._fatal = self._fatal, {}
        for number, (handler, previous) in fatal.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                if signal.getsignal(number) is handler:
                    signal.signal(number, previous)

    def _line(self) -> str:
        changes = _changes(self._changes)
        # In place it counts down; said once, the line says how long.
        when = f"{self._left}…" if self._in_place else f"{self._left} seconds…"
        key = ", or any other key to cancel quitting" if self.keys else ""
        return (
            f"Saving {changes} before quitting in {when} press Ctrl-C again to quit "
            f"immediately without them{key}."
        )

    def _say(self) -> None:
        line = self._line()
        if not self._in_place:
            self._write(f"{line}\n")
            return
        # Left out while the terminal can't take it: the next one rewinds to the last shown.
        if self._write(self._rewind() + line):
            self._shown = line

    def _rewind(self) -> str:
        """Back to where the line began: ``\\r``, then up the rows it wrapped onto, if it did."""
        if not self._shown:
            return "\r"  # over the ``^C`` the terminal echoed
        try:
            columns = os.get_terminal_size(self._out.fileno()).columns
        except OSError, ValueError, AttributeError:
            columns = 0
        rows = (len(self._shown) - 1) // columns if columns > 0 else 0
        return "\r" + (f"\x1b[{rows}A" if rows else "")

    def _write(self, text: str) -> bool:
        return _write(self._out, text)

    def _finish(self, text: str) -> None:
        """Say ``text``, the countdown's last words; once the terminal takes them, if it can't now.

        Unlike a second's line, these say how the countdown ended ("Quitting cancelled."),
        and the application may go on running: after Ctrl-Q they are there (R14W L1). In
        order: what comes while some wait waits behind them.
        """
        if not text:
            return
        if not self._later and self._write(text):
            return
        self._later += text
        if self._later_watch:
            return
        try:
            channel = GLib.IOChannel.unix_new(self._out.fileno())
        except OSError, ValueError, AttributeError:
            self._later = ""
            return
        self._later_watch = GLib.io_add_watch(
            channel,
            GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.OUT | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._said_later,
        )

    def _said_later(self, _channel: GLib.IOChannel, condition: GLib.IOCondition) -> bool:
        if condition & GLib.IOCondition.OUT and not self._write(self._later):
            return GLib.SOURCE_CONTINUE
        self._later = ""
        self._later_watch = 0
        return GLib.SOURCE_REMOVE

    def _tick(self) -> bool:
        try:
            self._left -= 1
            if self._left > 0:
                if self._in_place:
                    self._say()
                return GLib.SOURCE_CONTINUE
        except Exception:
            # Toward saving, never a hold for ever: the countdown ends as if its time were up.
            LOGGER.exception("the countdown to quitting failed; saving and quitting now")
        self._timer = 0  # removed by returning
        self.stop()
        self._done()
        return GLib.SOURCE_REMOVE

    def _key(self, _channel: GLib.IOChannel, condition: GLib.IOCondition) -> bool:
        fd = self._fd
        if fd is None:
            self._watch = 0
            return GLib.SOURCE_REMOVE
        try:
            if not keys_usable(fd):
                # Sent to the background since it started (Ctrl-Z, then ``bg``): a read now
                # would stop the process (SIGTTIN), held changes and all (Codex R41-5). A
                # terminal that hung up comes here too. No more keys from it; the countdown
                # goes on, and the terminal is put back from the foreground, if the process is
                # there again by then: at the countdown's end, by a fatal signal's handler
                # during it, or at the exit (`_restore`).
                self._watch = 0
                return GLib.SOURCE_REMOVE
            pressed = b""
            if condition & GLib.IOCondition.IN:
                try:
                    pressed = os.read(fd, 64)
                except OSError:
                    pressed = b""
            if not pressed:
                # The terminal hung up, or can't be read: no key from it, and back as it was.
                self._watch = 0
                _restore(fd)
                self._fd = None
                return GLib.SOURCE_REMOVE
            # The rest of what came with the key (a paste is one read of up to 64 bytes) is
            # not for the shell to run once the application has exited (R14V).
            with contextlib.suppress(termios.error, OSError):
                termios.tcflush(fd, termios.TCIFLUSH)
        except Exception:
            # As in `_tick`: the countdown ends, toward saving.
            LOGGER.exception("reading a key failed; saving and quitting now")
            self._watch = 0  # removed by returning
            self.stop()
            self._done()
            return GLib.SOURCE_REMOVE
        self._watch = 0  # removed by returning
        self.stop()
        instead = self._cancelled()
        self._finish(f"{instead or CANCELLED}\n")
        return GLib.SOURCE_REMOVE
