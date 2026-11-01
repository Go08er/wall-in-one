"""A first Ctrl-C saves what is waiting after a countdown; a second quits without it; any other key cancels.

The owner's request (r14o): a first Ctrl-C quits as ``ctl quit`` does (`Application.request_quit`),
but the changes accepted and not yet being written are held first, for a countdown said on
standard error, so that a second Ctrl-C can quit without them and any other key in the terminal can
cancel the quit. Before (55f26e1), a first Ctrl-C was PyGObject's fallback, ``quit()``, which handed on
renderer numbers and coalesced Settings (R41-2) but dropped every other task queued behind a save
in progress: a playlist, pairing or schedule edit (review R14, P3).

Never a real signal: each Ctrl-C calls the handler installed for SIGINT, as Python calls it when
one lands (at 55f26e1 that is PyGObject's, which calls ``quit()``). The terminal is a pty of the
test's own, never the real one: it is the countdown's standard input, and the check that this
process is in its foreground (which a pty that isn't the controlling terminal can't pass) is
stubbed for it. Real Application; the service is the suite's fake runtime.
"""

from __future__ import annotations

import importlib
import io
import os
import signal
import termios
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib  # noqa: E402

import wall_in_one.ui.app as app_module  # noqa: E402
from tests.test_ui_next_window import Step, application_lanes, run_application  # noqa: E402
from tests.test_ui_renderer_dials import _own, toolkit  # noqa: E402,F401
from tests.test_ui_renderer_settle_quit import _after, _rows, _scanned  # noqa: E402
from tests.test_ui_renderer_settle_shutdown import (  # noqa: E402
    _LANES,
    GIVE_UP,
    HANG,
    _join_lanes,
    _run,
    _setup,
)
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.library import pairings, playlists  # noqa: E402
from wall_in_one.library.model import Kind  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402

#: The line, said once (standard error isn't a terminal here), for ``changes``; ``key`` when the
#: terminal is standard input.
ONCE = (
    "Saving {changes} before quitting in 5 seconds… press Ctrl-C again to quit immediately "
    "without them{key}."
)
KEY_PART = ", or any other key to cancel quitting"


def _line(changes: str, *, key: bool = False) -> str:
    return ONCE.format(changes=changes, key=KEY_PART if key else "")


def _ctrl_c() -> None:
    """A Ctrl-C, as Python hands one to the handler installed for SIGINT: called, never raised."""
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler) and handler is not signal.default_int_handler, handler
    handler(signal.SIGINT, None)


def _counting(application: Application) -> bool:
    """Whether a first Ctrl-C's countdown runs (never at 55f26e1, which has none)."""
    return getattr(application, "_quit_countdown", None) is not None


def _quick_countdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Five steps of 20 ms, not of a second (no such constant at 55f26e1)."""
    monkeypatch.setattr(app_module, "QUIT_COUNTDOWN_TICK_MS", 20, raising=False)


def _ready(application: Application) -> bool:
    """Whether a new edit would be admitted (`Application.authoring_ready`), asked afresh."""
    return application.authoring_ready


def _names() -> set[str]:
    """The playlists ``playlists.json`` holds, by name."""
    return {playlist.name for playlist in playlists.load().values()}


class _Lane:
    """A playlist edit made on the authoring lane, and one held there as the save in progress."""

    def __init__(self, application: Application) -> None:
        self.application = application
        self.started = threading.Event()
        self.release = threading.Event()
        #: Each playlist whose write the worker began, by name.
        self.written: list[str] = []
        self.timers: list[threading.Timer] = []

    def create(self, name: str, *, held: bool = False) -> bool:
        store = self.application.session.playlists  # on GTK; written on the worker

        def work() -> playlists.Playlist:
            self.written.append(name)
            if held:
                self.started.set()
                if not self.release.wait(GIVE_UP):
                    raise RuntimeError("the save in progress was never let go")
            return store.create(name)

        return self.application.authoring_action_async(work, lambda _made: None)

    def release_in(self, seconds: float) -> None:
        timer = threading.Timer(seconds, self.release.set)
        self.timers.append(timer)
        timer.start()

    def close(self) -> None:
        self.release.set()
        for timer in self.timers:
            timer.cancel()
            timer.join(GIVE_UP)


def _run_to_the_end(application: Application, scenario: Iterator[Step], *, service: bool) -> bool:
    """`_run`; whether ``run()`` ended with KeyboardInterrupt, as a Ctrl-C that quits at once does
    it."""
    try:
        _run(application, scenario, service=service)
    except KeyboardInterrupt:
        return True
    return False


# -- (a) a first Ctrl-C: the edit queued behind a save in progress is saved --------------------


@pytest.mark.parametrize("ui", ["classic", "next"])
def test_a_first_ctrl_c_saves_a_playlist_edit_queued_behind_a_save_in_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    ui: str,
) -> None:
    """A playlist created while another playlist's save is being written, then one Ctrl-C.
    Before (55f26e1): the fallback's ``quit()`` dropped the queued one, and ``run()`` ended with
    KeyboardInterrupt. Now both are saved, after the countdown, which says "1 change" (the one
    being written is never stopped, so it isn't one), and ``run()`` returns as ``ctl quit``'s
    does."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui=ui, service=False, settle_seconds=2.0
    )
    _quick_countdown(monkeypatch)
    lane = _Lane(application)

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        assert lane.create("In flight", held=True)
        yield "the save in progress", lane.started.is_set
        assert lane.create("Queued")
        _LANES.extend(application_lanes(application))
        lane.release_in(0.3)
        _ctrl_c()

    try:
        interrupted = _run_to_the_end(application, scenario(), service=False)
    finally:
        lane.close()
    err = capfd.readouterr().err
    assert _names() >= {"In flight", "Queued"}, f"{ui}: the queued edit was lost: {_names()}"
    assert lane.written == ["In flight", "Queued"], lane.written
    assert err.count(_line("1 change")) == 1, err
    assert app_module.QUIT_WITHOUT_THEM not in err, err
    assert not interrupted, "a first Ctrl-C's careful quit ended run() with KeyboardInterrupt"
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "handler not restored"


# -- (b, e) a second Ctrl-C during the countdown -------------------------------------------------


def test_a_second_ctrl_c_during_the_countdown_quits_without_what_is_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A save in progress; behind it a playlist edit, a Settings change and a renderer number,
    each of a kind the countdown holds: "3 changes". A second Ctrl-C at once: ``run()`` ends with
    KeyboardInterrupt, none of the three is written, or even started (the countdown is a real
    one, so nothing could start by its end), and the save in progress is written all the same
    (e). Today's single Ctrl-C dropped the playlist edit too, and handed the other two on."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=2.0
    )
    lane = _Lane(application)
    started: list[str] = []
    set_dial = pairings.Store.set_dial

    def recorded_dial(self: pairings.Store, *arguments: Any, **keywords: Any) -> Any:
        if threading.current_thread() is not threading.main_thread():
            started.append("number")
        return set_dial(self, *arguments, **keywords)

    monkeypatch.setattr(pairings.Store, "set_dial", recorded_dial)
    update = config.update

    def recorded_update(changes: dict[str, Any], *arguments: Any, **keywords: Any) -> Any:
        if threading.current_thread() is not threading.main_thread():
            started.append(f"settings {sorted(changes)}")
        return update(changes, *arguments, **keywords)

    monkeypatch.setattr(config, "update", recorded_update)
    shuffled = config.load_strict().shuffle

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, _brightness = _rows(application, clip)
        assert lane.create("In flight", held=True)
        yield "the save in progress", lane.started.is_set
        assert lane.create("Queued")
        assert application.update_settings_async(shuffle=not shuffled)
        speed.set_value(1.5)  # waits out its settle time
        _LANES.extend(application_lanes(application))
        _ctrl_c()
        yield "the countdown", lambda: _counting(application)
        assert application._authoring_held
        _ctrl_c()
        lane.release_in(0.3)  # once the loop has ended: still written

    try:
        interrupted = _run_to_the_end(application, scenario(), service=False)
    finally:
        lane.close()
    err = capfd.readouterr().err
    assert interrupted, "a second Ctrl-C didn't end run() as a Ctrl-C ends it"
    assert lane.written == ["In flight"], f"a write was started for a dropped edit: {lane.written}"
    assert not started, f"a write was started for a dropped change: {started}"
    assert "In flight" in _names() and "Queued" not in _names(), _names()
    assert config.load_strict().shuffle is shuffled
    assert _own(clip, Kind.VIDEO).dial("video", "speed") is None
    assert err.count(_line("3 changes")) == 1, err
    assert err.count(app_module.QUIT_WITHOUT_THEM) == 1, err
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "handler not restored"


# -- (c) web wallpapers turned off is never dropped ---------------------------------------------


@pytest.mark.parametrize("variant", ["web-off-waiting", "control-web-off-alone"])
def test_web_turned_off_while_waiting_is_saved_by_a_quit_without_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    """``--service``, web wallpapers on and published. Behind a save in progress: Allow web
    wallpapers turned off, and (``web-off-waiting``) a playlist edit. A first Ctrl-C counts down
    for "1 change", not two: the turn-off is never dropped. A second quits without the edit; the
    turn-off is saved all the same, and the file on disk is replaced with the one that plays
    nothing (the shutdown's web-off safety write). ``control-web-off-alone``: with only the
    turn-off waiting there is nothing a second Ctrl-C would drop, so no countdown and no line:
    the first Ctrl-C quits at once, handing the turn-off on, and the file is replaced all the
    same, as at 55f26e1 (R41-2)."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=True, settle_seconds=2.0, allow_web=True
    )
    lane = _Lane(application)
    alone = variant.startswith("control")

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        assert runtime_config.allows_web_wallpapers(paths.runtime_config_path())
        assert lane.create("In flight", held=True)
        yield "the save in progress", lane.started.is_set
        assert application.update_settings_async(allow_web_wallpapers=False)
        if not alone:
            assert lane.create("Queued")
        _LANES.extend(application_lanes(application))
        _ctrl_c()
        if alone:
            lane.release_in(0.3)
            return
        yield "the countdown", lambda: _counting(application)
        _ctrl_c()
        lane.release_in(0.3)

    try:
        interrupted = _run_to_the_end(application, scenario(), service=True)
    finally:
        lane.close()
    err = capfd.readouterr().err
    assert config.load_strict().allow_web_wallpapers is False, "the turn-off was dropped"
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path()), (
        "settings.toml says web wallpapers are off, and the file on disk still allows them"
    )
    # Saved once the publications had ended: the shutdown's web-off safety write replaced it.
    assert paths.runtime_config_path().read_text(
        encoding="utf-8"
    ) == runtime_config.nothing_to_play_document(
        config.load_strict(), runtime_config.WEB_OFF_REASON
    ), "not the document that plays nothing"
    assert interrupted
    if alone:
        assert "Saving" not in err, err
        return
    assert "Queued" not in _names(), _names()
    assert err.count(_line("1 change")) == 1, err


# -- (d) the control: nothing waiting --------------------------------------------------------


@pytest.mark.parametrize("ui", ["classic", "next"])
def test_a_ctrl_c_with_nothing_waiting_quits_and_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    ui: str,
) -> None:
    """Nothing accepted that isn't saved: one Ctrl-C quits at once, with no countdown and no
    line, and ``run()`` ends as a Ctrl-C ends it (at 55f26e1 too, through ``quit()``)."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui=ui, service=False, settle_seconds=2.0
    )

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        _ctrl_c()

    interrupted = _run_to_the_end(application, scenario(), service=False)
    err = capfd.readouterr().err
    assert "Saving" not in err and "Quitting" not in err, err
    assert interrupted
    assert application._window is None


# -- repeated Ctrl-C: the escalation, one press later -----------------------------------------


def test_a_third_ctrl_c_ends_the_settle_a_second_one_began(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A number nudged, then a first Ctrl-C: the countdown (shortened), then the careful quit
    hands the number on, and its publication hangs on the runtime lane. A second Ctrl-C quits at
    once: nothing is left waiting, and the shutdown's settle waits for that publication, as
    ``quit()``'s did (the bound is the real one). A third ends the settle, and ``run()`` returns
    at once, with KeyboardInterrupt. The number is saved: it was being written. Before (55f26e1)
    the second ended the settle; the escalation is one press later, and as bounded."""
    application, clip, slow, spans = _setup(
        monkeypatch,
        tmp_path,
        ui="classic",
        service=False,
        settle_seconds=app_module.SHUTDOWN_SETTLE_SECONDS,
        reload_seconds=HANG,
    )
    _quick_countdown(monkeypatch)
    marks: dict[str, float] = {}

    def third() -> bool:
        marks["third"] = time.monotonic()
        _ctrl_c()  # the settle's own handler is installed by now
        return GLib.SOURCE_REMOVE

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, _brightness = _rows(application, clip)
        speed.set_value(1.5)
        _LANES.extend(application_lanes(application))
        slow.armed = True
        _ctrl_c()
        yield "the number's publication to hang", lambda: bool(slow.on_gtk)
        assert application._quit_requested and not _counting(application)
        _LANES.extend(application_lanes(application))
        _ctrl_c()
        GLib.timeout_add(300, third)  # runs inside the settle's own iterations

    _LANES.clear()
    ended = 0.0
    with pytest.raises(KeyboardInterrupt):
        try:
            run_application(application, scenario())
        finally:
            ended = time.monotonic()  # before the hung reload's lane is joined
            _join_lanes()
    assert spans, "the second Ctrl-C's shutdown didn't settle"
    assert "third" in marks, "the third Ctrl-C never came"
    assert ended - marks["third"] < 1.0, f"run() returned {ended - marks['third']:.2f}s after it"
    assert _own(clip, Kind.VIDEO).dial("video", "speed") == 1.5
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "handler not restored"


# -- (f, g, h) the terminal: a key, and its mode put back ------------------------------------------


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, int, ModuleType]]:
    """A pty of the test's own as the countdown's standard input: (master, slave, the module)."""
    countdown = importlib.import_module("wall_in_one.ui.quit_countdown")
    master, slave = os.openpty()
    monkeypatch.setattr(countdown, "keys_fd", lambda: slave)
    monkeypatch.setattr(countdown, "keys_usable", lambda fd: fd == slave)
    try:
        yield master, slave, countdown
    finally:
        countdown._restore(slave)  # whatever the test did, before the descriptor goes
        os.close(slave)
        os.close(master)


def _cbreak_now(slave: int) -> bool:
    """Whether the pty is in cbreak mode with echo off, and Ctrl-C still a signal."""
    lflag = termios.tcgetattr(slave)[3]
    return not lflag & termios.ICANON and not lflag & termios.ECHO and bool(lflag & termios.ISIG)


def test_a_key_during_the_countdown_cancels_the_quit_and_a_later_ctrl_c_is_a_first_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    terminal: tuple[int, int, ModuleType],
) -> None:
    """(f) A playlist edit behind a save in progress, a Ctrl-C, then an arrow key (an escape
    sequence: it cancels, as Esc did, and as any key does now): the quit is cancelled, its timer and its watch are gone, the
    terminal is as it was, nothing of the quit is left, and the edit is saved as if no Ctrl-C had
    come. (g) Another edit behind another save, another Ctrl-C: a fresh countdown, which runs to
    its end (shortened) and saves it, and the application quits."""
    master, slave, _countdown = terminal
    before = termios.tcgetattr(slave)
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=2.0
    )
    first = _Lane(application)
    second = _Lane(application)
    marks: dict[str, Any] = {}

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        assert first.create("In flight", held=True)
        yield "the save in progress", first.started.is_set
        assert first.create("Queued")
        _ctrl_c()
        yield "the countdown", lambda: _counting(application)
        counting = application._quit_countdown
        assert counting is not None
        assert counting._timer and counting._watch, "no timer, or no watch of the terminal"
        assert _cbreak_now(slave), termios.tcgetattr(slave)
        assert not _ready(application), "a new edit would be admitted while it is held"
        os.write(master, b"\x1b[A")  # an arrow: an escape sequence, a key like any other
        yield "the cancel", lambda: not _counting(application)
        marks["after the cancel"] = termios.tcgetattr(slave)
        assert not counting._timer and not counting._watch, "its timer or its watch is left"
        assert not application._authoring_held and not application._quit_requested
        assert not application._quit_without_waiting and not application._ctrl_c_quit
        assert signal.getsignal(signal.SIGINT) is application._sigint_handler
        first.release.set()
        yield (
            "the held edit saved",
            lambda: "Queued" in _names() and not application._authoring_queue,
        )
        assert _ready(application), "authoring is still held after the cancel"
        assert application._window is not None, "the cancelled quit quit"
        # (g) a Ctrl-C after the cancel starts a fresh countdown
        _quick_countdown(monkeypatch)
        assert second.create("In flight again", held=True)
        yield "the second save in progress", second.started.is_set
        assert second.create("Queued again")
        _LANES.extend(application_lanes(application))
        second.release_in(0.3)
        _ctrl_c()

    try:
        interrupted = _run_to_the_end(application, scenario(), service=False)
    finally:
        first.close()
        second.close()
    err = capfd.readouterr().err
    assert marks["after the cancel"] == before, "the terminal wasn't put back at the cancel"
    assert termios.tcgetattr(slave) == before, "the terminal wasn't put back at the end"
    assert _names() >= {"In flight", "Queued", "In flight again", "Queued again"}, _names()
    assert not interrupted
    assert err.count(_line("1 change", key=True)) == 2, err
    assert err.count("Quitting cancelled.") == 1, err
    assert err.index("Quitting cancelled.") < err.rindex(_line("1 change", key=True)), err


@pytest.mark.parametrize("ended_by", ["the-countdown", "a-second-ctrl-c"])
def test_the_terminal_is_put_back_however_the_countdown_ends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    terminal: tuple[int, int, ModuleType],
    ended_by: str,
) -> None:
    """(h) In cbreak mode while it counts down; as it was once the countdown has ended, or a
    second Ctrl-C has, and nothing of it is left to put back at the process's exit."""
    _master, slave, countdown = terminal
    before = termios.tcgetattr(slave)
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=2.0
    )
    lane = _Lane(application)
    during: list[bool] = []
    if ended_by == "the-countdown":
        monkeypatch.setattr(app_module, "QUIT_COUNTDOWN_TICK_MS", 200)

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        assert lane.create("In flight", held=True)
        yield "the save in progress", lane.started.is_set
        assert lane.create("Queued")
        _LANES.extend(application_lanes(application))
        lane.release_in(0.3)
        _ctrl_c()
        yield "the countdown", lambda: _counting(application)
        during.append(_cbreak_now(slave))
        if ended_by == "a-second-ctrl-c":
            _ctrl_c()

    try:
        interrupted = _run_to_the_end(application, scenario(), service=False)
    finally:
        lane.close()
    assert during == [True], "not in cbreak mode during the countdown"
    assert termios.tcgetattr(slave) == before, "the terminal wasn't put back"
    assert slave not in countdown._SAVED, "left for the process's exit to put back"
    assert interrupted is (ended_by == "a-second-ctrl-c")
    assert ("Queued" in _names()) is (ended_by == "the-countdown"), _names()


# -- the countdown on its own: the line in place, and the process's exit -------------------------


@pytest.mark.parametrize("changes", [1, 2])
def test_on_a_terminal_the_line_counts_down_in_place(
    monkeypatch: pytest.MonkeyPatch, changes: int
) -> None:
    """Standard error a terminal (a pty), standard input not one: the line is rewritten over
    itself once a step, 5 down to 1, with no key offered in it, then finished with a newline."""
    countdown = importlib.import_module("wall_in_one.ui.quit_countdown")
    master, slave = os.openpty()
    out = io.TextIOWrapper(io.FileIO(slave, "w", closefd=False), encoding="utf-8")
    monkeypatch.setattr("sys.stderr", out)
    monkeypatch.setattr(countdown, "keys_fd", lambda: None)
    ended: list[str] = []
    context = GLib.MainContext.default()
    try:
        running = countdown.Countdown(
            changes,
            5,
            10,
            done=lambda: ended.append("done"),
            cancelled=lambda: ended.append("cancelled"),
        )
        running.start()
        deadline = time.monotonic() + GIVE_UP
        while not ended and time.monotonic() < deadline:
            context.iteration(True)
        out.flush()
        written = _drain(master)
    finally:
        out.close()
        os.close(slave)
        os.close(master)
    assert ended == ["done"]
    said = "1 change" if changes == 1 else f"{changes} changes"
    expected = "".join(
        f"\rSaving {said} before quitting in {left}… press Ctrl-C again to quit immediately "
        "without them."
        for left in range(5, 0, -1)
    )
    assert written == expected + "\r\n", repr(written)  # the pty's own \n is \r\n


def _drain(master: int) -> str:
    """What has been written to the pty so far, without waiting for more."""
    chunks: list[bytes] = []
    os.set_blocking(master, False)
    while True:
        try:
            chunk = os.read(master, 4096)
        except BlockingIOError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def test_the_process_exit_puts_back_a_terminal_left_in_cbreak_mode() -> None:
    """The last backstop: whatever ran, the module's exit handler puts the terminal back."""
    countdown = importlib.import_module("wall_in_one.ui.quit_countdown")
    master, slave = os.openpty()
    try:
        before = termios.tcgetattr(slave)
        assert countdown._cbreak(slave)
        assert _cbreak_now(slave)
        countdown._restore_all()  # what atexit runs
        assert termios.tcgetattr(slave) == before
        assert slave not in countdown._SAVED
    finally:
        countdown._restore(slave)
        os.close(slave)
        os.close(master)


# -- any other key cancels (R14V) ------------------------------------------------------------------


def _run_countdown_on_pty(
    countdown: ModuleType, slave: int, between: Any = None, *, seconds: float = 1.5
) -> list[str]:
    """The real `Countdown` (1 change, 5 steps of 100 ms), its standard input the pty; how it
    ended, as ["done"] or ["cancelled"], after ``between`` ran with the countdown started."""
    ended: list[str] = []
    context = GLib.MainContext.default()
    running = countdown.Countdown(
        1,
        5,
        100,
        done=lambda: ended.append("done"),
        cancelled=lambda: ended.append("cancelled"),
    )
    running.start()
    assert running.keys
    if between is not None:
        between()
    deadline = time.monotonic() + seconds
    while not ended and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)
    running.stop()
    return ended


@pytest.mark.parametrize("pressed", [b"x", b"\x1b[A", b"\r", b"\x04", b"abc"])
def test_r14v_any_other_key_cancels_the_quit(
    terminal: tuple[int, int, ModuleType], pressed: bytes
) -> None:
    """A plain letter, an arrow (an escape sequence), Enter, Ctrl-D (0x04, a byte in cbreak mode)
    and several bytes at once each cancel the countdown, as Esc did. Before (f1c082f) only Esc
    did: the letter and the arrow were ignored and the countdown ran out ("done")."""
    master, slave, countdown = terminal
    ended = _run_countdown_on_pty(countdown, slave, lambda: os.write(master, pressed))
    assert ended == ["cancelled"], (pressed, ended)


def test_r14v_what_was_typed_before_the_countdown_does_not_cancel_it(
    terminal: tuple[int, int, ModuleType],
) -> None:
    """Typeahead on the terminal when the countdown starts is thrown away: the countdown runs
    out. Control: a key pressed after it started does cancel (the test above). At f1c082f this
    passes too (any typeahead was ignored there, as it was not Esc), so it is a control for
    the flush; with Esc typed ahead it would have cancelled there."""
    master, slave, countdown = terminal
    os.write(master, b"typed before the Ctrl-C")
    time.sleep(0.1)  # into the pty's input queue
    ended = _run_countdown_on_pty(countdown, slave)
    assert ended == ["done"], ended
