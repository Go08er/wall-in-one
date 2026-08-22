"""Runtime socket I/O never owns GTK's main thread.

These are GUI tests because importing the real application brings in GTK, but
they present no window and touch no desktop state.  The delayed socket fakes
are deliberately longer than a UI heartbeat: a synchronous regression makes
the heartbeat assertion fail before any wallpaper command can be attempted.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from wall_in_one import paths, runtime_config, runtime_health  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import (  # noqa: E402
    pairings,
    playlists,
)
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class FakeWindow:
    """Only the state delivery surface used by ``Application``."""

    def __init__(self) -> None:
        self.statuses: list[dict[str, object]] = []
        self.unavailable = 0
        self.delayed = 0
        self.protocol_errors: list[str] = []
        self.busy: list[bool] = []
        self.reports: list[str] = []
        self.currents = 0
        self.health_changes = 0

    def show_runtime_status(self, status: dict[str, object]) -> None:
        self.statuses.append(status)

    def show_runtime_unavailable(self) -> None:
        self.unavailable += 1

    def show_runtime_delayed(self) -> None:
        self.delayed += 1

    def show_runtime_protocol_error(self, message: str) -> None:
        self.protocol_errors.append(message)

    def set_runtime_busy(self, busy: bool) -> None:
        self.busy.append(busy)

    def report(self, message: str) -> None:
        self.reports.append(message)

    def show_current(self, _session: object) -> None:
        self.currents += 1

    def pairing_health_changed(self, _session: object) -> None:
        self.health_changes += 1


def _application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Application:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return Application()


def _attach(application: Application, window: FakeWindow) -> None:
    application._window = cast(MainWindow, window)
    application._window_generation += 1


def _spin_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not predicate():
        while context.pending():
            context.iteration(False)
        if time.monotonic() >= deadline:
            raise AssertionError("GLib callback did not arrive before the test deadline")
        time.sleep(0.002)


def _close(application: Application) -> None:
    application._runtime_shutdown = True
    if application._runtime_jobs is not None:
        application._runtime_jobs.shutdown(wait=True, cancel_futures=True)
        application._runtime_jobs = None
    application._stills.shutdown()
    application._session.shutdown()
    application._window = None


def _status(name: str) -> Response:
    identifier = name.casefold().replace(" ", "-")
    return Response.success(
        json.dumps(
            {
                "playlist_id": identifier,
                "playlist": name,
                "source": "schedule",
                "paused": False,
                "cycle_enabled": True,
                "shuffle": False,
                "last_error": "",
                "schedule": {
                    "following": True,
                    "playlist_id": identifier,
                    "playlist": name,
                    "rule_id": None,
                },
            }
        )
    )


def test_status_taboo_is_persisted_once_and_missing_reports_never_clear_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    path = tmp_path / "library" / "paper.png"
    item = MediaItem(path=path, kind=Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library(roots=(path.parent,), items=(item,)))
    playlist = application.session.playlists.create("Evening")
    application.session.playlists.add(playlist.id, path, entry_id="paper-entry")
    publishes: list[bool] = []

    def publish() -> bool:
        publishes.append(True)
        return True

    monkeypatch.setattr(
        application,
        "_publish_runtime_for_context",
        publish,
    )
    assert runtime_config.update(application.settings, application.session)
    generation = runtime_config.read_config_generation()
    snapshot = {
        "config_generation": generation,
        "config_path": str(paths.runtime_config_path().absolute()),
        "runtime_instance": "a" * runtime_health.RUNTIME_INSTANCE_HEX_CHARS,
        "config_epoch": 1,
        "playlist_id": playlist.id,
        "playlist": playlist.name,
        "source": "schedule",
        "taboo_entries": [
            {
                "playlist_id": playlist.id,
                "entry_id": "paper-entry",
                "reason": "renderer rejected this wallpaper",
                "source": "automatic-apply",
                "durable": False,
                "observed_config_epoch": 1,
            }
        ],
        "taboo_entries_omitted": 0,
    }
    try:
        real_window = cast(MainWindow, window)
        application._adopt_runtime_status(snapshot, real_window)
        application._adopt_runtime_status(snapshot, real_window)
        application._adopt_runtime_status(
            {"playlist_id": playlist.id, "playlist": playlist.name, "source": "schedule"},
            real_window,
        )

        health = application.session.pairings.health(pairings.Identity.of(item))
        assert health.is_borked
        assert health.reason == "renderer rejected this wallpaper"
        assert publishes == [True], "polling the same status must not create a reload loop"
        assert window.health_changes == 1
    finally:
        _close(application)


def test_retry_borked_holds_compiler_transaction_through_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    path = tmp_path / "library" / "paper.png"
    item = MediaItem(path=path, kind=Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library(roots=(path.parent,), items=(item,)))
    application.session.pairings.mark_borked(item, "renderer crashed", "automatic-apply")
    entered = False

    class ObservedLock:
        def __enter__(self) -> None:
            nonlocal entered
            entered = True

        def __exit__(
            self,
            _kind: type[BaseException] | None,
            _error: BaseException | None,
            _traceback: object,
        ) -> None:
            nonlocal entered
            entered = False

    def play(_item: MediaItem) -> bool:
        assert entered, "Quick choice publication escaped the clear transaction"
        return True

    monkeypatch.setattr(runtime_config, "compiler_lock", ObservedLock)
    monkeypatch.setattr(application, "play_item_async", play)
    try:
        assert application.retry_borked(item)
        assert not entered
        assert not application.session.pairings.health(pairings.Identity.of(item)).is_borked
    finally:
        _close(application)


def test_display_quick_choice_compiles_reloads_then_targets_only_one_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    path = tmp_path / "library" / "paper.png"
    item = MediaItem(path=path, kind=Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library(roots=(path.parent,), items=(item,)))
    independent = replace(
        application.settings,
        display_mode="independent",
        theme_source_connector="DP-1",
    )
    application._settings = independent
    application.session.update_settings(independent, rescan_library=False)
    calls: list[tuple[str, ...]] = []

    def send_runtime(verb: str, _argument: str | None = None, **_kwargs: object) -> Response:
        calls.append((verb,))
        return Response.success("reloaded")

    def send_on(
        connector: str,
        verb: str,
        argument: str | None = None,
        **_kwargs: object,
    ) -> Response:
        calls.append((connector, verb, argument or ""))
        return Response.success("changed")

    monkeypatch.setattr(client, "send_runtime", send_runtime)
    monkeypatch.setattr(client, "send_runtime_on", send_on)
    try:
        assert application.play_item_on_async(item, "DP-1")
        _spin_until(lambda: not application._runtime_action_pending)

        identifier = playlists.display_quick_choice_id("DP-1")
        chosen = application.session.playlists.get(identifier)
        assert chosen is not None
        assert [entry.path for entry in chosen.entries] == [path]
        assert calls == [("reload",), ("DP-1", "playlist-use", identifier)]
    finally:
        _close(application)


def test_display_mode_defaults_are_one_ordered_gui_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    calls: list[tuple[str, str, str | None]] = []

    def send_on(connector: str, verb: str, argument: str | None = None) -> Response:
        calls.append((connector, verb, argument))
        return Response.success(verb)

    monkeypatch.setattr(client, "send_runtime_on", send_on)
    monkeypatch.setattr(client, "send_runtime", lambda *_args, **_kwargs: _status("Only"))
    try:
        assert application.reset_display_modes_on_async("DP-1")
        _spin_until(lambda: window.busy == [True, False])

        assert calls == [
            ("DP-1", "cycle", "default"),
            ("DP-1", "shuffle", "default"),
        ]
        assert window.reports == []
    finally:
        _close(application)


@pytest.mark.parametrize(
    ("refused_verb", "expected_calls", "message"),
    (
        ("cycle", ("cycle",), "Shuffle was not changed"),
        ("shuffle", ("cycle", "shuffle"), "Cycle returned to its saved default"),
    ),
)
def test_display_mode_default_refusal_stops_honestly_and_refreshes_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refused_verb: str,
    expected_calls: tuple[str, ...],
    message: str,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    calls: list[str] = []

    def send_on(_connector: str, verb: str, _argument: str | None = None) -> Response:
        calls.append(verb)
        return (
            Response.failure(f"{verb} refused") if verb == refused_verb else Response.success(verb)
        )

    monkeypatch.setattr(client, "send_runtime_on", send_on)
    monkeypatch.setattr(client, "send_runtime", lambda *_args, **_kwargs: _status("Only"))
    try:
        assert application.reset_display_modes_on_async("DP-1")
        _spin_until(lambda: window.busy == [True, False])
        _spin_until(lambda: bool(window.statuses))

        assert tuple(calls) == expected_calls
        assert len(window.reports) == 1 and message in window.reports[0]
        # Neither a complete nor partial reset mutates Python settings. The
        # follow-up status snapshot remains the only visible runtime truth.
        assert application.settings.cycle_enabled is False
        assert application.settings.shuffle is False
        assert window.statuses[-1]["playlist"] == "Only"
    finally:
        _close(application)


def test_forgetting_destroyed_media_removes_every_playlist_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    deleted = tmp_path / "wallpapers" / "gone.png"
    try:
        playlist = application.session.playlists.create("Keep clean")
        application.session.playlists.add(playlist.id, deleted)
        application.session.playlists.add(playlist.id, deleted)

        application.forget(deleted)

        assert application.session.playlists.find(playlist.id).entries == ()
    finally:
        _close(application)


def test_delayed_status_keeps_the_glib_heartbeat_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    started = threading.Event()
    release = threading.Event()

    def delayed(verb: str, _argument: str | None = None, **_kwargs: object) -> Response:
        assert verb == "status"
        started.set()
        assert release.wait(2)
        return _status("After")

    monkeypatch.setattr(client, "send_runtime", delayed)
    try:
        assert application.refresh_runtime_status_async()
        assert started.wait(1)
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(10, beat)
        _spin_until(lambda: heartbeat)
        assert window.statuses == [], "the worker should still be waiting"
        release.set()
        _spin_until(lambda: window.statuses)
        assert window.statuses[-1]["playlist"] == "After"
    finally:
        release.set()
        _close(application)


def test_status_ticks_coalesce_and_a_stale_window_never_receives_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    old = FakeWindow()
    _attach(application, old)
    first_started = threading.Event()
    first_release = threading.Event()
    calls: list[int] = []

    def delayed(verb: str, _argument: str | None = None, **_kwargs: object) -> Response:
        assert verb == "status"
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            first_started.set()
            assert first_release.wait(2)
            return _status("Old")
        return _status("New")

    monkeypatch.setattr(client, "send_runtime", delayed)
    try:
        assert application.refresh_runtime_status_async()
        assert first_started.wait(1)
        for _ in range(20):
            assert not application.refresh_runtime_status_async()
        assert calls == [1], "timer ticks must not queue behind a slow status call"

        application._window = None
        application._window_generation += 1
        new = FakeWindow()
        _attach(application, new)
        assert not application.refresh_runtime_status_async()
        first_release.set()

        _spin_until(lambda: bool(new.statuses))
        assert calls == [1, 2]
        assert old.statuses == []
        assert new.statuses[-1]["playlist"] == "New"
    finally:
        first_release.set()
        _close(application)


def test_delayed_runtime_action_is_async_and_controls_are_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    started = threading.Event()
    release = threading.Event()

    def delayed(verb: str, _argument: str | None = None, **_kwargs: object) -> Response:
        if verb == "pause":
            started.set()
            assert release.wait(2)
            return Response.success("paused")
        assert verb == "status"
        return _status("Only")

    monkeypatch.setattr(client, "send_runtime", delayed)
    try:
        assert application.runtime_action_async("pause")
        assert started.wait(1)
        assert window.busy == [True]
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(10, beat)
        _spin_until(lambda: heartbeat)
        assert window.busy == [True]
        release.set()
        _spin_until(lambda: window.busy == [True, False])
        _spin_until(lambda: bool(window.statuses))
        assert window.reports == []
    finally:
        release.set()
        _close(application)


def test_timeout_never_authorises_the_python_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    fallback: list[bool] = []

    calls = 0

    def status_then_timeout(*_arguments: object, **_keywords: object) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _status("Last known")
        raise client.ControlError("timed out after 0.25s")

    monkeypatch.setattr(client, "send_runtime", status_then_timeout)
    try:
        assert application.refresh_runtime_status_async()
        _spin_until(lambda: len(window.statuses) == 1)
        remembered = application.runtime_status
        assert remembered is window.statuses[-1]

        assert application.refresh_runtime_status_async(on_absent=lambda: fallback.append(True))
        _spin_until(lambda: window.delayed == 1)
        assert application.runtime_status is remembered
        assert len(window.statuses) == 1
        assert window.unavailable == 0
        assert fallback == []
    finally:
        _close(application)


@pytest.mark.parametrize(
    "response",
    (
        Response.failure("unsupported status schema"),
        Response.success("not json"),
        Response.success("[]"),
        Response.success("{}"),
    ),
)
def test_invalid_first_status_is_visibly_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: Response,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(client, "send_runtime", lambda *_args, **_kwargs: response)
    try:
        assert application.refresh_runtime_status_async()
        _spin_until(lambda: bool(window.protocol_errors))
        assert application.runtime_status is None
        assert window.statuses == []
        assert window.unavailable == 0
        assert "status" in window.protocol_errors[-1].casefold()
    finally:
        _close(application)


def test_invalid_later_status_preserves_the_last_atomic_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    answers = iter((_status("Last known"), Response.success("not json")))
    monkeypatch.setattr(client, "send_runtime", lambda *_args, **_kwargs: next(answers))
    try:
        assert application.refresh_runtime_status_async()
        _spin_until(lambda: len(window.statuses) == 1)
        remembered = application.runtime_status

        assert application.refresh_runtime_status_async()
        _spin_until(lambda: bool(window.protocol_errors))

        assert application.runtime_status is remembered
        assert len(window.statuses) == 1
        assert window.unavailable == 0
    finally:
        _close(application)


@pytest.mark.parametrize("verb", ["playlist-use", "schedule-follow"])
def test_failed_playlist_mode_change_does_not_mutate_python_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    playlist = application.session.playlists.create("Evening", entry_id="evening")
    if verb == "schedule-follow":
        application.session.use_playlist(playlist.id)
    before = application.session.manual_playlist

    def rejected(request: str, _argument: str | None = None, **_kwargs: object) -> Response:
        if request == "status":
            return _status("Scheduled")
        assert request == verb
        return Response.failure(f"{verb} rejected")

    monkeypatch.setattr(client, "send_runtime", rejected)
    try:
        started = (
            application.activate_playlist_async(playlist.id)
            if verb == "playlist-use"
            else application.resume_schedule_async()
        )
        assert started
        _spin_until(lambda: window.busy == [True, False])
        assert application.session.manual_playlist == before
        assert window.reports == [f"{verb} rejected"]
    finally:
        _close(application)


def test_missing_socket_keeps_the_session_fallback_serialised_on_gtk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    main_thread = threading.get_ident()
    applied_on: list[int] = []

    def absent(*_arguments: object, **_keywords: object) -> Response:
        raise client.NotRunningError("runtime socket is absent")

    def fallback(_action: object) -> Response:
        applied_on.append(threading.get_ident())
        return Response.success("advanced locally")

    monkeypatch.setattr(client, "send_runtime", absent)
    monkeypatch.setattr(application, "apply", fallback)
    try:
        assert application.runtime_action_async("next")
        _spin_until(lambda: window.busy == [True, False])
        assert applied_on == [main_thread]
    finally:
        _close(application)


def test_successful_rust_navigation_does_not_advance_the_python_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.library.model import Kind, Library, MediaItem

    application = _application(tmp_path, monkeypatch)
    first = MediaItem(tmp_path / "first.png", Kind.STILL, size=1, mtime=1)
    second = MediaItem(tmp_path / "second.png", Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library((tmp_path,), (first, second)))
    before = application.session.cursor
    assert before is first
    window = FakeWindow()
    _attach(application, window)

    def runtime(verb: str, _argument: str | None = None, **_kwargs: object) -> Response:
        return _status("Only") if verb == "status" else Response.success(verb)

    monkeypatch.setattr(client, "send_runtime", runtime)
    try:
        assert application.runtime_action_async("next")
        _spin_until(lambda: window.busy == [True, False])
        assert application.session.cursor is before
    finally:
        _close(application)


def test_successful_synchronous_rust_navigation_does_not_advance_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.library.model import Kind, Library, MediaItem

    application = _application(tmp_path, monkeypatch)
    first = MediaItem(tmp_path / "first.png", Kind.STILL, size=1, mtime=1)
    second = MediaItem(tmp_path / "second.png", Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library((tmp_path,), (first, second)))
    before = application.session.cursor
    assert before is first
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda *_args, **_kwargs: Response.success("advanced in Rust"),
    )
    try:
        assert application.runtime_action("next").ok
        assert application.session.cursor is before
    finally:
        _close(application)


def test_action_cannot_overtake_a_trailing_configuration_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    first_reload_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    def delayed(verb: str, _argument: str | None = None, **_kwargs: object) -> Response:
        calls.append(verb)
        if verb == "reload" and calls.count("reload") == 1:
            first_reload_started.set()
            assert release_first.wait(2)
        if verb == "status":
            return _status("Only")
        return Response.success(verb)

    monkeypatch.setattr(client, "send_runtime", delayed)
    try:
        with application._runtime_config_lock:
            application._runtime_document_generation = 1
        application._queue_runtime_reload()
        assert first_reload_started.wait(1)

        # Document B lands after reload A has begun.  The action is already
        # queued behind A, so a completion-callback-only design would run it
        # before B's trailing reload.
        with application._runtime_config_lock:
            application._runtime_document_generation = 2
        application._queue_runtime_reload()
        assert application.runtime_action_async("pause")
        release_first.set()

        _spin_until(lambda: "pause" in calls)
        _spin_until(lambda: window.busy == [True, False])
        assert calls[:3] == ["reload", "reload", "pause"]
    finally:
        release_first.set()
        _close(application)
