"""What a shutdown waits for after it hands the waiting numbers on, and for how long (r13c, G1).

`do_shutdown` hands every renderer number still waiting out its settle time on, then runs the
loop until they settle (review r13b, G1). Review r13c found four things wrong with that wait:

* **L1, its bound.** Under ``--service`` the handed write's publication ran synchronously on
  GTK (``reload``, up to RUNTIME_ACTION_TIMEOUT), inside one iteration, so the deadline was only
  looked at after it returned: with a service that doesn't answer, Ctrl-C hung past the bound.
  Now every publication from `do_shutdown` on runs on the runtime lane, which the shutdown cuts
  off, ``--service`` included. The windowed lane is the control.
* **L2, its scope.** It waited for every tail of the application (`_quit_waits`), even when the
  one number was dropped or saved nothing. Now only the numbers' writes and the publication
  they asked for. The tail here is a synthetic Settings save in flight.
* **N-2, a second Ctrl-C** did nothing: PyGObject's SIGINT fallback ignores every SIGINT after
  the first while ``run()`` is on the stack. Now it ends the wait, and a number still queued
  behind another write is still saved. A real SIGINT is raised in this process (to the handler
  installed when it lands), as the kernel delivers one.
* **N-3, a number nudged while ``ctl quit`` waits** was refused without a word when its timer
  fired. Now it is saved, as one still waiting when the quit ends is.
* **r13d N11, web wallpapers turned off while the shutdown settles:** that publication, on the
  runtime lane, was cut before it installed, and the file on disk still allowed web wallpapers.
  Now the end of the shutdown replaces it, file-only, with the document that plays nothing; it
  compiles nothing and scans nothing (r13e, L1 and L2), and a second Ctrl-C ends its wait for
  the compiler lock too.

Real Application, both interfaces where it matters; the service is the suite's fake runtime,
made to hang on ``reload`` where said. Nothing outside the test's sandbox is touched.

The deadline is looked at between the loop's iterations, so one iteration's own GTK work comes
on top of the bound: the classic pane rebuilds its editor after the save, which takes
milliseconds alone and seconds late in a full run, among the widgets earlier tests left. So
nothing here is judged by a fixed margin over the bound: each settle's slowest iteration is
recorded, and the checks are that the hung reload never ran on GTK, that the settle ended
within one iteration of its bound, and that it never ran to its deadline waiting for a tail
that isn't the number's.
"""

from __future__ import annotations

import fcntl
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib  # noqa: E402

import wall_in_one.ui.app as app_module  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    Step,
    application_lanes,
    run_application,
    sandboxed_runtime,
    settled,
)
from tests.test_ui_renderer_dials import _own, toolkit  # noqa: E402,F401
from tests.test_ui_renderer_settle_quit import _after, _clip, _rows, _scanned  # noqa: E402
from wall_in_one import config, paths, runtime_compatibility, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.library import pairings, scan  # noqa: E402
from wall_in_one.library.model import Kind  # noqa: E402
from wall_in_one.ui import dial_rows  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402


class _Slow:
    """`client.send_runtime` that sleeps ``seconds`` on "reload" once armed, then answers.

    ``on_gtk``: for each reload while armed, whether it was sent from GTK's own thread.
    ``first``: called once, as the first reload while armed begins.
    """

    def __init__(self, inner: Callable[..., Any], seconds: float) -> None:
        self.inner = inner
        self.seconds = seconds
        self.armed = False
        self.on_gtk: list[bool] = []
        self.first: Callable[[], None] | None = None

    def __call__(self, verb: str, *arguments: Any, **keywords: Any) -> Any:
        if verb == "reload" and self.armed:
            self.on_gtk.append(threading.current_thread() is threading.main_thread())
            first, self.first = self.first, None
            if first is not None:
                first()
            time.sleep(self.seconds)
        return self.inner(verb, *arguments, **keywords)


class _Settles(list[float]):
    """How long each settle took; and, in ``slowest``, each one's slowest loop iteration."""

    def __init__(self) -> None:
        super().__init__()
        self.slowest: list[float] = []


#: How long the faked service hangs on ``reload``, and a bound nothing here should reach.
HANG: float = 8.0

#: Every worker pool seen, so that each is joined after the run (a sleeping fake included).
_LANES: list[Any] = []


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    ui: Any,
    service: bool,
    settle_seconds: float,
    reload_seconds: float = 0.0,
    allow_web: bool = False,
    battery: bool = False,
) -> tuple[Application, Path, _Slow, _Settles]:
    """The application, its video, the faked service, and how long each settle took.

    ``battery``: "Stop animations on battery" on, so that every save of the settings checks
    the service (`runtime_compatibility.require_schema`), here a no-op unless a test says.
    """
    fake = sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    if allow_web or battery:
        config.update(
            {
                **({"allow_web_wallpapers": True} if allow_web else {}),
                **({"stop_animations_on_battery": True} if battery else {}),
            }
        )
    monkeypatch.setattr(app_module, "SHUTDOWN_SETTLE_SECONDS", settle_seconds)
    slow = _Slow(fake.send_runtime, reload_seconds)
    monkeypatch.setattr(client, "send_runtime", slow)
    clip = _clip(tmp_path)
    application = Application(service=service, ui=ui)
    spans = _Settles()
    settle = application._settle_before_shutdown

    def timed() -> None:
        iterations: list[float] = []
        iterate = GLib.MainContext.iteration

        def counted(context: GLib.MainContext, may_block: bool) -> bool:
            began = time.monotonic()
            try:
                return iterate(context, may_block)
            finally:
                iterations.append(time.monotonic() - began)

        monkeypatch.setattr(GLib.MainContext, "iteration", counted)
        started = time.monotonic()
        try:
            settle()
        finally:
            spans.append(time.monotonic() - started)
            spans.slowest.append(max(iterations, default=0.0))
            GLib.MainContext.iteration = iterate  # type: ignore[method-assign]
            _LANES.extend(application_lanes(application))  # before do_shutdown drops them

    monkeypatch.setattr(application, "_settle_before_shutdown", timed)
    return application, clip, slow, spans


def _run(application: Application, scenario: Iterator[Step], *, service: bool) -> None:
    def launch() -> bool:
        application.activate()
        return GLib.SOURCE_REMOVE

    if service:
        GLib.idle_add(launch)

    def recorded() -> Iterator[Step]:
        for step in scenario:
            _LANES.extend(application_lanes(application))
            yield step
        _LANES.extend(application_lanes(application))  # after its last action (the quit)

    _LANES.clear()
    try:
        assert run_application(application, recorded()) == 0
    finally:
        _join_lanes()


def _join_lanes() -> None:
    for lane in dict.fromkeys(_LANES):
        lane.shutdown(wait=True)
    _LANES.clear()
    dial_rows._PENDING.clear()  # a timer of a loop that has ended: nothing may reach the next test


# -- L1: the bound holds while the publication blocks ----------------------------------------


@pytest.mark.parametrize("ui", ["classic", "next"])
@pytest.mark.parametrize("mode", ["service", "windowed-control"])
def test_the_settle_is_bounded_while_the_publication_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ui: str, mode: str
) -> None:
    """A reload that hangs, a 1 s bound. Before: ``[service-*]`` waited the whole hang (3.2 s for
    a 3 s one); in a window 1.0 s."""
    service = mode == "service"
    application, clip, slow, spans = _setup(
        monkeypatch, tmp_path, ui=ui, service=service, settle_seconds=1.0, reload_seconds=HANG
    )

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, _brightness = _rows(application, clip)
        speed.set_value(1.5)
        slow.armed = True  # the service hangs from here: each reload takes HANG
        application.quit()  # what SIGINT's fallback calls

    _run(application, scenario(), service=service)
    own = _own(clip, Kind.VIDEO)
    assert own.dial("video", "speed") == 1.5, own  # saved, durably, whatever the bound did
    assert spans, "no settle ran"
    assert not any(slow.on_gtk), f"{ui}/{mode}: the hung reload ran on GTK, inside the settle"
    assert spans[0] <= 1.0 + spans.slowest[0] + 0.5, (
        f"{ui}/{mode}: the settle took {spans[0]:.2f}s, bound 1.0s, its slowest iteration "
        f"{spans.slowest[0]:.2f}s"
    )


def test_after_the_bound_the_service_lane_doesnt_keep_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run()`` returns once the bound has passed, the reload still hanging on its lane."""
    application, clip, slow, spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=True, settle_seconds=1.0, reload_seconds=HANG
    )
    quit_at: list[float] = []

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, _brightness = _rows(application, clip)
        speed.set_value(1.5)
        slow.armed = True
        quit_at.append(time.monotonic())
        application.quit()

    def launch() -> bool:
        application.activate()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(launch)
    _LANES.clear()
    try:
        assert run_application(application, scenario()) == 0
        returned = time.monotonic() - quit_at[0]
    finally:
        _join_lanes()
    assert spans
    assert not any(slow.on_gtk), "the hung reload ran on GTK"
    assert returned < HANG, f"run() returned {returned:.2f}s after quit(): it waited the hang"


# -- L2: only what was handed on ---------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "dropped-plus-unrelated",
        "admitted-plus-unrelated",
        "admitted-only-control",
        "unrelated-only-control",
    ],
)
def test_the_settle_waits_only_for_what_it_handed_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variant: str
) -> None:
    """A long bound and a Settings save that never ends. Before: the first two waited it all."""
    application, clip, _slow, spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=HANG
    )

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, _brightness = _rows(application, clip)
        if variant != "unrelated-only-control":
            speed.set_value(1.5)
        if variant.startswith("dropped"):
            application._removed_item_paths.add(clip)  # its removal committed
        if "unrelated" in variant:
            with application._settings_authoring_lock:
                application._settings_authoring_running = True  # a Settings save in flight
        application.quit()

    _run(application, scenario(), service=False)
    own = _own(clip, Kind.VIDEO)
    if variant == "unrelated-only-control":
        assert not spans, f"nothing was nudged, yet the shutdown waited: {spans}"
        return
    if variant.startswith("dropped"):
        assert own.dial("video", "speed") is None, own
    else:
        assert own.dial("video", "speed") == 1.5, own
        assert spans, "no settle ran"
    if spans:
        # Not to its deadline, and no longer than its own slowest iteration (GTK work) and a
        # moment: what it waits for is the number's write and publication, here quick.
        assert spans[0] < HANG, f"{variant}: waited {spans[0]:.2f}s, to its deadline"
        assert spans[0] <= spans.slowest[0] + 1.0, (
            f"{variant}: waited {spans[0]:.2f}s for another tail (slowest iteration "
            f"{spans.slowest[0]:.2f}s)"
        )


# -- N-2: a second Ctrl-C ----------------------------------------------------------------------


def _interrupt(failures: list[str]) -> None:
    """A real SIGINT to this process, as the kernel delivers one; never to Python's default."""
    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
        failures.append("no SIGINT handler but Python's default was installed")
        return
    signal.raise_signal(signal.SIGINT)


@pytest.mark.parametrize("variant", ["second-sigint", "one-sigint-control"])
def test_a_second_sigint_cuts_the_settle_short(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variant: str
) -> None:
    """Windowed classic. The number's publication hangs on the runtime lane; the bound is the
    real one. Before: the second SIGINT did nothing, and ``run()`` returned only once the hang
    was over (2.8 s after it, for a 3 s hang)."""
    application, clip, slow, _spans = _setup(
        monkeypatch,
        tmp_path,
        ui="classic",
        service=False,
        settle_seconds=app_module.SHUTDOWN_SETTLE_SECONDS,
        reload_seconds=HANG if variant == "second-sigint" else 0.0,
    )
    marks: dict[str, float] = {}
    failures: list[str] = []

    def second() -> bool:
        marks["second"] = time.monotonic()
        _interrupt(failures)
        return GLib.SOURCE_REMOVE

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        handler = signal.getsignal(signal.SIGINT)
        assert getattr(handler, "__module__", "") == "gi._ossighelper", (
            f"PyGObject's SIGINT fallback is not installed here: {handler!r}"
        )
        speed, _brightness = _rows(application, clip)
        speed.set_value(1.5)
        _LANES.extend(application_lanes(application))
        slow.armed = True
        marks["first"] = time.monotonic()
        _interrupt(failures)  # the first Ctrl-C: PyGObject calls application.quit()
        if variant == "second-sigint":
            GLib.timeout_add(300, second)  # runs inside the settle's own iterations

    _LANES.clear()
    ended = 0.0
    with pytest.raises(KeyboardInterrupt):  # what PyGObject raises once run() returns
        try:
            run_application(application, scenario())
        finally:
            ended = time.monotonic()
            _join_lanes()
    assert not failures, failures
    own = _own(clip, Kind.VIDEO)
    assert own.dial("video", "speed") == 1.5, own
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "handler not restored"
    if variant == "second-sigint":
        assert "second" in marks, "the second SIGINT never ran"
        assert ended - marks["second"] < 1.0, (
            f"run() returned {ended - marks['second']:.2f}s after the second SIGINT"
        )


#: What the app last heard from the service, for the end of the shutdown to judge by.
HEARD: dict[str, dict[str, object] | None] = {
    "nothing": None,
    "an-older-service": {
        "status_version": 2,
        "supported_config_schemas": [4, 5, 6, 7],
        "playback_state": "playing",
    },
    "control-reads-eight": {
        "status_version": 2,
        "supported_config_schemas": [4, 5, 6, 7, 8],
        "playback_state": "playing",
    },
}


def _naming(scenes: Sequence[str]) -> str:
    """A document that plays ``scenes`` (named as the compiler names one: its ``scene_id``)."""
    head = 'schema_version = 4\nconfig_generation = "' + "d" * 64 + '"\n[[playlists]]\nid = "p"\n'
    return head + "".join(f'[[playlists.entries]]\nscene_id = "{scene}"\n' for scene in scenes)


@pytest.mark.parametrize("heard", list(HEARD))
def test_a_second_sigint_isnt_held_by_a_scan_of_the_workshop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, heard: str
) -> None:
    """r13e L1 (its p3): the same, but what the app last heard from the service is nothing, or a
    service older than schema 8, and the file on disk at the end names Workshop scenes, none of
    which the scan lists (written under the compiler lock as the end of the shutdown begins, so
    that no publication of the lane's can come between). Before: the end of the shutdown scanned
    the Workshop on GTK's thread to judge that file, which took 2.2 s for 2,500 items, after the
    second SIGINT, with nothing to end it; here it then installed a compile over it. Now it
    judges only whether the file allows web wallpapers, which it doesn't: each scan and each
    install on GTK's thread once the shutdown has begun is recorded, and there must be none."""
    application, clip, slow, _spans = _setup(
        monkeypatch,
        tmp_path,
        ui="classic",
        service=False,
        settle_seconds=app_module.SHUTDOWN_SETTLE_SECONDS,
        reload_seconds=HANG,
    )
    status = HEARD[heard]
    monkeypatch.setattr(Application, "runtime_status", property(lambda _self: status))
    named = _naming([str(2_000_000_000 + index) for index in range(3)])
    workshop_items = scan.workshop_items
    scanned: list[bool] = []

    def recorded(*arguments: Any, **keywords: Any) -> Any:
        if application._runtime_shutdown and (
            threading.current_thread() is threading.main_thread()
        ):
            scanned.append(True)
        return workshop_items(*arguments, **keywords)

    monkeypatch.setattr(scan, "workshop_items", recorded)
    install = runtime_config._install
    installed: list[bool] = []

    def recorded_install(*arguments: Any, **keywords: Any) -> None:
        if application._runtime_shutdown and (
            threading.current_thread() is threading.main_thread()
        ):
            installed.append(True)
        install(*arguments, **keywords)

    monkeypatch.setattr(runtime_config, "_install", recorded_install)
    step = application._web_off_on_disk_at_exit

    def names_the_scenes_first() -> None:
        with runtime_config.compiler_lock():
            paths.runtime_config_path().write_text(named, encoding="utf-8")
        step()

    monkeypatch.setattr(application, "_web_off_on_disk_at_exit", names_the_scenes_first)
    marks: dict[str, float] = {}
    failures: list[str] = []

    def second() -> bool:
        marks["second"] = time.monotonic()
        _interrupt(failures)
        return GLib.SOURCE_REMOVE

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, _brightness = _rows(application, clip)
        speed.set_value(1.5)
        _LANES.extend(application_lanes(application))
        slow.armed = True
        _interrupt(failures)  # the first Ctrl-C: PyGObject calls application.quit()
        GLib.timeout_add(300, second)  # runs inside the settle's own iterations

    _LANES.clear()
    ended = 0.0
    with pytest.raises(KeyboardInterrupt):  # what PyGObject raises once run() returns
        try:
            run_application(application, scenario())
        finally:
            ended = time.monotonic()
            _join_lanes()
    assert not failures, failures
    own = _own(clip, Kind.VIDEO)
    assert own.dial("video", "speed") == 1.5, own
    assert "second" in marks, "the second SIGINT never ran"
    assert not scanned, "the end of the shutdown scanned the Workshop on GTK's thread"
    assert not installed, "the end of the shutdown replaced a file that allows no web wallpaper"
    assert ended - marks["second"] < 1.0, (
        f"run() returned {ended - marks['second']:.2f}s after the second SIGINT"
    )
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "handler not restored"


@pytest.mark.parametrize("ended_by", ["the-bound", "a-second-sigint"])
def test_a_number_still_queued_when_the_settle_ends_is_still_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ended_by: str
) -> None:
    """Two numbers: the first's write takes 2 s, the second waits behind it. Before: the
    bound dropped the second from the queue; a second SIGINT did nothing."""
    by_sigint = ended_by == "a-second-sigint"
    application, clip, _slow, spans = _setup(
        monkeypatch,
        tmp_path,
        ui="classic",
        service=False,
        settle_seconds=app_module.SHUTDOWN_SETTLE_SECONDS if by_sigint else 0.5,
    )
    set_dial = pairings.Store.set_dial
    calls: list[str] = []

    def slow_first(self: pairings.Store, *arguments: Any, **keywords: Any) -> Any:
        if not calls and threading.current_thread() is not threading.main_thread():
            calls.append("slow")
            time.sleep(2.0)
        return set_dial(self, *arguments, **keywords)

    monkeypatch.setattr(pairings.Store, "set_dial", slow_first)
    marks: dict[str, float] = {}
    failures: list[str] = []

    def second() -> bool:
        marks["second"] = time.monotonic()
        _interrupt(failures)
        return GLib.SOURCE_REMOVE

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, brightness = _rows(application, clip)
        speed.set_value(1.5)
        brightness.set_value(20)
        _LANES.extend(application_lanes(application))
        if by_sigint:
            _interrupt(failures)  # the first Ctrl-C
            GLib.timeout_add(300, second)
        else:
            application.quit()

    _LANES.clear()
    ended = 0.0
    if by_sigint:
        with pytest.raises(KeyboardInterrupt):
            try:
                run_application(application, scenario())
            finally:
                ended = time.monotonic()
                _join_lanes()  # the worker the process waits for before it exits
    else:
        try:
            assert run_application(application, scenario()) == 0
        finally:
            _join_lanes()
    assert not failures, failures
    assert calls == ["slow"]
    own = _own(clip, Kind.VIDEO)
    assert own.dial("video", "speed") == 1.5, own
    assert own.dial("video", "brightness") == 20, f"the queued number was dropped: {own}"
    if by_sigint:
        assert ended - marks["second"] < 1.0, "the second SIGINT didn't end the wait"
    else:
        assert spans and spans[0] < 0.5 + 0.8


# -- N-3: a number nudged while ``ctl quit`` waits ---------------------------------------------


@pytest.mark.parametrize("ui", ["classic", "next"])
@pytest.mark.parametrize("timer", ["still-waiting-control", "fired-during-the-wait"])
def test_a_number_nudged_while_ctl_quit_waits_is_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ui: str, timer: str
) -> None:
    """``--service``. ``ctl quit`` while another accepted write holds the application (a
    synthetic lifetime hold), then a nudge; the hold ends before the number's timer fires (the
    number is handed on by `do_shutdown`) or after (the timer's own write meets the quit).
    Before: ``[fired-during-the-wait-*]`` was refused without a word."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui=ui, service=True, settle_seconds=5.0
    )
    reports: list[str] = []
    real_report = application.window_report

    def report(message: str) -> None:
        reports.append(message)
        real_report(message)

    monkeypatch.setattr(application, "window_report", report)

    def release() -> None:
        application._authoring_lifetime_holds -= 1
        application.release()
        application._maybe_finish_requested_quit()

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, _brightness = _rows(application, clip)
        application._authoring_lifetime_holds += 1  # another accepted write, still saving
        application.hold()
        application.request_quit()  # `ctl quit`
        assert application._quit_requested
        speed.set_value(1.5)  # the person nudges while the quit waits
        if timer == "fired-during-the-wait":
            yield "the settle time to pass", _after((dial_rows.SETTLE_MS + 300) / 1000)
        release()

    _run(application, scenario(), service=True)
    own = _own(clip, Kind.VIDEO)
    assert own.dial("video", "speed") == 1.5, f"{ui}/{timer}: {own}, said {reports}"
    assert not [line for line in reports if "renderer settings" in line.lower()], reports


# -- controls: a ctl quit, or a close, during the settle ---------------------------------------


@pytest.mark.parametrize("ui", ["classic", "next"])
@pytest.mark.parametrize("during", ["request-quit", "window-close"])
def test_a_quit_or_close_during_the_settle_is_harmless(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    ui: str,
    during: str,
) -> None:
    service = during == "request-quit"  # `Control.quit` exists in --service mode only
    application, clip, slow, _spans = _setup(
        monkeypatch, tmp_path, ui=ui, service=service, settle_seconds=5.0, reload_seconds=1.5
    )
    fired: list[str] = []

    def meanwhile() -> bool:
        fired.append(during)
        if during == "request-quit":
            application.request_quit()
        else:
            window = application._window
            assert window is not None
            window.close()  # type: ignore[attr-defined]
        return GLib.SOURCE_REMOVE

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, brightness = _rows(application, clip)
        speed.set_value(1.5)
        brightness.set_value(20)
        slow.armed = True
        GLib.timeout_add(300, meanwhile)
        application.quit()

    _run(application, scenario(), service=service)
    err = capfd.readouterr().err
    criticals = [line for line in err.splitlines() if "-CRITICAL" in line]
    own = _own(clip, Kind.VIDEO)
    assert fired == [during], fired
    assert own.dial("video", "speed") == 1.5, own
    assert own.dial("video", "brightness") == 20, own
    assert application._window is None
    if ui == "classic":
        # The next window logs teardown criticals with no dial touched (r13b observation).
        assert not criticals, criticals[:3]


# -- r13d N11: web wallpapers turned off while the shutdown settles ----------------------------
#
# Ordered by events, not by time: under one slow core the lane's compile alone can outlast any
# settle bound worth waiting for. The settle is ended as a second Ctrl-C ends it (`_settle_cut`,
# which PyGObject's handler is wrapped to set), once what the test needs has happened; its bound
# is only a fallback. The lane is held inside the number's reload until the shutdown has begun.

#: How long anything here waits before it gives up: past it the test fails, it doesn't hang.
GIVE_UP: float = 60.0


def _identity() -> tuple[int, int] | None:
    """Which ``runtime.toml`` is on disk: a rename makes another."""
    try:
        found = paths.runtime_config_path().stat()
    except FileNotFoundError:
        return None
    return found.st_ino, found.st_mtime_ns


def _until(condition: Callable[[], bool], failures: list[str], what: str) -> None:
    """Off GTK: wait for ``condition`` (set by GTK's thread), or say that it never came."""
    deadline = time.monotonic() + GIVE_UP
    while not condition():
        if time.monotonic() > deadline:
            failures.append(f"gave up waiting for {what}")
            return
        time.sleep(0.01)


@pytest.mark.parametrize(
    ("mode", "variant"),
    [
        ("service", "turned-off-during-the-settle"),
        ("windowed", "turned-off-during-the-settle"),
        ("service", "control-left-on"),
        ("service", "control-off-throughout"),
        ("windowed", "control-off-throughout"),
    ],
)
def test_web_turned_off_while_the_shutdown_settles_leaves_no_file_that_allows_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, variant: str
) -> None:
    """Ctrl-C with a number waiting; its publication installs, and its reload hangs. While it
    hangs, inside the settle, the person turns web wallpapers off in the window: that save lands
    and is adopted, and its publication waits on the lane behind the hung reload. Then a second
    Ctrl-C ends the settle, and the shutdown cuts that publication before it installs anything.
    Before: ``[*-turned-off-*]`` left the number's document, which allows web wallpapers, on disk
    for the service's poll, while settings.toml said they are off. What replaces it is the
    document that plays nothing, and nothing is compiled on GTK's thread once the shutdown has
    begun (r13e, L1 and L2: the compile at the end was of the Session in memory, and took as
    long as the library made it). The controls change nothing during the settle, and nothing
    more is written."""
    service = mode == "service"
    turning_off = variant.startswith("turned-off")
    application, clip, slow, spans = _setup(
        monkeypatch,
        tmp_path,
        ui="classic",
        service=service,
        settle_seconds=GIVE_UP / 2,  # a fallback: the settle is cut below
        allow_web=variant != "control-off-throughout",
    )
    installed: list[tuple[int, int] | None] = []
    turned: list[bool] = []
    failures: list[str] = []
    compiled_at_the_end: list[bool] = []
    compile_ = runtime_config._compile

    def watched_compile(*arguments: Any, **keywords: Any) -> Any:
        if application._runtime_shutdown and (
            threading.current_thread() is threading.main_thread()
        ):
            compiled_at_the_end.append(True)
        return compile_(*arguments, **keywords)

    monkeypatch.setattr(runtime_config, "_compile", watched_compile)

    def cut() -> bool:
        application._settle_cut = True  # what a second Ctrl-C sets
        return GLib.SOURCE_REMOVE

    def saved(settings: config.Settings) -> None:
        # On GTK, once the save is adopted: its publication is now waiting on the lane.
        turned.append(settings.allow_web_wallpapers is False)
        cut()

    def refused(message: str) -> None:
        failures.append(f"the toggle wasn't saved: {message}")
        cut()

    def turn_off() -> bool:
        accepted = application.update_settings_async(
            allow_web_wallpapers=False, on_success=saved, on_error=refused
        )
        if not accepted:
            refused("not accepted")
        return GLib.SOURCE_REMOVE

    def hung() -> None:
        # On the lane, inside the number's reload: its document is installed by now.
        installed.append(_identity())
        GLib.idle_add(turn_off if turning_off else cut)  # run by the settle's loop
        # Held until the shutdown has begun, so that nothing more is compiled on this lane.
        _until(lambda: application._runtime_shutdown, failures, "the shutdown to begin")

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, _brightness = _rows(application, clip)
        speed.set_value(1.5)
        slow.first = hung
        slow.armed = True
        application.quit()  # what SIGINT's fallback calls

    _run(application, scenario(), service=service)
    assert not failures, failures
    own = _own(clip, Kind.VIDEO)
    assert own.dial("video", "speed") == 1.5, own
    assert spans, "no settle ran"
    assert installed and installed[0] is not None, "the number's publication never installed"
    on_disk = runtime_config.allows_web_wallpapers(paths.runtime_config_path())
    if turning_off:
        assert turned == [True], f"the toggle was never saved: {turned}"
        assert config.load_strict().allow_web_wallpapers is False
        assert not on_disk, (
            "settings.toml says web wallpapers are off, and the file on disk still allows them"
        )
        assert _identity() != installed[0]
        text = paths.runtime_config_path().read_text(encoding="utf-8")
        assert text == runtime_config.nothing_to_play_document(
            config.load_strict(), runtime_config.WEB_OFF_REASON
        ), "not the document that plays nothing"
        assert not compiled_at_the_end, "the end of the shutdown compiled the library on GTK"
        return
    assert config.load_strict().allow_web_wallpapers is (variant == "control-left-on")
    assert on_disk is (variant == "control-left-on")
    assert _identity() == installed[0], "something was written after the publication"
    assert not compiled_at_the_end, "the end of the shutdown compiled the library on GTK"


def test_web_turned_off_as_the_loop_ends_leaves_no_file_that_allows_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--service``, Ctrl-C just after the toggle, no number waiting, so no settle: the save is
    still being written when the loop ends, and gets no publication at all. It lands only once
    the shutdown has dropped its runtime lane, just before its last step. Before: the file on
    disk still allowed web wallpapers once it had landed. Now the end of the shutdown waits for
    that save, briefly, and then replaces the file."""
    application, clip, _slow, spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=True, settle_seconds=2.0, allow_web=True
    )
    update = config.update
    failures: list[str] = []

    def late_save(changes: dict[str, Any], *arguments: Any, **keywords: Any) -> config.Settings:
        if "allow_web_wallpapers" in changes and (
            threading.current_thread() is not threading.main_thread()
        ):
            # Still being written when the loop ends: it lands once the shutdown has begun
            # and dropped the runtime lane, which it does just before its last step.
            _until(
                lambda: application._runtime_shutdown and application._runtime_jobs is None,
                failures,
                "the shutdown to drop its runtime lane",
            )
        return update(changes, *arguments, **keywords)

    monkeypatch.setattr(config, "update", late_save)
    published: list[bool] = []

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        published.append(runtime_config.allows_web_wallpapers(paths.runtime_config_path()))
        assert application.update_settings_async(allow_web_wallpapers=False)
        application.quit()  # what SIGINT's fallback calls

    _run(application, scenario(), service=True)
    assert not failures, failures
    assert published == [True], "the first publication didn't allow web wallpapers"
    assert not spans, "a settle ran, with no number waiting"
    assert config.load_strict().allow_web_wallpapers is False
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path()), (
        "settings.toml says web wallpapers are off, and the file on disk still allows them"
    )


# -- r13d N11: a save of the setting slower than 2 s, within its own bound ---------------------


def _held_save_check(
    monkeypatch: pytest.MonkeyPatch,
    held: Callable[[], None],
) -> None:
    """The save's check of the service ("Stop animations on battery"), as it runs on the
    authoring worker: ``held`` runs in it, standing for a status call that hasn't answered."""

    def check(_schema: int) -> None:
        if threading.current_thread().name.startswith("control-authoring"):
            held()

    monkeypatch.setattr(runtime_compatibility, "require_schema", check)


def _in_the_last_steps_wait() -> bool:
    """Whether the shutdown's own Ctrl-C handler is installed, as it is while it waits."""
    handler = signal.getsignal(signal.SIGINT)
    return "_second_interrupt_ends_the_settle" in getattr(handler, "__qualname__", "")


#: Longer than the 2 s the end of the shutdown waited at 5bd68ef, well inside a save's bound.
SLOW_SAVE: float = 4.0


@pytest.mark.parametrize("slow_in", ["its-check", "its-write"])
def test_a_save_turning_web_off_slower_than_two_seconds_is_still_waited_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, slow_in: str
) -> None:
    """``--service``, Ctrl-C just after the web toggle, no number waiting: the toggle's save is
    still being written when the loop ends, and lands only 4 s after the shutdown has dropped its
    runtime lane, within what such a save can take. ``its-check``: "Stop animations on battery"
    is on, and the save's status call answers late. ``its-write``: the write itself is slow, as
    a batch that also changes the library's sources is while its dry run scans them (review
    r13e's p4: 1.5 s for 2,500 Workshop items). Before (d4a369d): the end of the shutdown waited
    2 s, read the setting still on, and left the file that allows web wallpapers. Both pass at
    040b3aa, which waits for the save as long as it can take: the file is replaced, and only
    after the save has landed."""
    application, clip, _slow, spans = _setup(
        monkeypatch,
        tmp_path,
        ui="classic",
        service=True,
        settle_seconds=2.0,
        allow_web=True,
        battery=slow_in == "its-check",
    )
    failures: list[str] = []
    answered = threading.Event()
    timers: list[threading.Timer] = []
    order: list[str] = []

    def late() -> None:
        _until(
            lambda: application._runtime_shutdown and application._runtime_jobs is None,
            failures,
            "the shutdown to drop its runtime lane",
        )
        timer = threading.Timer(SLOW_SAVE, answered.set)  # the service answers, or the scan ends
        timers.append(timer)
        timer.start()
        if not answered.wait(GIVE_UP):
            failures.append("the save was never let go")

    if slow_in == "its-check":
        _held_save_check(monkeypatch, late)
    update = config.update

    def recorded_save(changes: dict[str, Any], *arguments: Any, **keywords: Any) -> config.Settings:
        turning = "allow_web_wallpapers" in changes and (
            threading.current_thread() is not threading.main_thread()
        )
        if turning and slow_in == "its-write":
            late()
        saved = update(changes, *arguments, **keywords)
        if turning:
            order.append("saved")
        return saved

    monkeypatch.setattr(config, "update", recorded_save)
    install = runtime_config._install

    def recorded_install(*arguments: Any, **keywords: Any) -> None:
        if application._runtime_shutdown and (
            threading.current_thread() is threading.main_thread()
        ):
            order.append("replaced")
        install(*arguments, **keywords)

    monkeypatch.setattr(runtime_config, "_install", recorded_install)
    published: list[bool] = []

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        published.append(runtime_config.allows_web_wallpapers(paths.runtime_config_path()))
        assert application.update_settings_async(allow_web_wallpapers=False)
        application.quit()  # what SIGINT's fallback calls

    try:
        _run(application, scenario(), service=True)
    finally:
        answered.set()
        for timer in timers:
            timer.cancel()
            timer.join()
    assert not failures, failures
    assert published == [True], "the first publication didn't allow web wallpapers"
    assert timers, "the save was never held"
    assert not spans, "a settle ran, with no number waiting"
    assert config.load_strict().allow_web_wallpapers is False
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path()), (
        "settings.toml says web wallpapers are off, and the file on disk still allows them"
    )
    assert order == ["saved", "replaced"], f"not waited for: {order}"


def test_a_quit_with_no_save_being_written_waits_for_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control. A settings save that has landed and settled before the quit: the end of the
    shutdown waits for nothing, however long a save may take. Timed from the authoring lane's
    shutdown to the stills' (the step in between), against a margin far below any wait it has."""
    application, clip, _slow, _spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=False, settle_seconds=2.0
    )
    marks: dict[str, float] = {}
    saved: list[bool] = []

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        assert application.update_settings_async(
            shuffle=True, on_success=lambda _settings: saved.append(True)
        )
        yield "the save to settle", lambda: bool(saved) and settled(application)
        lanes = application._shutdown_authoring_jobs
        stills = application._stills.shutdown

        def lanes_down() -> None:
            lanes()
            marks["lanes"] = time.monotonic()

        def stills_down() -> None:
            marks["stills"] = time.monotonic()
            stills()

        monkeypatch.setattr(application, "_shutdown_authoring_jobs", lanes_down)
        monkeypatch.setattr(application._stills, "shutdown", stills_down)
        application.quit()

    _run(application, scenario(), service=False)
    assert saved == [True]
    assert {"lanes", "stills"} <= marks.keys(), marks
    took = marks["stills"] - marks["lanes"]
    assert took < 1.0, f"the end of the shutdown waited {took:.2f}s with no save being written"


def test_a_second_ctrl_c_ends_the_wait_for_a_save_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same slow save, whose status call doesn't answer while the app runs. A second Ctrl-C
    while the end of the shutdown waits for it ends that wait: ``run()`` returns at once, and the
    save it cut off isn't waited for (the person cut it off). It still lands, as the process's
    exit waits for it, with web wallpapers off: so the file that allows them is replaced all the
    same. Before (r13e, L3's last case): it was left, and the page played on while they were
    off."""
    application, clip, _slow, _spans = _setup(
        monkeypatch,
        tmp_path,
        ui="classic",
        service=True,
        settle_seconds=2.0,
        allow_web=True,
        battery=True,
    )
    failures: list[str] = []
    marks: dict[str, float] = {}
    answered = threading.Event()

    def status_call() -> None:
        _until(
            lambda: _in_the_last_steps_wait() or answered.is_set(),
            failures,
            "the wait for the save",
        )
        if answered.is_set():  # run() is over
            failures.append("the shutdown ended without waiting for the save")
            return
        marks["second"] = time.monotonic()
        _interrupt(failures)  # the second Ctrl-C, while it waits
        if not answered.wait(GIVE_UP):
            failures.append("the status call was never answered")

    _held_save_check(monkeypatch, status_call)

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        assert application.update_settings_async(allow_web_wallpapers=False)
        _LANES.extend(application_lanes(application))  # its worker, joined after the run
        application.quit()  # the first Ctrl-C (SIGINT's fallback calls this)

    def launch() -> bool:
        application.activate()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(launch)
    _LANES.clear()
    ended = 0.0
    with pytest.raises(KeyboardInterrupt):  # what PyGObject raises once run() returns
        try:
            run_application(application, scenario())
        finally:
            ended = time.monotonic()
            answered.set()
            _join_lanes()
    assert not failures, failures
    assert "second" in marks, "the second Ctrl-C was never sent"
    assert ended - marks["second"] < 1.0, (
        f"run() returned {ended - marks['second']:.2f}s after the second Ctrl-C"
    )
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "handler not restored"
    assert config.load_strict().allow_web_wallpapers is False, "the save never landed"
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path()), (
        "settings.toml says web wallpapers are off, and the file on disk still allows them"
    )


@pytest.mark.parametrize("variant", ["turns-web-off", "control-another-setting"])
def test_a_save_still_written_past_its_bound_that_turns_web_off_is_judged_as_landed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variant: str
) -> None:
    """``--service``, web wallpapers on, Ctrl-C just after a toggle, no number waiting: the save
    is still being written when the end of the shutdown stops waiting for it at its bound (made
    short here), and lands only once ``run()`` has returned, as the process's exit waits for it.
    Before (r13e, L3's last case): the step read the setting still on and left the file that
    allows web wallpapers, and the save then turned them off. Now a save that turns them off is
    judged as landed, and the file is replaced. The control: a save of another setting; the file
    is left as it is."""
    application, clip, _slow, spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=True, settle_seconds=2.0, allow_web=True
    )
    monkeypatch.setattr(app_module, "SETTINGS_SAVE_SECONDS", 0.2)
    turning_off = variant == "turns-web-off"
    key = "allow_web_wallpapers" if turning_off else "shuffle"
    failures: list[str] = []
    returned = threading.Event()
    update = config.update

    def held_save(changes: dict[str, Any], *arguments: Any, **keywords: Any) -> config.Settings:
        if (
            key in changes
            and threading.current_thread() is not threading.main_thread()
            and not returned.wait(GIVE_UP)
        ):
            failures.append("run() never returned")
        return update(changes, *arguments, **keywords)

    monkeypatch.setattr(config, "update", held_save)
    published: list[tuple[int, int] | None] = []

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        assert runtime_config.allows_web_wallpapers(paths.runtime_config_path())
        published.append(_identity())
        assert (
            application.update_settings_async(allow_web_wallpapers=False)
            if turning_off
            else application.update_settings_async(shuffle=True)
        )
        _LANES.extend(application_lanes(application))  # its worker, joined after the run
        application.quit()  # what SIGINT's fallback calls

    def launch() -> bool:
        application.activate()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(launch)
    _LANES.clear()
    try:
        assert run_application(application, scenario()) == 0
    finally:
        returned.set()
        _join_lanes()
    assert not failures, failures
    assert not spans, "a settle ran, with no number waiting"
    saved = config.load_strict()
    if not turning_off:
        assert saved.shuffle is True and saved.allow_web_wallpapers is True
        assert _identity() == published[0], "the file was replaced with web wallpapers on"
        return
    assert saved.allow_web_wallpapers is False, "the save never landed"
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path()), (
        "settings.toml says web wallpapers are off, and the file on disk still allows them"
    )


# -- r13e L1: the wait for the compiler lock ends at a second Ctrl-C too -----------------------


def _held_compiler_lock(
    by: str, holding: threading.Event, let_go: threading.Event, marks: dict[str, float]
) -> None:
    """Off GTK: hold the compiler lock as ``by`` does until ``let_go`` (or `GIVE_UP`).

    ``this-process``: the lock as this process's own lane takes it (`runtime_config.compiler_lock`,
    its in-process gate and the file's flock); ``another-process``: the file's flock alone, on an
    open file of its own, as another process holds it.
    """
    if by == "this-process":
        with runtime_config.compiler_lock():
            holding.set()
            let_go.wait(GIVE_UP)
            marks["let go"] = time.monotonic()
        return
    lock_path = runtime_config._compiler_lock_path(paths.runtime_config_path())
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        holding.set()
        let_go.wait(GIVE_UP)
        marks["let go"] = time.monotonic()
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    ("holder", "variant"),
    [
        ("this-process", "second-ctrl-c"),
        ("another-process", "second-ctrl-c"),
        ("this-process", "control-let-go"),
    ],
)
def test_a_second_ctrl_c_ends_the_wait_for_the_compiler_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, holder: str, variant: str
) -> None:
    """``--service``, web wallpapers on and then turned off just before Ctrl-C: the save lands once
    the shutdown has dropped its runtime lane, so it gets no publication, and the end of the
    shutdown finds the file that allows them. Another writer holds the compiler lock meanwhile:
    this process's own lane, or another process. A second Ctrl-C while the end of the shutdown
    waits for that lock ends the wait at once, and ``run()`` returns with the lock still held.
    The wait's own bound is put out of reach, so only the holder or the Ctrl-C can end it.
    Before (r13e, L1): the step's Ctrl-C handler was installed only while it waited for a save,
    so PyGObject's swallowed this one, and the lock was waited for until the holder let go. The
    control: the holder lets go while the step waits, and the file is replaced."""
    application, clip, _slow, spans = _setup(
        monkeypatch, tmp_path, ui="classic", service=True, settle_seconds=2.0, allow_web=True
    )
    monkeypatch.setattr(app_module, "SHUTDOWN_WEB_OFF_LOCK_SECONDS", 2 * GIVE_UP)
    failures: list[str] = []
    marks: dict[str, float] = {}
    holding = threading.Event()
    waiting = threading.Event()
    let_go = threading.Event()
    held: list[threading.Thread] = []
    second = variant == "second-ctrl-c"

    def hold() -> None:
        _held_compiler_lock(holder, holding, let_go, marks)

    def watch() -> None:
        # Once the end of the shutdown waits for the lock: the second Ctrl-C, or the control's
        # letting go.
        if not waiting.wait(GIVE_UP):
            failures.append("the end of the shutdown never waited for the compiler lock")
        elif second:
            marks["second"] = time.monotonic()
            _interrupt(failures)
        else:
            let_go.set()

    update = config.update

    def late_save(changes: dict[str, Any], *arguments: Any, **keywords: Any) -> config.Settings:
        if "allow_web_wallpapers" in changes and (
            threading.current_thread() is not threading.main_thread()
        ):
            _until(
                lambda: application._runtime_shutdown and application._runtime_jobs is None,
                failures,
                "the shutdown to drop its runtime lane",
            )
            for work in (hold, watch):
                thread = threading.Thread(target=work, name=f"test-{work.__name__}")
                held.append(thread)
                thread.start()
            if not holding.wait(GIVE_UP):
                failures.append("the compiler lock was never taken")
        return update(changes, *arguments, **keywords)

    monkeypatch.setattr(config, "update", late_save)
    lock = runtime_config.compiler_lock

    def watched(*arguments: Any, **keywords: Any) -> Any:
        if threading.current_thread() is threading.main_thread() and application._runtime_shutdown:
            waiting.set()
        return lock(*arguments, **keywords)

    monkeypatch.setattr(runtime_config, "compiler_lock", watched)

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        assert runtime_config.allows_web_wallpapers(paths.runtime_config_path())
        assert application.update_settings_async(allow_web_wallpapers=False)
        _LANES.extend(application_lanes(application))  # its worker, joined after the run
        application.quit()  # the first Ctrl-C (SIGINT's fallback calls this)

    ended = 0.0
    try:
        if second:

            def launch() -> bool:
                application.activate()
                return GLib.SOURCE_REMOVE

            GLib.idle_add(launch)
            _LANES.clear()
            with pytest.raises(KeyboardInterrupt):  # what PyGObject raises once run() returns
                try:
                    run_application(application, scenario())
                finally:
                    ended = time.monotonic()
                    let_go.set()
                    _join_lanes()
        else:
            _run(application, scenario(), service=True)
    finally:
        let_go.set()
        for thread in held:
            thread.join()
    assert not failures, failures
    assert not spans, "a settle ran, with no number waiting"
    assert config.load_strict().allow_web_wallpapers is False
    if not second:
        assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path()), (
            "settings.toml says web wallpapers are off, and the file on disk still allows them"
        )
        return
    assert "second" in marks, "the second Ctrl-C was never sent"
    assert ended < marks["let go"], (
        f"the end of the shutdown waited for the compiler lock past a second Ctrl-C, until it "
        f"was let go ({marks['let go'] - marks['second']:.2f}s after it)"
    )
    assert ended - marks["second"] < 1.0, (
        f"run() returned {ended - marks['second']:.2f}s after the second Ctrl-C"
    )
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "handler not restored"
