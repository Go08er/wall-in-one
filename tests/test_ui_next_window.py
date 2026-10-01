"""``--ui=next``: the real application lives its whole life against `NextWindow`.

These drive `Application.run` itself -- startup, the control socket, the
first activation with its migration check and library scan, the two-second
status poll, settings and palette work, control-socket verbs, and the close
that ends the process -- so a window missing any `WindowServices` member, or
one that breaks on a path the classic window survives, fails here.

Isolation: the autouse conftest guard gives every test its own HOME and XDG
directories and refuses network and Noctalia mutation. The Rust runtime is a
scripted fake behind ``client.send_runtime``; Noctalia's reader is replaced so
no ``noctalia`` process is spawned; the GApplication runs non-unique, so no
session bus (absent in the Nix check, privately autolaunched in a dev shell)
decides which process owns the window. Every wait is bounded.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import get_protocol_members

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from tests.test_next_status_line import BATTERY, two_display_status  # noqa: E402
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.theme import noctalia, source  # noqa: E402
from wall_in_one.ui import app as app_module  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.status_model import RuntimeStatusView, StatusChange  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402
from wall_in_one.ui.window_services import WindowServices  # noqa: E402

#: One wait inside a scenario: what is awaited, and the check for it.
Step = tuple[str, Callable[[], bool]]

STEP_SECONDS = 10.0
TOTAL_SECONDS = 90


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class FakeRuntime:
    """wall-in-one-service's socket: a scripted status reply, every verb acknowledged."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: Callable[[], Response] = self._absent
        self._verbs: list[str] = []

    @staticmethod
    def _absent() -> Response:
        raise client.NotRunningError("no runtime in this test")

    @staticmethod
    def _late() -> Response:
        raise client.ControlTimeoutError("status timed out after 0.25s")

    def answer(self, status: dict[str, object] | None, *, late: bool = False) -> None:
        """Reply to the next status probes with ``status``; None means not running."""
        if late:
            reply = self._late
        elif status is None:
            reply = self._absent
        else:
            document = json.dumps(status)

            def reply() -> Response:
                return Response.success(document)

        with self._lock:
            self._status = reply

    def heard(self, verb: str) -> bool:
        with self._lock:
            return verb in self._verbs

    def send_runtime(
        self,
        verb: str,
        argument: str | None = None,
        *,
        timeout: float | None = None,
        cancellation: client.Cancellation | None = None,
    ) -> Response:
        with self._lock:
            self._verbs.append(verb)
            status = self._status
        return status() if verb == "status" else Response.success()


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    """A sandboxed profile with a two-wallpaper library, a fake runtime, no Noctalia."""
    fake = FakeRuntime()
    monkeypatch.setattr(client, "send_runtime", fake.send_runtime)

    def no_shell(*_arguments: object, **_keywords: object) -> str:
        raise noctalia.NoctaliaError("noctalia is not reachable from this test")

    monkeypatch.setattr(noctalia, "_run", no_shell)
    # The control socket's directory, as a login session would provide it.
    paths.runtime_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    library = tmp_path / "wallpapers"
    library.mkdir()
    for name in ("dawn.png", "dusk.jpg"):
        (library / name).write_bytes(b"placeholder image bytes")
    config.update({"roots": (library,)})
    return fake


def run_application(application: Application, scenario: Iterator[Step]) -> int:
    """Run the real GApplication, advancing ``scenario`` as each wait is met.

    The scenario starts after the first activation and performs its actions
    between yields. When it is exhausted the application must exit by
    itself (the scenario's last action closes the window). A wait that does
    not complete within STEP_SECONDS, or a run longer than TOTAL_SECONDS,
    quits the application and fails the test instead of hanging the suite.

    The waits run inside the application's own main loop rather than
    iterating the default context from outside, so they are checked by a
    GLib timeout here instead of `tests.gtk_helpers.spin_until`; the rule is
    the same: every wait has a deadline and fails naming what it waited for.
    """
    failures: list[BaseException] = []
    waiting: list[tuple[str, Callable[[], bool], float]] = []
    started = False
    watchdog_fired = False

    def fail(error: BaseException) -> bool:
        failures.append(error)
        application.quit()
        return GLib.SOURCE_REMOVE

    def tick() -> bool:
        try:
            while True:
                if waiting:
                    label, ready, deadline = waiting[0]
                    if not ready():
                        if time.monotonic() > deadline:
                            raise AssertionError(f"timed out waiting for: {label}")
                        return GLib.SOURCE_CONTINUE
                    waiting.clear()
                try:
                    label, ready = next(scenario)
                except StopIteration:
                    return GLib.SOURCE_REMOVE
                waiting.append((label, ready, time.monotonic() + STEP_SECONDS))
        except BaseException as error:
            return fail(error)

    def activated(_application: Gio.Application) -> None:
        nonlocal started
        if not started:
            started = True
            GLib.timeout_add(10, tick)

    def watchdog() -> bool:
        nonlocal watchdog_fired
        watchdog_fired = True
        return fail(AssertionError(f"the application was still running after {TOTAL_SECONDS}s"))

    # Uniqueness is a D-Bus matter, covered by the run() tests below.
    application.set_flags(application.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    application.connect_after("activate", activated)
    watchdog_source = GLib.timeout_add_seconds(TOTAL_SECONDS, watchdog)
    try:
        exit_status = application.run([])
    finally:
        if not watchdog_fired:
            GLib.source_remove(watchdog_source)
    if failures:
        raise failures[0]
    return exit_status


def application_lanes(application: Application) -> list[ThreadPoolExecutor]:
    """Every worker pool the application has started, before shutdown drops them."""
    pools = (
        application._runtime_jobs,
        application._authoring_jobs,
        application._theme_jobs,
        application._library_scan_jobs,
        application._legacy_migration_jobs,
        application._browse_jobs,
    )
    return [pool for pool in pools if pool is not None]


def settled(application: Application) -> bool:
    """Nothing is in flight: no write, settings batch, command, scan or palette work."""
    return (
        application.authoring_ready
        and not application._authoring_active
        and not application._authoring_queue
        and not application._settings_authoring_running
        and not application._runtime_action_pending
        and application._library_scan_future is None
        and not application._theme_draining
    )


def control(
    requests: ThreadPoolExecutor, verb: str, argument: str | None = None
) -> Future[Response]:
    """Send one ``ctl`` request over the real socket from a client thread."""
    return requests.submit(client.send, Request(verb=verb, argument=argument))


def test_next_ui_survives_the_whole_application_lifecycle(
    runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next", initial_page="playlists")
    changes: list[StatusChange] = []
    application.status_model.subscribe(lambda change, _view: changes.append(change))
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")
    lanes: list[ThreadPoolExecutor] = []
    shown: list[NextWindow] = []
    palettes: list[source.ResolvedPalette] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow), "--ui=next must build the new window"
        assert application.ui == "next"
        shown.append(window)
        assert window.note_text.startswith("Playlists is not in the new interface yet")
        monkeypatch.setattr(window, "show_palette", palettes.append)

        yield (
            "the migration check, authoring repair and first scan",
            lambda: settled(application) and window.library_text == "2 wallpapers in the library",
        )
        yield (
            "the first poll to find no runtime",
            lambda: window.status_text == "Wallpaper service not running",
        )

        # -- status updates through the model -----------------------------
        runtime.answer(two_display_status())
        application.refresh_runtime_status_async()
        yield (
            "a two-display snapshot",
            lambda: window.status_text == "2 displays · playing Evening",
        )
        runtime.answer(None, late=True)
        application.refresh_runtime_status_async()
        yield (
            "a missed status deadline",
            lambda: window.status_text == "2 displays · playing Evening · status delayed",
        )
        runtime.answer(two_display_status("paused", extra=BATTERY))
        application.refresh_runtime_status_async()
        yield (
            "a paused snapshot on battery",
            lambda: (
                window.status_text == "2 displays · paused Evening · Animations stopped on battery"
            ),
        )

        # -- one playback command: busy, then released ----------------------
        assert application.runtime_action_async("next")
        yield (
            "the command to reach the runtime and finish",
            lambda: runtime.heard("next") and not application.status_model.view.busy,
        )
        assert StatusChange.BUSY in changes

        # -- a settings change from the application -------------------------
        saved: list[config.Settings] = []
        assert application.update_settings_async(cycle_interval=900, on_success=saved.append)
        yield (
            "the settings write and its adoption",
            lambda: bool(saved) and window.settings.cycle_interval == 900,
        )
        assert config.load().cycle_interval == 900

        # -- palette resolution and the theme hand-off --------------------
        resolved: list[source.ResolvedPalette | None] = []
        application.reload_palette(lambda palette, _error: resolved.append(palette))
        yield "the palette worker", lambda: bool(resolved)
        assert resolved[0] is not None and palettes, "the window was told about the palette"

        # -- ctl over the real control socket ------------------------------
        opened = control(requests, "open", "settings")
        yield "ctl open", opened.done
        answer = opened.result()
        assert not answer.ok and answer.kind == "not-in-new-ui"
        assert "settings page is not available in the new UI yet" in answer.message
        assert window.note_text.startswith("Settings is not in the new interface yet")

        status = control(requests, "status")
        yield "ctl status", status.done
        assert status.result().kind == "runtime-not-running", "the GUI never poses as the runtime"

        # The control socket refuses a write while another is being saved
        # ("authoring-busy"), as it should; a script waits, and so does this.
        yield "authoring to settle before ctl cycle", lambda: settled(application)
        cycle = control(requests, "cycle", "off")
        yield "ctl cycle off", cycle.done
        assert cycle.result() == Response.success("cycle off")
        yield "the control settings adoption", lambda: window.settings.cycle_enabled is False

        yield "authoring to settle before ctl playlist-new", lambda: settled(application)
        made = control(requests, "playlist-new", "Morning")
        yield "ctl playlist-new", made.done
        assert made.result() == Response.success("made Morning")

        yield "authoring to settle before ctl reload-palette", lambda: settled(application)
        repainted = control(requests, "reload-palette")
        yield "ctl reload-palette", repainted.done
        assert repainted.result().ok, repainted.result().message

        # -- the runtime goes away again, then the user closes the window ----
        runtime.answer(None)
        application.refresh_runtime_status_async()
        yield (
            "the runtime to be reported gone",
            lambda: window.status_text == "Wallpaper service not running",
        )
        yield "every tail to settle", lambda: settled(application)
        lanes.extend(application_lanes(application))
        window.close()

    try:
        assert run_application(application, scenario()) == 0
    finally:
        requests.shutdown(wait=True)
        for lane in lanes:
            lane.shutdown(wait=True)

    assert application._window is None
    assert shown, "the scenario ran against the window"
    assert len(application.status_model._subscribers) == 2, "the window unsubscribed on close"
    assert StatusChange.STATUS in changes and StatusChange.UNAVAILABLE in changes


def test_the_default_application_still_builds_the_classic_window(runtime: FakeRuntime) -> None:
    application = Application()
    lanes: list[ThreadPoolExecutor] = []
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert application.ui == "classic"
        assert isinstance(window, MainWindow), "no --ui means today's window"
        yield "the first scan", lambda: settled(application) and window._playable == 2

        runtime.answer(two_display_status())
        application.refresh_runtime_status_async()
        yield (
            "the classic header to show the snapshot",
            lambda: "playing Evening (schedule)" in window._subtitle.get_subtitle(),
        )

        opened = control(requests, "open", "settings")
        yield "ctl open", opened.done
        assert opened.result() == Response.success("opened settings")
        assert window._stack.get_visible_child_name() == "settings"

        yield "every tail to settle", lambda: settled(application)
        lanes.extend(application_lanes(application))
        window.close()

    try:
        assert run_application(application, scenario()) == 0
    finally:
        requests.shutdown(wait=True)
        for lane in lanes:
            lane.shutdown(wait=True)
    assert application._window is None


@pytest.mark.parametrize("window_class", [MainWindow, NextWindow])
def test_both_windows_define_every_window_service(
    window_class: type[MainWindow] | type[NextWindow],
) -> None:
    """The runtime half of the mypy proof: same names, same parameters."""
    members = get_protocol_members(WindowServices)
    assert members == {
        "present",
        "report",
        "show_page",
        "apply_settings",
        "show_palette",
        "open_palette_browser",
        "show_library",
        "show_library_scanning",
        "show_current",
        "playlists_changed",
        "pairing_health_changed",
        "show_runtime_status",
        "show_runtime_unavailable",
        "show_runtime_delayed",
        "show_runtime_protocol_error",
        "set_runtime_busy",
    }
    for name in members - {"present"}:
        implemented = getattr(window_class, name)
        declared = getattr(WindowServices, name)
        assert list(inspect.signature(implemented).parameters) == list(
            inspect.signature(declared).parameters
        ), f"{window_class.__name__}.{name}"
    assert callable(window_class.present)


def test_next_window_renders_from_the_model_not_the_forwarded_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forwarded show_* calls are no-ops; the subscription does the work."""
    application = Application(ui="next")
    window = NextWindow(application, application.settings)
    views: list[RuntimeStatusView] = []
    subscribers = application.status_model._subscribers
    window.present()
    try:
        window.show_runtime_status(two_display_status())
        assert window.status_text == "Checking the wallpaper service…"
        application.status_model.subscribe(lambda _change, view: views.append(view))
        application.status_model.adopt(two_display_status())
        assert window.status_text == "2 displays · playing Evening"
        application.status_model.set_busy(True)
        assert window.status_text.endswith("sending a playback command…")
        assert window.show_page("media") is False
        assert window.note_text.startswith("Library is not in the new interface yet")
    finally:
        window.destroy()
        application._stills.shutdown()
        application.session.shutdown()
    application.status_model.set_busy(False)
    assert len(views) == 3
    assert len(subscribers) == 2, "the destroyed window no longer listens; the forwarder does"


def _remote(events: list[object]) -> type:
    """A verified running instance of this package, as registration reports it."""

    class Remote:
        _stills = SimpleNamespace(shutdown=lambda: None)
        _session = SimpleNamespace(shutdown=lambda: None)

        def __init__(self, **keywords: object) -> None:
            events.append(("constructed", keywords.get("ui")))

        def register(self, _cancellable: object) -> None:
            pass

        def get_is_remote(self) -> bool:
            return True

        def has_action(self, _name: str) -> bool:
            return True

        def get_action_state(self, _name: str) -> GLib.Variant:
            return GLib.Variant("s", app_module.PACKAGE_SOURCE)

        def activate_action(self, name: str, parameters: GLib.Variant) -> None:
            events.append((name, parameters.unpack()))

        def get_dbus_connection(self) -> None:
            return None

    return Remote


def test_a_second_launch_with_another_ui_presents_the_running_window_unchanged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[object] = []
    monkeypatch.setattr(app_module, "Application", _remote(events))

    assert app_module.run(initial_page="media", ui="next") == 0

    presented = (app_module.PRESENT_PACKAGE_ACTION, (app_module.PACKAGE_SOURCE, "media"))
    assert events == [("constructed", "next"), presented], "the (ss) activation is unchanged"
    error = capsys.readouterr().err
    assert "already running, so --ui=next was not applied" in error


def test_a_second_default_launch_says_nothing_new(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[object] = []
    monkeypatch.setattr(app_module, "Application", _remote(events))

    assert app_module.run(initial_page="media") == 0

    assert events[0] == ("constructed", "classic")
    assert capsys.readouterr().err == ""
