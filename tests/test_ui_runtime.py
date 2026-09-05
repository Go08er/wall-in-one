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
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from wall_in_one import config, paths, runtime_config, runtime_health  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import (  # noqa: E402
    displays,
    favourites,
    pairing,
    pairings,
    playlists,
    schedules,
    state_file,
)
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import LibraryRefreshResult, RemovalPlan, RemovalResult  # noqa: E402
from wall_in_one.ui.app import (  # noqa: E402
    Application,
    _Commands,
    _RuntimeHealthRequest,
    _RuntimeHealthResult,
)
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
        self.playlist_changes = 0
        self.library_changes = 0
        self.scanning: list[bool] = []

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

    def playlists_changed(self, _session: object) -> None:
        self.playlist_changes += 1

    def show_library(self, _session: object) -> None:
        self.library_changes += 1

    def show_library_scanning(self, scanning: bool) -> None:
        self.scanning.append(scanning)

    def apply_settings(self, _settings: config.Settings) -> None: ...


def _application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Application:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    application = Application()
    # Runtime/authoring tests start after first-run migration disposition; the
    # dedicated migration suite owns the blocked-gate lifecycle itself.
    application._authoring_migration_ready = True
    return application


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


def _observe_removal_worker(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Separate real storage completion from the two-second GTK delivery bound.

    Directory fsync can wait for unrelated filesystem writeback, even in a
    serial test run. Keep every real write/sync, but do not misreport its time
    as time spent delivering an already-completed result on the main loop.
    The held-fsync regression below independently checks responsiveness and
    the publication/quit barriers while this worker is unfinished.
    """
    finished = threading.Event()
    original = RemovalPlan.run

    def observed(plan: RemovalPlan) -> RemovalResult:
        try:
            return original(plan)
        finally:
            finished.set()

    monkeypatch.setattr(RemovalPlan, "run", observed)
    return finished


def _close(application: Application) -> None:
    application._runtime_shutdown = True
    application._shutdown_library_scan_jobs(wait=True)
    # Every test owns its workers, including failure teardown. A late actor
    # completion must not inherit the next test's HOME/XDG and socket mocks.
    authoring_jobs = application._authoring_jobs
    application._shutdown_authoring_jobs()
    if authoring_jobs is not None:
        authoring_jobs.shutdown(wait=True, cancel_futures=True)
    if application._runtime_jobs is not None:
        application._runtime_jobs.shutdown(wait=True, cancel_futures=True)
        application._runtime_jobs = None
    application._stills.shutdown()
    application._session.shutdown()
    application._window = None
    if application._runtime_publication_held:
        application._runtime_publication_held = False
        application.release()
    if application._runtime_library_scan_held:
        application._runtime_library_scan_held = False
        application.release()
    if application._removal_convergence_held:
        application._removal_convergence_held = False
        application.release()
    while application._authoring_lifetime_holds:
        application._authoring_lifetime_holds -= 1
        application.release()


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
    reloads: list[str] = []

    def reload_runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        assert verb == "reload"
        reloads.append(verb)
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", reload_runtime)
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
    worker_started = threading.Event()
    release_worker = threading.Event()

    def hold_runtime_lane() -> Response:
        worker_started.set()
        assert release_worker.wait(2)
        return Response.success()

    try:
        application._runtime_pool().submit(hold_runtime_lane)
        assert worker_started.wait(1)
        real_window = cast(MainWindow, window)
        application._adopt_runtime_status(snapshot, real_window)
        finding = cast(list[dict[str, object]], snapshot["taboo_entries"])[0]
        finding["reason"] = "mutated after queue"
        release_worker.set()
        _spin_until(
            lambda: application.session.pairings.health(pairings.Identity.of(item)).is_borked
        )
        assert (
            application.session.pairings.health(pairings.Identity.of(item)).reason
            == "renderer rejected this wallpaper"
        )

        finding["reason"] = "renderer rejected this wallpaper"
        application._adopt_runtime_status(snapshot, real_window)
        application._adopt_runtime_status(
            {"playlist_id": playlist.id, "playlist": playlist.name, "source": "schedule"},
            real_window,
        )
        _spin_until(lambda: not application._runtime_health_pending)

        health = application.session.pairings.health(pairings.Identity.of(item))
        assert health.is_borked
        assert health.reason == "renderer rejected this wallpaper"
        assert reloads == ["reload"], "polling the same status must not create a reload loop"
        assert window.health_changes == 1
    finally:
        release_worker.set()
        _close(application)


def test_every_global_and_targeted_quick_choice_path_refuses_borked_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    path = tmp_path / "library" / "paper.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image")
    item = MediaItem(path=path, kind=Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library(roots=(path.parent,), items=(item,)))
    application.session.pairings.mark_borked(item, "renderer crashed", "automatic-apply")
    calls: list[tuple[str, ...]] = []

    def send(*arguments: str, **_kwargs: object) -> Response:
        calls.append(arguments)
        return Response.success("unexpected")

    monkeypatch.setattr(client, "send_runtime", send)
    monkeypatch.setattr(client, "send_runtime_on", send)
    try:
        response = application.play_item(item)
        assert not response.ok and "Playback unavailable" in response.message
        assert not application.play_item_async(item)
        assert not application.play_item_on_async(item, "DP-1")

        assert calls == []
        assert application.session.playlists.get("quick-choice") is None
        assert application.session.playlists.get(playlists.display_quick_choice_id("DP-1")) is None
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
        roots=(path.parent,),
        display_mode="independent",
        theme_source_connector="DP-1",
    )
    config.save(independent)
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
        identifier = playlists.display_quick_choice_id("DP-1")
        assert application.play_item_on_async(item, "DP-1")
        _spin_until(
            lambda: (
                application.session.playlists.get(identifier) is not None
                and not application._authoring_active
                and not application._runtime_action_pending
                and len(calls) == 2
            )
        )

        chosen = application.session.playlists.get(identifier)
        assert chosen is not None
        assert [entry.path for entry in chosen.entries] == [path]
        assert calls == [("reload",), ("DP-1", "playlist-use", identifier)]
    finally:
        _close(application)


def test_second_gui_quick_choice_is_refused_before_it_can_overwrite_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    root = tmp_path / "library"
    first = MediaItem(root / "first.png", Kind.STILL, 1, 1)
    second = MediaItem(root / "second.png", Kind.STILL, 1, 1)
    application.session.adopt_library(Library((root,), (first, second)))
    started = threading.Event()
    release = threading.Event()
    original = application.session.playlists.set_singleton
    runtime_calls: list[tuple[str, str | None]] = []

    def held_singleton(
        identifier: str,
        name: str,
        source: Path,
        *,
        entry_id: str | None = None,
    ) -> playlists.Playlist:
        started.set()
        assert release.wait(2)
        return original(identifier, name, source, entry_id=entry_id)

    def send(verb: str, argument: str | None = None) -> Response:
        runtime_calls.append((verb, argument))
        return Response.success()

    monkeypatch.setattr(application.session.playlists, "set_singleton", held_singleton)
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    monkeypatch.setattr(
        application,
        "_execute_gui_runtime_call",
        lambda work: (work(), True),
    )
    monkeypatch.setattr(application, "_send_gui_runtime", send)
    try:
        assert application.play_item_async(first)
        assert started.wait(1)
        assert not application.play_item_async(second)
        release.set()
        _spin_until(lambda: not application._quick_choice_pending and bool(runtime_calls))

        chosen = application.session.playlists.find("quick-choice")
        assert [entry.path for entry in chosen.entries] == [first.path]
        assert runtime_calls == [("playlist-use", "quick-choice")]
    finally:
        release.set()
        _close(application)


def test_display_mode_defaults_are_one_ordered_gui_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    calls: list[tuple[str, str, str | None]] = []

    def send_on(
        connector: str,
        verb: str,
        argument: str | None = None,
        **_kwargs: object,
    ) -> Response:
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

    def send_on(
        _connector: str,
        verb: str,
        _argument: str | None = None,
        **_kwargs: object,
    ) -> Response:
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
    deleted.parent.mkdir()
    deleted.write_bytes(b"image")
    item = MediaItem(deleted, Kind.STILL, 5, 1)
    try:
        application.session.adopt_library(Library((deleted.parent,), (item,)))
        playlist = application.session.playlists.create("Keep clean")
        application.session.playlists.add(playlist.id, deleted)
        application.session.playlists.add(playlist.id, deleted)

        intent = application.prepare_item_removal(item)
        deleted.unlink()
        assert application.forget_item(item, intent=intent) == ()

        assert application.session.playlists.find(playlist.id).entries == ()
    finally:
        _close(application)


def test_item_cleanup_aggregates_store_failures_and_keeps_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    path = tmp_path / "library" / "gone.png"
    path.parent.mkdir()
    path.write_bytes(b"image")
    removed = MediaItem(path, Kind.STILL, 5, 1)
    application.session.adopt_library(Library((path.parent,), (removed,)))
    intent = application.prepare_item_removal(removed)
    path.unlink()

    def fail_favourite(_path: Path) -> bool:
        raise favourites.FavouritesError("local-io", "favourites are read-only")

    def fail_pairing(
        _item: MediaItem,
        *,
        removed_stills: tuple[Path, ...] = (),
    ) -> bool:
        del removed_stills
        raise pairings.PairingError("local-io", "pairings are read-only")

    def fail_playlist(_path: Path) -> bool:
        raise playlists.PlaylistError("local-io", "playlists are read-only")

    monkeypatch.setattr(application.session.favourites, "discard", fail_favourite)
    monkeypatch.setattr(application.session.pairings, "forget_item", fail_pairing)
    monkeypatch.setattr(application.session.playlists, "forget_path", fail_playlist)
    try:
        failures = application.forget_item(removed, intent=intent)

        assert len(failures) == 3
        assert "favourites are read-only" in failures[0]
        assert "pairings are read-only" in failures[1]
        assert "playlists are read-only" in failures[2]
        assert application.session.removal_journal.records

        monkeypatch.setattr(application.session.favourites, "discard", lambda _path: False)
        monkeypatch.setattr(
            application.session.pairings,
            "forget_item",
            lambda _item, *, removed_stills=(): False,
        )
        monkeypatch.setattr(application.session.playlists, "forget_path", lambda _path: False)

        assert application.session.retry_removals() == ()
        assert application.session.removal_journal.records == ()
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


def test_held_compiler_lock_never_blocks_the_glib_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda *_args, **_kwargs: Response.success(),
    )
    heartbeat: list[bool] = []

    def beat() -> bool:
        heartbeat.append(True)
        return GLib.SOURCE_REMOVE

    try:
        with runtime_config.compiler_lock():
            assert application._publish_runtime_async()
            GLib.timeout_add(10, beat)
            _spin_until(lambda: heartbeat)
            assert application._runtime_compile_pending
        _spin_until(lambda: not application._runtime_compile_pending)
    finally:
        _close(application)


def test_control_display_discovery_is_deferred_and_keeps_glib_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    started = threading.Event()
    release = threading.Event()

    def delayed_discovery() -> tuple[()]:
        started.set()
        assert release.wait(2)
        return ()

    monkeypatch.setattr("wall_in_one.ui.app.outputs.discover", delayed_discovery)
    replies: list[Response] = []
    try:
        server.dispatch(
            server.build_verb_table(_Commands(application)),
            Request("displays").encode(),
            replies.append,
        )
        assert replies == [], "the control connection must stay open for the deferred answer"
        assert started.wait(1)
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(10, beat)
        _spin_until(lambda: heartbeat)
        assert replies == []
        release.set()
        _spin_until(lambda: replies)
        assert replies[0].ok
        assert "no screens reported" in replies[0].message
    finally:
        release.set()
        _close(application)


def test_delete_invalidates_an_inflight_compile_before_it_can_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "personal.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    playlist = application.session.playlists.create("Saved", entry_id="saved")
    application.session.playlists.add(playlist.id, item.path)
    settings = replace(
        application.settings,
        roots=(root,),
        active_playlist=playlist.id,
    ).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (item,)))
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    compile_started = threading.Event()
    release_compile = threading.Event()
    removed: list[bool] = []
    reloads: list[str] = []
    refreshes: list[bool] = []
    original_update = runtime_config.update
    storage_finished = _observe_removal_worker(monkeypatch)

    def held_update(
        current: config.Settings,
        session: object,
        path: Path | None = None,
    ) -> bool:
        compile_started.set()
        assert release_compile.wait(2)
        return original_update(current, session, path)  # type: ignore[arg-type]

    def runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        reloads.append(verb)
        return Response.success()

    monkeypatch.setattr(runtime_config, "update", held_update)
    monkeypatch.setattr(client, "send_runtime", runtime)
    monkeypatch.setattr(application, "refresh_library", lambda: refreshes.append(True))
    try:
        assert application._publish_runtime_async()
        assert compile_started.wait(1)
        assert application.remove_item_async(
            item,
            trash=True,
            finish=lambda result: removed.append(result.committed),
        )
        _spin_until(lambda: application._removal_runtime_invalidated)
        assert application._runtime_authoring_request is None
        assert not application._runtime_library_ready.is_set()

        release_compile.set()
        assert storage_finished.wait(30), "real removal storage worker did not finish"
        try:
            _spin_until(lambda: removed == [True] and refreshes == [True])
        except AssertionError as error:
            raise AssertionError(
                f"removal did not converge: removed={removed}, refreshes={refreshes}, "
                f"runtime_calls={reloads}, reports={window.reports}, "
                f"scan_pending={application._runtime_library_scan_pending}, "
                f"actor_active={application._authoring_active}"
            ) from error
        assert "reload" not in reloads
        assert not source.exists()
        assert not application._runtime_library_ready.is_set()
    finally:
        release_compile.set()
        _close(application)


def test_delete_waits_for_an_inflight_stale_reload_before_unlinking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "personal.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    playlist = application.session.playlists.create("Saved", entry_id="saved")
    application.session.playlists.add(playlist.id, item.path)
    settings = replace(
        application.settings,
        roots=(root,),
        active_playlist=playlist.id,
    ).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (item,)))
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    reload_started = threading.Event()
    release_reload = threading.Event()
    removed: list[bool] = []
    refreshes: list[bool] = []
    source_seen_during_reload: list[bool] = []
    storage_finished = _observe_removal_worker(monkeypatch)

    def held_runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        if verb == "status":
            return Response.failure("no status snapshot in this deletion fixture")
        assert verb == "reload"
        source_seen_during_reload.append(source.exists())
        reload_started.set()
        assert release_reload.wait(2)
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", held_runtime)
    monkeypatch.setattr(application, "refresh_library", lambda: refreshes.append(True))
    try:
        assert application._publish_runtime_async()
        assert reload_started.wait(1)
        assert application.remove_item_async(
            item,
            trash=True,
            finish=lambda result: removed.append(result.committed),
        )
        _spin_until(lambda: application._removal_runtime_invalidated)
        # The delete worker waits for the stale runtime acknowledgement.  It
        # may never make a loaded config point at an already-unlinked source.
        assert source.exists()

        release_reload.set()
        assert storage_finished.wait(30), "real removal storage worker did not finish"
        _spin_until(lambda: removed == [True] and refreshes == [True])
        assert source_seen_during_reload == [True]
        assert not source.exists()
    finally:
        release_reload.set()
        _close(application)


def test_removal_fsync_keeps_gtk_responsive_and_publication_and_quit_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "personal.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (item,)))
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    storage_finished = _observe_removal_worker(monkeypatch)
    flush_entered = threading.Event()
    release_flush = threading.Event()
    original_sync = paths.fsync_directory
    main_thread = threading.get_ident()
    removed: list[bool] = []
    quit_calls: list[bool] = []
    refreshes: list[bool] = []
    reloads: list[str] = []

    def held_source_sync(path: Path) -> None:
        # The removal worker deliberately uses its pinned /proc/self/fd
        # directory, not a re-resolved pathname. Compare the inode it owns.
        if path.samefile(root):
            assert threading.get_ident() != main_thread
            flush_entered.set()
            assert release_flush.wait(30), "test did not release the source-directory flush"
        original_sync(path)

    def runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        reloads.append(verb)
        return Response.failure("no runtime in this fixture")

    monkeypatch.setattr(paths, "fsync_directory", held_source_sync)
    monkeypatch.setattr(client, "send_runtime", runtime)
    monkeypatch.setattr(application, "quit", lambda: quit_calls.append(True))
    monkeypatch.setattr(application, "refresh_library", lambda: refreshes.append(True))
    try:
        assert application.remove_item_async(
            item, trash=True, finish=lambda result: removed.append(result.committed)
        )
        _spin_until(lambda: application._removal_runtime_invalidated)
        assert flush_entered.wait(30), "real removal did not reach the source-directory flush"
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(10, beat)
        _spin_until(lambda: heartbeat)
        application.request_quit()
        assert not storage_finished.is_set()
        assert removed == []
        assert refreshes == []
        assert quit_calls == []
        assert application._removal_runtime_invalidated
        assert application._authoring_lifetime_holds > 0
        assert not application._runtime_library_ready.is_set()
        assert "reload" not in reloads

        release_flush.set()
        assert storage_finished.wait(30), "real removal storage worker did not finish"
        _spin_until(lambda: removed == [True] and refreshes == [True])
        assert not source.exists()
        # The fake refresh has not completed the post-delete convergence tail;
        # graceful quit must still retain it after the physical worker ends.
        assert application._removal_convergence_held
        assert quit_calls == []
    finally:
        release_flush.set()
        _close(application)


def test_late_refresh_and_removal_cleanup_do_not_publish_after_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    calls: list[str] = []

    def publish() -> bool:
        calls.append("publish")
        return True

    monkeypatch.setattr(application.session, "refresh", lambda: calls.append("scan"))
    monkeypatch.setattr(application, "_publish_runtime", publish)
    try:
        application._runtime_shutdown = True
        application._library_scan_shutdown = True
        application._removal_runtime_invalidated = True
        # These callbacks can already be queued when shutdown closes the last
        # window. They must not mistake that for explicit headless service mode.
        application._release_removal_lane()
        application.refresh_library()
        assert calls == []
    finally:
        _close(application)


def test_explicit_quit_drains_committed_removal_scan_and_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "personal.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (item,)))
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    scan_started = threading.Event()
    release_scan = threading.Event()
    reconciled: list[bool] = []
    removed: list[bool] = []
    quits: list[bool] = []

    class HeldPlan:
        def cancel(self) -> None: ...

        def scan(self) -> Library:
            scan_started.set()
            assert release_scan.wait(2)
            return Library((root,), ())

        def reconcile(self, library: Library) -> LibraryRefreshResult:
            reconciled.append(True)
            return LibraryRefreshResult(
                library=library,
                replay_failures=(),
                cleanups=(),
                removed_workshop=(),
                workshop_cleanup_failures=(),
                known_workshop=(),
                pending_workshop_cleanup=(),
                repaired_faults=(),
            )

    monkeypatch.setattr(
        application.session,
        "prepare_library_refresh",
        lambda: HeldPlan(),
    )
    monkeypatch.setattr(application, "_queue_wallpaper_query", lambda _generation: None)
    monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
    monkeypatch.setattr(application, "refresh_runtime_status_async", lambda: True)
    monkeypatch.setattr(client, "send_runtime", lambda *_args, **_kwargs: Response.success())
    monkeypatch.setattr(Application, "quit", lambda _self: quits.append(True))
    try:
        assert application.remove_item_async(
            item,
            trash=True,
            finish=lambda result: removed.append(result.committed),
        )
        _spin_until(lambda: removed == [True] and scan_started.is_set(), timeout=5)
        application.request_quit()
        assert quits == []

        release_scan.set()
        _spin_until(lambda: reconciled == [True] and quits == [True], timeout=5)
        assert not application._authoring_active
        assert not application._authoring_queue
        assert application._authoring_lifetime_holds == 0
        assert not application._runtime_publication_held
        assert not application._removal_convergence_held
        assert application.session.library.items == ()
        assert not source.exists()
    finally:
        release_scan.set()
        _close(application)


def test_control_authoring_waits_for_the_store_lock_without_stopping_gtk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    started = threading.Event()
    original_create = application.session.playlists.create

    def observed_create(name: str, entry_id: str | None = None) -> playlists.Playlist:
        started.set()
        return original_create(name, entry_id)

    monkeypatch.setattr(application.session.playlists, "create", observed_create)
    replies: list[Response] = []
    heartbeat: list[bool] = []

    def beat() -> bool:
        heartbeat.append(True)
        return GLib.SOURCE_REMOVE

    try:
        with state_file.mutation_lock(
            playlists.state_path(),
            description="held authoring test",
        ):
            server.dispatch(
                server.build_verb_table(_Commands(application)),
                Request("playlist-new", "Evening").encode(),
                replies.append,
            )
            assert started.wait(1)
            GLib.timeout_add(10, beat)
            _spin_until(lambda: heartbeat)
            assert replies == []
        _spin_until(lambda: replies)
        assert replies[0] == Response.success("made Evening")
        assert application.session.playlists.find("Evening").name == "Evening"
        assert window.playlist_changes == 1
    finally:
        _close(application)


def test_last_window_close_waits_for_committed_authoring_runtime_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    compile_started = threading.Event()
    release_compile = threading.Event()
    replies: list[Response] = []
    reloads: list[str] = []
    original_compile = application._compile_runtime_request
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "personal.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    saved = application.session.playlists.create("Saved", entry_id="saved")
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (item,)))
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)

    def held_compile(request: object) -> bool:
        compile_started.set()
        assert release_compile.wait(2)
        return original_compile(request)  # type: ignore[arg-type]

    def runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        reloads.append(verb)
        return Response.success()

    monkeypatch.setattr(application, "_compile_runtime_request", held_compile)
    monkeypatch.setattr(client, "send_runtime", runtime)
    try:
        outcome = _Commands(application).add_to_playlist(f"{saved.id} {source}")
        assert isinstance(outcome, server.Deferred)
        outcome.start(replies.append)
        _spin_until(compile_started.is_set)
        _spin_until(lambda: replies == [Response.success("personal added to Saved")])
        assert not application._authoring_active
        assert application._authoring_lifetime_holds == 0
        assert application._runtime_publication_held

        application._on_close_request(cast(Gtk.Window, window))
        assert application._window is None
        assert application._runtime_publication_held

        release_compile.set()
        _spin_until(lambda: not application._runtime_publication_held)
        assert reloads == ["reload"]
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert "Saved" in document
        assert str(source) in document
    finally:
        release_compile.set()
        _close(application)


def test_control_authoring_fsync_never_owns_the_gtk_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    started = threading.Event()
    release = threading.Event()
    original_fsync = state_file.fsync_parent
    worker_threads: list[int] = []

    def delayed_fsync(path: Path) -> None:
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        original_fsync(path)

    monkeypatch.setattr(state_file, "fsync_parent", delayed_fsync)
    replies: list[Response] = []
    try:
        server.dispatch(
            server.build_verb_table(_Commands(application)),
            Request("playlist-new", "Evening").encode(),
            replies.append,
        )
        assert started.wait(1)
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(10, beat)
        _spin_until(lambda: heartbeat)
        assert replies == []
        assert worker_threads == [worker_threads[0]]
        assert worker_threads[0] != threading.get_ident()
        release.set()
        _spin_until(lambda: replies)
        assert replies[0].ok
    finally:
        release.set()
        _close(application)


def test_control_mutation_is_rejected_busy_and_never_commits_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    adopted: list[bool] = []

    def held_gui_work() -> bool:
        started.set()
        assert release.wait(2)
        return True

    try:
        assert application.authoring_action_async(held_gui_work, adopted.append)
        assert started.wait(1)
        replies: list[Response] = []
        server.dispatch(
            server.build_verb_table(_Commands(application)),
            Request("playlist-new", "Ghost").encode(),
            replies.append,
        )

        assert replies and replies[0].kind == "authoring-busy"
        release.set()
        _spin_until(lambda: adopted == [True])
        assert application.session.playlists.get("ghost") is None
    finally:
        release.set()
        _close(application)


def test_same_value_settings_request_during_blocked_write_keeps_both_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    calls = 0
    callbacks: list[str] = []
    original_update = config.update

    def held_update(changes: object, path: Path | None = None) -> config.Settings:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(2)
        return original_update(changes, path)  # type: ignore[arg-type]

    monkeypatch.setattr(config, "update", held_update)
    try:
        assert application.update_settings_async(
            opacity=0.5,
            on_success=lambda _settings: callbacks.append("first"),
        )
        assert started.wait(1)
        assert application.update_settings_async(
            opacity=0.5,
            on_success=lambda _settings: callbacks.append("second"),
        )
        release.set()
        _spin_until(lambda: callbacks == ["first", "second"])

        assert calls == 2
        assert application.settings.opacity == 0.5
        assert not application._settings_authoring_pending
        assert not application._settings_authoring_running
    finally:
        release.set()
        _close(application)


def test_explicit_quit_drains_an_already_accepted_settings_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    calls = 0
    callbacks: list[str] = []
    quits: list[bool] = []
    original_update = config.update

    def held_update(changes: object, path: Path | None = None) -> config.Settings:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(2)
        return original_update(changes, path)  # type: ignore[arg-type]

    monkeypatch.setattr(config, "update", held_update)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    monkeypatch.setattr(Application, "quit", lambda _self: quits.append(True))
    try:
        assert application.update_settings_async(
            opacity=0.5,
            on_success=lambda _settings: callbacks.append("first"),
        )
        assert started.wait(1)
        assert application.update_settings_async(
            cycle_enabled=True,
            on_success=lambda _settings: callbacks.append("second"),
        )
        application.request_quit()
        assert quits == []

        release.set()
        _spin_until(lambda: callbacks == ["first", "second"] and quits == [True])
        saved = config.load_strict()
        assert saved.opacity == 0.5
        assert saved.cycle_enabled
        assert calls == 2
        assert not application._settings_authoring_pending
        assert not application._settings_authoring_running
    finally:
        release.set()
        _close(application)


def test_source_settings_wait_for_an_active_runtime_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    old_item = MediaItem(old_root / "old.png", Kind.STILL, 1, 1)
    old_item.path.write_bytes(b"old")
    old_settings = replace(
        application.settings,
        roots=(old_root,),
        scan_workshop=False,
    ).validated()
    config.save(old_settings)
    application._settings = old_settings
    application._settings_requested = old_settings
    application.session.update_settings(old_settings, rescan_library=False)
    application.session.adopt_library(Library((old_root,), (old_item,)))
    application._accepted_library_sources = (old_settings.roots, False)
    compile_started = threading.Event()
    release_compile = threading.Event()
    settings_write_started = threading.Event()
    callbacks: list[config.Settings] = []
    refreshes: list[bool] = []
    reloads: list[str] = []
    real_compile = application._compile_runtime_request
    real_update = config.update

    def held_compile(request: Any) -> bool:
        compile_started.set()
        assert release_compile.wait(2)
        return real_compile(request)

    def observed_update(changes: Any, path: Path | None = None) -> config.Settings:
        settings_write_started.set()
        return real_update(changes, path)

    def runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        reloads.append(verb)
        return Response.success()

    monkeypatch.setattr(application, "_compile_runtime_request", held_compile)
    monkeypatch.setattr(config, "update", observed_update)
    monkeypatch.setattr(application, "refresh_library", lambda: refreshes.append(True))
    monkeypatch.setattr(client, "send_runtime", runtime)
    try:
        assert application._publish_runtime_async()
        assert compile_started.wait(1)
        assert application.update_settings_async(
            roots=(new_root,),
            on_success=callbacks.append,
        )
        _spin_until(lambda: application._authoring_active)

        assert not settings_write_started.is_set()
        assert config.load_strict().roots == (old_root,)

        release_compile.set()
        _spin_until(lambda: len(callbacks) == 1, timeout=5)
        assert settings_write_started.is_set()
        assert callbacks[0].roots == (new_root,)
        assert config.load_strict().roots == (new_root,)
        assert reloads.count("reload") == 1
        assert refreshes == [True]
    finally:
        release_compile.set()
        _close(application)


def test_explicit_quit_waits_for_source_scan_and_runtime_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    root = tmp_path / "new-library"
    root.mkdir()
    source = root / "new.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    scan_started = threading.Event()
    release_scan = threading.Event()
    reload_seen = threading.Event()
    reconciled: list[bool] = []
    callbacks: list[config.Settings] = []
    quits: list[bool] = []

    class HeldPlan:
        def cancel(self) -> None: ...

        def scan(self) -> Library:
            scan_started.set()
            assert release_scan.wait(2)
            return Library((root,), (item,))

        def reconcile(self, library: Library) -> LibraryRefreshResult:
            reconciled.append(True)
            return LibraryRefreshResult(
                library=library,
                replay_failures=(),
                cleanups=(),
                removed_workshop=(),
                workshop_cleanup_failures=(),
                known_workshop=(),
                pending_workshop_cleanup=(),
                repaired_faults=(),
            )

    def runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        if verb == "reload":
            reload_seen.set()
        return Response.success()

    monkeypatch.setattr(application.session, "prepare_library_refresh", lambda: HeldPlan())
    monkeypatch.setattr(application, "_queue_wallpaper_query", lambda _generation: None)
    monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
    monkeypatch.setattr(application, "refresh_runtime_status_async", lambda: True)
    monkeypatch.setattr(client, "send_runtime", runtime)
    monkeypatch.setattr(Application, "quit", lambda _self: quits.append(True))
    try:
        assert application.update_settings_async(
            roots=(root,),
            scan_workshop=False,
            on_success=callbacks.append,
        )
        _spin_until(scan_started.is_set, timeout=5)
        _spin_until(lambda: callbacks and not application._authoring_active)
        assert application._runtime_library_scan_held
        assert not application._runtime_publication_held

        application.request_quit()
        assert quits == []

        release_scan.set()
        _spin_until(lambda: reconciled == [True] and quits == [True], timeout=5)
        assert reload_seen.is_set()
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert str(source) in document
    finally:
        release_scan.set()
        _close(application)


def test_queued_item_authoring_cannot_resurrect_metadata_after_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "personal.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (item,)))
    saved = application.session.playlists.create("Saved", entry_id="saved")
    application.session.playlists.add(saved.id, item.path)
    application.session.favourites.add(item.path)
    application.session.pairings.choose_palette(
        item,
        pairings.PalettePolicy("builtin", "Nord"),
    )
    started = threading.Event()
    release = threading.Event()
    removed: list[bool] = []
    refused: list[str] = []

    def blocker() -> None:
        started.set()
        assert release.wait(2)

    try:
        assert application.authoring_action_async(blocker, lambda _result: None)
        assert started.wait(1)
        assert application.remove_item_async(
            item,
            trash=True,
            finish=lambda result: removed.append(result.committed),
        )

        def favourite_prepare() -> Any:
            current = application.current_item_for_authoring(item)
            store = application.session.favourites
            return lambda: store.add(current.path)

        application.authoring_action_async(
            lambda: False,
            lambda _changed: None,
            prepare=favourite_prepare,
            failure=refused.append,
        )
        application.authoring_action_async(
            lambda: application.session.pairings.choose_palette(
                item, pairings.PalettePolicy("builtin", "Nord")
            ),
            lambda _record: None,
            prepare=lambda: application.prepare_pairing_mutation(
                item,
                lambda store, current: store.choose_palette(
                    current, pairings.PalettePolicy("builtin", "Nord")
                ),
            ),
            failure=refused.append,
        )

        def playlist_prepare() -> Any:
            current = application.current_item_for_authoring(item)
            if application.session.playlists.get(saved.id) is None:
                raise ValueError("playlist was deleted")
            store = application.session.playlists
            return lambda: store.add(saved.id, current.path)

        application.authoring_action_async(
            lambda: application.session.playlists.add(saved.id, item.path),
            lambda _playlist: None,
            prepare=playlist_prepare,
            failure=refused.append,
        )

        release.set()
        _spin_until(lambda: removed == [True] and len(refused) == 3)

        assert not source.exists()
        assert not favourites.Store.open().is_favourite(item.path)
        assert pairings.Store.open().get(pairings.Identity.of(item)) is None
        durable_playlist = playlists.Store.open().get(saved.id)
        assert durable_playlist is not None
        assert all(entry.source != str(item.path) for entry in durable_playlist.entries)
    finally:
        release.set()
        _close(application)


def test_committed_removal_filters_live_library_before_a_failed_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "personal.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (item,), still_inventory=(item,)))
    saved = application.session.playlists.create("Saved", entry_id="saved")
    application.session.playlists.add(saved.id, item.path)
    application.session.favourites.add(item.path)
    application.session.pairings.choose_palette(
        item,
        pairings.PalettePolicy("builtin", "Nord"),
    )
    refreshes: list[bool] = []
    removed: list[bool] = []
    monkeypatch.setattr(application, "refresh_library", lambda: refreshes.append(True))
    monkeypatch.setattr(client, "send_runtime", lambda *_args, **_kwargs: Response.success())

    def resolve(outcome: server.Outcome) -> Response:
        if isinstance(outcome, Response):
            return outcome
        replies: list[Response] = []
        outcome.start(replies.append)
        _spin_until(lambda: bool(replies))
        return replies[0]

    try:
        assert application.remove_item_async(
            item,
            trash=True,
            finish=lambda result: removed.append(result.committed),
        )
        _spin_until(lambda: removed == [True] and refreshes == [True])
        assert application.session.library.find(item.path) is None
        assert all(
            candidate.path != item.path for candidate in application.session.library.still_inventory
        )

        commands = _Commands(application)
        attempts = (
            commands.select_wallpaper(str(item.path)),
            commands.add_favourite(str(item.path)),
            commands.set_palette(f"{item.path} builtin:Nord"),
            commands.add_to_playlist(f"{saved.id} {item.path}"),
        )
        responses = tuple(resolve(attempt) for attempt in attempts)
        assert all(not response.ok for response in responses)
        assert all("not in the library" in response.message for response in responses)

        assert not favourites.Store.open().is_favourite(item.path)
        assert pairings.Store.open().get(pairings.Identity.of(item)) is None
        durable = playlists.Store.open().get(saved.id)
        assert durable is not None
        assert all(entry.source != str(item.path) for entry in durable.entries)
    finally:
        _close(application)


def test_clear_still_resolves_default_off_gtk_and_immediate_read_is_pure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    root = tmp_path / "wallpapers"
    root.mkdir()
    video_path = root / "clip.mp4"
    sibling = root / "clip-still.png"
    chosen = root / "chosen.png"
    video_path.write_bytes(b"video")
    sibling.write_bytes(b"still")
    chosen.write_bytes(b"chosen")
    moving = MediaItem(video_path, Kind.VIDEO, 5, 1, paired_still=chosen)
    sibling_item = MediaItem(sibling, Kind.STILL, 5, 1)
    chosen_item = MediaItem(chosen, Kind.STILL, 6, 1)
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(
        Library(
            (root,),
            (moving, sibling_item, chosen_item),
            still_inventory=(sibling_item, chosen_item),
        )
    )
    application.session.pairings.choose_still(moving, chosen)
    gtk_thread = threading.get_ident()
    probe_threads: list[int] = []
    original_find = pairing.find_still

    def observed_find(
        video: Path,
        roots: object = (),
        *,
        adopted: Path | None = None,
    ) -> Path | None:
        probe_threads.append(threading.get_ident())
        return original_find(video, roots, adopted=adopted)  # type: ignore[arg-type]

    monkeypatch.setattr(pairing, "find_still", observed_find)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    monkeypatch.setattr(application, "refresh_library", lambda: None)
    replies: list[Response] = []
    try:
        outcome = _Commands(application).set_still(f"{video_path} default")
        assert isinstance(outcome, server.Deferred)
        outcome.start(replies.append)
        _spin_until(lambda: bool(replies))
        assert replies[0].ok
        assert probe_threads and all(thread != gtk_thread for thread in probe_threads)
        accepted = application.session.library.find(video_path)
        assert accepted is not None
        assert accepted.paired_still == sibling

        monkeypatch.setattr(
            Path,
            "is_file",
            lambda _path: (_ for _ in ()).throw(AssertionError("pairing read touched disk")),
        )
        shown = _Commands(application).show_pairing(str(video_path))
        assert shown.ok
        assert str(sibling) in shown.message
    finally:
        _close(application)


def test_queued_still_choice_cannot_relink_a_removed_library_still(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    root = tmp_path / "wallpapers"
    root.mkdir()
    video_path = root / "motion.mp4"
    still_path = root / "poster.png"
    video_path.write_bytes(b"video")
    still_path.write_bytes(b"still")
    video = MediaItem(video_path, Kind.VIDEO, 5, 1)
    still = MediaItem(still_path, Kind.STILL, 5, 1)
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (video, still)))
    application.session.pairings.choose_still(video, still.path)
    started = threading.Event()
    release = threading.Event()
    removed: list[bool] = []
    refused: list[str] = []

    def blocker() -> None:
        started.set()
        assert release.wait(2)

    try:
        assert application.authoring_action_async(blocker, lambda _result: None)
        assert started.wait(1)
        assert application.remove_item_async(
            still,
            trash=True,
            finish=lambda result: removed.append(result.committed),
        )
        assert application.authoring_action_async(
            lambda: application.session.pairings.choose_still(video, still.path),
            lambda _record: None,
            prepare=lambda: application.prepare_still_pairing_mutation(
                video,
                still.path,
                lambda store, current, current_still: store.choose_still(current, current_still),
            ),
            failure=refused.append,
        )

        release.set()
        _spin_until(lambda: removed == [True] and len(refused) == 1)

        assert not still_path.exists()
        durable = pairings.Store.open().get(pairings.Identity.of(video))
        assert durable is not None
        assert durable.still is None
    finally:
        release.set()
        _close(application)


def test_unexpected_removal_worker_failure_releases_guard_and_deferred_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "personal.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (item,)))
    original_prepare = application.session.prepare_removal_plan
    original_refresh = application.refresh_library
    started = threading.Event()
    release = threading.Event()
    reports: list[str] = []
    refreshed: list[bool] = []

    def explode() -> None:
        started.set()
        assert release.wait(2)
        raise RuntimeError("unexpected worker failure")

    monkeypatch.setattr(
        application.session,
        "prepare_removal_plan",
        lambda _item, *, trash: SimpleNamespace(run=explode),
    )
    monkeypatch.setattr(application, "window_report", reports.append)
    try:
        assert application.remove_item_async(item, trash=True, finish=lambda _result: None)
        assert started.wait(1)
        application._refresh_after_removal = True
        monkeypatch.setattr(application, "refresh_library", lambda: refreshed.append(True))
        release.set()
        _spin_until(
            lambda: not application._removal_active and bool(reports) and refreshed == [True]
        )
        assert any("unexpected worker failure" in report for report in reports)

        monkeypatch.setattr(application.session, "prepare_removal_plan", original_prepare)
        monkeypatch.setattr(application, "refresh_library", original_refresh)
        removed: list[bool] = []
        assert application.remove_item_async(
            item,
            trash=True,
            finish=lambda result: removed.append(result.committed),
        )
        _spin_until(lambda: removed == [True])
        assert not application._removal_active
        assert not source.exists()
    finally:
        release.set()
        _close(application)


def test_removal_invalidates_a_stale_health_write_queued_behind_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "personal.png"
    source.write_bytes(b"image")
    item = MediaItem(source, Kind.STILL, 5, 1)
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    library = Library((root,), (item,))
    application.session.adopt_library(library)
    started = threading.Event()
    release = threading.Event()
    removed: list[bool] = []
    stale_health_calls: list[_RuntimeHealthRequest] = []

    def blocker() -> None:
        started.set()
        assert release.wait(2)

    def poison(request: _RuntimeHealthRequest) -> _RuntimeHealthResult:
        stale_health_calls.append(request)
        store = pairings.Store.open()
        store.mark_borked(item, "stale crash", "renderer-crash")
        return _RuntimeHealthResult(request, changed=1, accepted=True, store=store)

    monkeypatch.setattr(application, "_ingest_runtime_health", poison)
    with application._runtime_config_lock:
        request = _RuntimeHealthRequest(
            application._runtime_authoring_generation,
            library,
            "{}",
        )
    try:
        assert application.authoring_action_async(blocker, lambda _result: None)
        assert started.wait(1)
        assert application.remove_item_async(
            item,
            trash=True,
            finish=lambda result: removed.append(result.committed),
        )
        application._queue_runtime_health(request)
        release.set()
        _spin_until(lambda: removed == [True] and not application._runtime_health_pending)

        assert stale_health_calls == []
        assert pairings.Store.open().get(pairings.Identity.of(item)) is None
    finally:
        release.set()
        _close(application)


def test_startup_repair_is_fail_closed_and_retries_every_dangling_playlist_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config.save(replace(config.Settings(), active_playlist="deleted"))
    schedules.Store.open().add("deleted", rule_id="dangling-rule")
    displays.Store.open().assign("DP-1", "deleted")
    application = Application()
    original_forget = schedules.Store.forget_playlist

    def fail_schedule(_store: schedules.Store, _playlist: str) -> bool:
        raise schedules.ScheduleError("local-io", "schedule disk is read-only")

    monkeypatch.setattr(schedules.Store, "forget_playlist", fail_schedule)
    try:
        partial = application._repair_dangling_playlist_references()
        response = application._finish_playlist_reference_repair(partial)

        assert not response.ok
        assert response.kind == "authoring-repair-required"
        assert not bool(application.authoring_ready)
        assert config.load_strict().active_playlist == ""
        assert displays.Store.open().all() == ()
        assert schedules.Store.open().rules[0].playlist == "deleted"

        monkeypatch.setattr(schedules.Store, "forget_playlist", original_forget)
        repaired = application._repair_dangling_playlist_references()
        response = application._finish_playlist_reference_repair(repaired)

        assert response.ok
        assert bool(application.authoring_ready)
        assert schedules.Store.open().rules == ()
        assert application.session.schedules.rules == ()
        assert application.session.displays.all() == ()
    finally:
        _close(application)


def test_control_select_keeps_a_delayed_runtime_socket_off_gtk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    item = MediaItem(tmp_path / "aurora.png", Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library((tmp_path,), (item,)))
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    started = threading.Event()
    release = threading.Event()
    runtime_threads: list[int] = []

    def delayed_runtime(
        verb: str,
        argument: str | None = None,
        **_kwargs: object,
    ) -> Response:
        if verb == "status":
            return _status("Quick choice")
        assert (verb, argument) == ("playlist-use", "quick-choice")
        runtime_threads.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return Response.success("runtime selected aurora")

    monkeypatch.setattr(client, "send_runtime", delayed_runtime)
    replies: list[Response] = []
    try:
        server.dispatch(
            server.build_verb_table(_Commands(application)),
            Request("select", str(item.path)).encode(),
            replies.append,
        )
        _spin_until(started.is_set)
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(10, beat)
        _spin_until(lambda: heartbeat)
        assert replies == []
        assert runtime_threads[0] != threading.get_ident()
        release.set()
        _spin_until(lambda: replies)
        assert replies == [Response.success("runtime selected aurora")]
        assert application.session.playlists.find("quick-choice").entries[0].source == str(
            item.path
        )
        assert window.playlist_changes == 1
    finally:
        release.set()
        _close(application)


def test_second_control_select_is_busy_and_never_commits_after_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Select A must not acknowledge or play the singleton later written by B."""
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    first = MediaItem(tmp_path / "first.png", Kind.STILL, size=1, mtime=1)
    second = MediaItem(tmp_path / "second.png", Kind.STILL, size=1, mtime=2)
    application.session.adopt_library(Library((tmp_path,), (first, second)))
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    first_mutated = threading.Event()
    first_runtime_started = threading.Event()
    release_first_runtime = threading.Event()
    mutations: list[Path] = []
    played_sources: list[str] = []
    original_singleton = application.session.playlists.set_singleton

    def observed_singleton(
        identifier: str,
        name: str,
        source: Path,
        *,
        entry_id: str | None = None,
    ) -> playlists.Playlist:
        chosen = original_singleton(identifier, name, source, entry_id=entry_id)
        mutations.append(source)
        if source == first.path:
            first_mutated.set()
        return chosen

    def observed_runtime(
        verb: str,
        argument: str | None = None,
        **_kwargs: object,
    ) -> Response:
        if verb == "status":
            return _status("Quick choice")
        assert (verb, argument) == ("playlist-use", "quick-choice")
        source = application.session.playlists.find("quick-choice").entries[0].source
        played_sources.append(source)
        if source == str(first.path):
            first_runtime_started.set()
            assert release_first_runtime.wait(2)
        return Response.success(f"played {Path(source).name}")

    monkeypatch.setattr(application.session.playlists, "set_singleton", observed_singleton)
    monkeypatch.setattr(client, "send_runtime", observed_runtime)
    replies: list[Response] = []
    try:
        verbs = server.build_verb_table(_Commands(application))
        server.dispatch(verbs, Request("select", str(first.path)).encode(), replies.append)
        assert first_mutated.wait(1)
        # Control clients are single-flight: a second request is refused now,
        # rather than queued to become a ghost commit after its caller leaves.
        server.dispatch(verbs, Request("select", str(second.path)).encode(), replies.append)
        time.sleep(0.05)
        assert mutations == [first.path]
        assert application.session.playlists.find("quick-choice").entries[0].source == str(
            first.path
        )

        _spin_until(first_runtime_started.is_set)
        assert mutations == [first.path]
        assert len(replies) == 1 and replies[0].kind == "authoring-busy"
        release_first_runtime.set()
        _spin_until(lambda: len(replies) == 2)
        assert mutations == [first.path]
        assert played_sources == [str(first.path)]
        assert replies[1] == Response.success("played first.png")
    finally:
        release_first_runtime.set()
        _close(application)


def test_control_playlist_mode_change_is_single_flight_and_never_ghosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    first = application.session.playlists.create("First", entry_id="first")
    second = application.session.playlists.create("Second", entry_id="second")
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[tuple[str, str | None]] = []

    def ordered_runtime(
        verb: str,
        argument: str | None = None,
        **_kwargs: object,
    ) -> Response:
        if verb == "status":
            return _status("Second")
        calls.append((verb, argument))
        if argument == first.id:
            first_started.set()
            assert release_first.wait(2)
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", ordered_runtime)
    replies: list[Response] = []
    try:
        verbs = server.build_verb_table(_Commands(application))
        server.dispatch(verbs, Request("playlist-use", first.id).encode(), replies.append)
        _spin_until(first_started.is_set)
        server.dispatch(verbs, Request("playlist-use", second.id).encode(), replies.append)
        time.sleep(0.05)
        assert calls == [("playlist-use", first.id)]
        release_first.set()
        _spin_until(lambda: len(replies) == 2)
        assert calls == [("playlist-use", first.id)]
        assert replies[0].kind == "authoring-busy"
        assert replies[1] == Response.success("playing First")
    finally:
        release_first.set()
        _close(application)


def test_shutdown_suppresses_a_late_authoring_reply_and_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_create = application.session.playlists.create

    def delayed_create(name: str, entry_id: str | None = None) -> playlists.Playlist:
        started.set()
        assert release.wait(2)
        try:
            return original_create(name, entry_id)
        finally:
            finished.set()

    monkeypatch.setattr(application.session.playlists, "create", delayed_create)
    replies: list[Response] = []
    try:
        outcome = _Commands(application).make_playlist("Too late")
        assert isinstance(outcome, server.Deferred)
        outcome.start(replies.append)
        assert started.wait(1)
        application._shutdown_authoring_jobs()
        release.set()
        assert finished.wait(1)
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(beat)
        _spin_until(lambda: heartbeat)
        assert replies == []
        assert window.playlist_changes == 0
    finally:
        release.set()
        _close(application)


def test_shutdown_suppresses_a_late_deferred_display_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def delayed_discovery() -> tuple[()]:
        started.set()
        assert release.wait(2)
        return ()

    monkeypatch.setattr("wall_in_one.ui.app.outputs.discover", delayed_discovery)
    outcome = _Commands(application).list_displays()
    assert isinstance(outcome, server.Deferred)
    replies: list[Response] = []
    try:
        outcome.start(replies.append)
        assert started.wait(1)
        with application._runtime_config_lock:
            application._runtime_shutdown = True
        application._runtime_cancellation.cancel()
        release.set()
        assert application._runtime_jobs is not None
        application._runtime_jobs.shutdown(wait=True, cancel_futures=True)
        application._runtime_jobs = None
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(beat)
        _spin_until(lambda: heartbeat)
        assert replies == []
    finally:
        release.set()
        _close(application)


def test_large_publication_captures_only_the_frozen_library_and_keeps_gtk_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    items = tuple(
        MediaItem(tmp_path / f"paper-{index:04d}.png", Kind.STILL, size=1, mtime=index)
        for index in range(4_096)
    )
    library = Library((), items)
    application.session.adopt_library(library)
    real_compile = application._compile_runtime_request
    started = threading.Event()
    release = threading.Event()
    requests: list[object] = []

    def delayed(request: Any) -> bool:
        requests.append(request)
        assert request.library is library
        started.set()
        assert release.wait(2)
        return real_compile(request)

    monkeypatch.setattr(application, "_compile_runtime_request", delayed)
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda *_args, **_kwargs: Response.success(),
    )
    try:
        before = time.monotonic()
        assert application._publish_runtime_async()
        assert time.monotonic() - before < 0.05
        assert started.wait(1)
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(10, beat)
        _spin_until(lambda: heartbeat)
        assert application._runtime_compile_pending
        release.set()
        _spin_until(lambda: not application._runtime_compile_pending, timeout=5)
        assert len(requests) == 1
    finally:
        release.set()
        _close(application)


def test_newest_publication_is_compiled_and_reloaded_before_a_queued_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    first = Library(
        (tmp_path,),
        (MediaItem(tmp_path / "first.png", Kind.STILL, size=1, mtime=1),),
    )
    second = Library(
        (tmp_path,),
        (MediaItem(tmp_path / "second.png", Kind.STILL, size=1, mtime=2),),
    )
    application.session.adopt_library(first)
    first_started = threading.Event()
    release_first = threading.Event()
    compiled: list[Library] = []
    socket_calls: list[str] = []

    def compile_request(request: Any) -> bool:
        library = request.library
        assert isinstance(library, Library)
        compiled.append(library)
        if len(compiled) == 1:
            first_started.set()
            assert release_first.wait(2)
            raise runtime_config.RuntimeConfigError("superseded fixture")
        return True

    def runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        socket_calls.append(verb)
        return _status("Only") if verb == "status" else Response.success(verb)

    monkeypatch.setattr(application, "_compile_runtime_request", compile_request)
    monkeypatch.setattr(client, "send_runtime", runtime)
    try:
        assert application._publish_runtime_async()
        assert first_started.wait(1)
        application.session.adopt_library(second)
        assert application._publish_runtime_async()
        assert application.runtime_action_async("pause")
        release_first.set()

        _spin_until(lambda: "pause" in socket_calls)
        _spin_until(lambda: window.busy == [True, False])
        assert compiled == [first, second]
        assert socket_calls[:2] == ["reload", "pause"]
        assert not any("superseded fixture" in report for report in window.reports)
    finally:
        release_first.set()
        _close(application)


def test_action_waits_for_new_root_scan_instead_of_publishing_the_old_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    old = Library((), (MediaItem(tmp_path / "old.png", Kind.STILL, size=1, mtime=1),))
    application.session.adopt_library(old)
    new_root = tmp_path / "new-root"
    new_root.mkdir()
    changed = replace(application.settings, roots=(new_root,), scan_workshop=False)
    config.save(changed)
    application._settings = changed
    application.session.update_settings(changed, rescan_library=False)
    application._begin_runtime_library_scan(7)
    socket_calls: list[str] = []

    def runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        socket_calls.append(verb)
        return _status("Only") if verb == "status" else Response.success(verb)

    monkeypatch.setattr(client, "send_runtime", runtime)
    try:
        # A light gesture during the scan captures old media but new durable
        # Settings. It must be deferred, never installed or reloaded.
        assert application._publish_runtime_async()
        assert application.runtime_action_async("pause")
        heartbeat: list[bool] = []

        def beat() -> bool:
            heartbeat.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(10, beat)
        _spin_until(lambda: heartbeat)
        assert socket_calls == []
        assert window.busy == [True]

        new = Library(
            (new_root,),
            (MediaItem(new_root / "new.png", Kind.STILL, size=1, mtime=2),),
        )
        application.session.adopt_library(new)
        application._adopt_runtime_library_scan(7)
        assert application._publish_runtime_async()
        application._finish_runtime_library_scan(7)

        _spin_until(lambda: "pause" in socket_calls, timeout=5)
        _spin_until(lambda: window.busy == [True, False])
        assert socket_calls[:2] == ["reload", "pause"]
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert str(new_root / "new.png") in document
        assert str(tmp_path / "old.png") not in document
    finally:
        _close(application)


def test_failed_new_source_scan_never_reloads_an_already_compiled_old_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    old_root.mkdir()
    new_root.mkdir()
    old_source = old_root / "old.png"
    old_source.write_bytes(b"old")
    old_item = MediaItem(old_source, Kind.STILL, 3, 1)
    old_settings = replace(
        application.settings,
        roots=(old_root,),
        scan_workshop=False,
    ).validated()
    config.save(old_settings)
    application._settings = old_settings
    application._settings_requested = old_settings
    application.session.update_settings(old_settings, rescan_library=False)
    application.session.adopt_library(Library((old_root,), (old_item,)))
    application._accepted_library_sources = (old_settings.roots, False)
    compiled = threading.Event()
    release_compile = threading.Event()
    socket_calls: list[str] = []
    real_compile = application._compile_runtime_request

    def compiled_then_held(request: Any) -> bool:
        changed = real_compile(request)
        compiled.set()
        assert release_compile.wait(2)
        return changed

    def runtime(verb: str, *_args: object, **_kwargs: object) -> Response:
        socket_calls.append(verb)
        return Response.success()

    monkeypatch.setattr(application, "_compile_runtime_request", compiled_then_held)
    monkeypatch.setattr(client, "send_runtime", runtime)
    try:
        assert application._publish_runtime_async()
        assert compiled.wait(1)

        # This models the exact terminal ordering: A reached runtime.toml, B
        # became durable and began its scan, then A's worker observed the scan
        # barrier before issuing Rust's reload.
        changed = replace(old_settings, roots=(new_root,)).validated()
        config.save(changed)
        application._settings = changed
        application._settings_requested = changed
        application.session.update_settings(changed, rescan_library=False)
        application._begin_runtime_library_scan(17)
        release_compile.set()
        _spin_until(lambda: not application._runtime_compile_pending)
        assert application._runtime_publication_held

        application._finish_runtime_library_scan(17, error="new-root scan failed")
        _spin_until(lambda: not application._runtime_publication_held, timeout=5)

        assert "reload" not in socket_calls
        assert any("new-root scan failed" in report for report in window.reports)
        assert not application._runtime_library_scan_held
    finally:
        release_compile.set()
        _close(application)


def test_shutdown_invalidates_a_blocked_runtime_compiler_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    started = threading.Event()
    release = threading.Event()

    def delayed(_request: object) -> bool:
        started.set()
        assert release.wait(2)
        raise runtime_config.RuntimeConfigError("late compiler failure")

    monkeypatch.setattr(application, "_compile_runtime_request", delayed)
    try:
        assert application._publish_runtime_async()
        assert started.wait(1)
        with application._runtime_config_lock:
            application._runtime_shutdown = True
            application._runtime_authoring_generation += 1
            application._runtime_health_request = None
        application._window = None
        release.set()
        assert application._runtime_jobs is not None
        application._runtime_jobs.shutdown(wait=True, cancel_futures=True)
        application._runtime_jobs = None
        _spin_until(lambda: not application._runtime_compile_pending)
        assert window.reports == []
    finally:
        release.set()
        application._stills.shutdown()
        application._session.shutdown()


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

        assert application.refresh_runtime_status_async()
        _spin_until(lambda: window.delayed == 1)
        assert application.runtime_status is remembered
        assert len(window.statuses) == 1
        assert window.unavailable == 0
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
    path = tmp_path / "evening.png"
    item = MediaItem(path, Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library((tmp_path,), (item,)))
    playlist = application.session.playlists.create("Evening", entry_id="evening")
    application.session.playlists.add(playlist.id, path)
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


def test_missing_socket_never_starts_the_legacy_renderer_from_the_gui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    applied_on: list[int] = []

    def absent(*_arguments: object, **_keywords: object) -> Response:
        raise client.NotRunningError("runtime socket is absent")

    def forbidden_fallback(_action: object) -> Response:
        applied_on.append(threading.get_ident())
        return Response.success("must not run")

    monkeypatch.setattr(client, "send_runtime", absent)
    monkeypatch.setattr(application, "apply", forbidden_fallback)
    try:
        assert application.runtime_action_async("next")
        _spin_until(lambda: window.busy == [True, False])
        assert applied_on == []
        assert window.reports == ["the wallpaper runtime is unavailable"]
    finally:
        _close(application)


def test_synchronous_authoring_command_cannot_start_the_legacy_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authoring socket also lives on GTK and must fail, not render."""
    application = _application(tmp_path, monkeypatch)
    applied: list[object] = []

    def absent(*_arguments: object, **_keywords: object) -> Response:
        raise client.NotRunningError("runtime socket is absent")

    def forbidden(action: object) -> Response:
        applied.append(action)
        return Response.success("must not run")

    monkeypatch.setattr(client, "send_runtime", absent)
    monkeypatch.setattr(application, "apply", forbidden)
    try:
        response = application.runtime_action("next")
        assert not response.ok
        assert response.message == "the wallpaper runtime is unavailable"
        assert applied == []
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
