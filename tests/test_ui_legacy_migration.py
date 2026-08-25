"""The predecessor decision is the first GUI question and never overwrites silently."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from wall_in_one import config, legacy_migration  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.ui.app import (  # noqa: E402
    Application,
    _Commands,
    _PlaylistReferenceRepairResult,
)
from wall_in_one.ui.window import MainWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _application() -> tuple[Application, Gtk.Window]:
    application = Application()
    parent = Gtk.Window()
    application._window = cast(MainWindow, parent)
    return application, parent


def _close(application: Application, parent: Gtk.Window) -> None:
    application._shutdown_legacy_migration_jobs(wait=True)
    application._shutdown_authoring_jobs()
    application._window = None
    parent.destroy()
    application._shutdown_theme_jobs(wait=True)
    application._stills.shutdown()
    application.session.shutdown()


def _capture_dialog(monkeypatch: pytest.MonkeyPatch) -> list[Adw.AlertDialog]:
    shown: list[Adw.AlertDialog] = []
    monkeypatch.setattr(
        Adw.AlertDialog,
        "present",
        lambda dialog, _parent: shown.append(dialog),
    )
    return shown


def _spin_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not predicate():
        while context.pending():
            context.iteration(False)
        if time.monotonic() >= deadline:
            raise AssertionError("GLib callback did not arrive before the test deadline")
        time.sleep(0.002)


def test_import_safely_is_offered_first_and_reopens_every_imported_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, parent = _application()
    source = tmp_path / "legacy"
    found = legacy_migration.Probe(
        "ready",
        "legacy authoring is ready",
        source,
        schema=5,
        playlists=3,
        outputs=2,
    )
    imported = legacy_migration.Outcome(True, "imported", tmp_path / "report.md")
    shown = _capture_dialog(monkeypatch)
    migrated: list[Path] = []
    reopened: list[config.Settings] = []
    continued: list[bool] = []
    reports: list[str] = []
    worker_threads: list[int] = []
    gtk_thread = threading.get_ident()
    settings = config.Settings(roots=(tmp_path / "wallpapers",))

    def migrate(*, source_dir: Path) -> legacy_migration.Outcome:
        worker_threads.append(threading.get_ident())
        migrated.append(source_dir)
        return imported

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", lambda: found)
    monkeypatch.setattr(
        "wall_in_one.ui.app.legacy_migration.migrate",
        migrate,
    )
    monkeypatch.setattr("wall_in_one.ui.app.config.load_strict", lambda: settings)
    monkeypatch.setattr(application, "_replace_session_after_migration", reopened.append)
    monkeypatch.setattr(
        application,
        "_continue_first_activation",
        lambda: continued.append(True),
    )
    monkeypatch.setattr(application, "window_report", reports.append)

    try:
        assert application._prompt_for_legacy_migration()
        assert len(shown) == 1
        progress = shown[0]
        assert progress.get_heading() == "Checking older data"
        assert not progress.get_can_close()
        assert not progress.get_response_enabled("working")
        _spin_until(lambda: len(shown) == 2)
        dialog = shown[-1]
        assert dialog.get_heading() == "Older Wall-in-One data found"
        assert str(source) in dialog.get_body()
        assert "Playlists: 3 · displays: 2" in dialog.get_body()
        assert dialog.get_response_label("import") == "Import safely"
        assert dialog.get_response_label("fresh") == "Start fresh"
        assert dialog.get_response_label("later") == "Not now"

        dialog.emit("response", "import")
        assert shown[-1].get_heading() == "Importing older data"
        assert not shown[-1].get_can_close()
        _spin_until(lambda: bool(reopened))

        assert migrated == [source]
        assert worker_threads == [worker_threads[0]]
        assert worker_threads[0] != gtk_thread
        assert reopened == [settings]
        assert continued == [True]
        assert reports and str(imported.report) in reports[0]
    finally:
        _close(application, parent)


def test_not_now_is_process_local_and_does_not_continue_into_empty_root_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, parent = _application()
    found = legacy_migration.Probe("ready", "ready", tmp_path / "legacy", schema=5)
    shown = _capture_dialog(monkeypatch)
    postponed: list[Path] = []
    continued: list[bool] = []

    def postpone(*, source_dir: Path) -> legacy_migration.Outcome:
        postponed.append(source_dir)
        return legacy_migration.Outcome(False, "postponed")

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", lambda: found)
    monkeypatch.setattr(
        "wall_in_one.ui.app.legacy_migration.postpone",
        postpone,
    )
    monkeypatch.setattr(
        application,
        "_continue_first_activation",
        lambda: continued.append(True),
    )
    monkeypatch.setattr(application, "window_report", lambda _message: None)

    try:
        assert application._prompt_for_legacy_migration()
        _spin_until(lambda: len(shown) == 2)
        shown[-1].emit("response", "later")
        _spin_until(lambda: application._legacy_migration_deferred)

        assert postponed == [found.source]
        assert continued == []
        assert application._legacy_migration_deferred
        assert not legacy_migration.marker_path().exists()
        blocked = _Commands(application).authoring_gate()
        assert blocked is not None
        assert blocked.kind == "migration-decision-required"
    finally:
        _close(application, parent)


def test_start_fresh_persists_the_exact_source_decision_then_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, parent = _application()
    found = legacy_migration.Probe("ready", "ready", tmp_path / "legacy", schema=4)
    shown = _capture_dialog(monkeypatch)
    declined: list[Path] = []
    continued: list[bool] = []

    def decline(*, source_dir: Path) -> legacy_migration.Outcome:
        declined.append(source_dir)
        return legacy_migration.Outcome(True, "kept fresh")

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", lambda: found)
    monkeypatch.setattr(
        "wall_in_one.ui.app.legacy_migration.decline",
        decline,
    )
    monkeypatch.setattr(
        application,
        "_continue_first_activation",
        lambda: continued.append(True),
    )
    monkeypatch.setattr(application, "window_report", lambda _message: None)

    try:
        assert application._prompt_for_legacy_migration()
        _spin_until(lambda: len(shown) == 2)
        shown[-1].emit("response", "fresh")
        _spin_until(lambda: bool(continued))

        assert declined == [found.source]
        assert continued == [True]
        assert not application._legacy_migration_deferred
    finally:
        _close(application, parent)


def test_existing_current_authoring_shows_conflicts_and_never_offers_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, parent = _application()
    conflict = tmp_path / "config" / "settings.toml"
    found = legacy_migration.Probe(
        "conflict",
        "current authoring already exists",
        tmp_path / "legacy",
        conflicts=(conflict,),
    )
    shown = _capture_dialog(monkeypatch)
    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", lambda: found)

    try:
        assert application._prompt_for_legacy_migration()
        _spin_until(lambda: len(shown) == 2)
        dialog = shown[-1]
        assert str(conflict) in dialog.get_body()
        assert dialog.get_response_label("fresh") == "Keep current"
        assert dialog.get_response_label("import") is None
    finally:
        _close(application, parent)


def test_interrupted_import_only_offers_resume_and_not_now(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, parent = _application()
    found = legacy_migration.Probe(
        "in-progress",
        "an interrupted import can resume",
        tmp_path / "legacy",
        schema=5,
    )
    shown = _capture_dialog(monkeypatch)
    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", lambda: found)

    try:
        assert application._prompt_for_legacy_migration()
        _spin_until(lambda: len(shown) == 2)
        dialog = shown[-1]
        assert dialog.get_response_label("import") == "Resume import"
        assert dialog.get_response_label("later") == "Not now"
        assert dialog.get_response_label("fresh") is None
    finally:
        _close(application, parent)


def test_blocked_probe_keeps_gtk_responsive_before_any_first_run_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, parent = _application()
    found = legacy_migration.Probe("ready", "ready", tmp_path / "legacy", schema=5)
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []
    shown = _capture_dialog(monkeypatch)

    def probe() -> legacy_migration.Probe:
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return found

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", probe)

    try:
        assert application._prompt_for_legacy_migration()
        assert started.wait(1)
        assert len(shown) == 1
        assert shown[0].get_heading() == "Checking older data"

        pulses: list[bool] = []

        def heartbeat() -> bool:
            pulses.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(5, heartbeat)
        _spin_until(lambda: bool(pulses))
        assert len(shown) == 1
        checking = _Commands(application).authoring_gate()
        assert checking == Response.failure(
            "current-profile authoring is still opening; retry shortly",
            kind="authoring-busy",
        )

        release.set()
        _spin_until(lambda: len(shown) == 2)
        assert worker_threads[0] != threading.get_ident()
        assert shown[-1].get_heading() == "Older Wall-in-One data found"
        decision = _Commands(application).authoring_gate()
        assert decision is not None
        assert decision.kind == "migration-decision-required"
    finally:
        release.set()
        _close(application, parent)


def test_control_gate_stays_truthfully_busy_through_absent_probe_and_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, parent = _application()
    found = legacy_migration.Probe(
        "absent",
        "no legacy config",
        tmp_path / "missing-legacy",
    )
    probe_started = threading.Event()
    release_probe = threading.Event()
    repair_started = threading.Event()
    release_repair = threading.Event()

    def probe() -> legacy_migration.Probe:
        probe_started.set()
        assert release_probe.wait(2)
        return found

    def repair() -> _PlaylistReferenceRepairResult:
        repair_started.set()
        assert release_repair.wait(2)
        return _PlaylistReferenceRepairResult()

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", probe)
    monkeypatch.setattr(application, "_repair_dangling_playlist_references", repair)
    monkeypatch.setattr(application, "_continue_first_activation", lambda: None)
    commands = _Commands(application)
    checking = Response.failure(
        "current-profile authoring is still opening; retry shortly",
        kind="authoring-busy",
    )

    try:
        assert application._queue_legacy_migration_job("probe")
        assert probe_started.wait(1)
        assert commands.authoring_gate() == checking

        release_probe.set()
        _spin_until(repair_started.is_set)
        assert not application.authoring_ready
        assert commands.authoring_gate() == checking

        release_repair.set()
        _spin_until(lambda: application.authoring_ready)
        assert commands.authoring_gate() is None
    finally:
        release_probe.set()
        release_repair.set()
        _close(application, parent)


def test_probe_failure_changes_control_gate_from_checking_to_decision_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, parent = _application()
    shown = _capture_dialog(monkeypatch)
    reports: list[str] = []

    def probe() -> legacy_migration.Probe:
        raise legacy_migration.MigrationError("unreadable predecessor directory")

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", probe)
    monkeypatch.setattr(application, "window_report", reports.append)

    try:
        assert application._prompt_for_legacy_migration()
        checking = _Commands(application).authoring_gate()
        assert checking is not None
        assert checking.kind == "authoring-busy"

        _spin_until(lambda: len(shown) == 2)
        assert shown[-1].get_heading() == "Could not check older data"
        blocked = _Commands(application).authoring_gate()
        assert blocked is not None
        assert blocked.kind == "migration-decision-required"
        assert "Try again or Not now" in blocked.message
        assert reports and "unreadable predecessor directory" in reports[-1]
    finally:
        _close(application, parent)


def test_failed_current_authoring_repair_is_not_mislabeled_as_a_migration_decision() -> None:
    application, parent = _application()
    try:
        result = application._finish_playlist_reference_repair(
            _PlaylistReferenceRepairResult(failures=("schedules.json is unreadable",))
        )
        assert not result.ok
        assert result.kind == "authoring-repair-required"

        blocked = _Commands(application).authoring_gate()
        assert blocked is not None
        assert blocked.kind == "authoring-repair-required"
        assert "needs repair" in blocked.message
    finally:
        _close(application, parent)


def test_blocked_import_keeps_gtk_responsive_and_duplicate_response_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, parent = _application()
    found = legacy_migration.Probe("ready", "ready", tmp_path / "legacy", schema=5)
    outcome = legacy_migration.Outcome(True, "imported", tmp_path / "report.md")
    settings = config.Settings(roots=(tmp_path / "wallpapers",))
    started = threading.Event()
    release = threading.Event()
    calls = 0
    worker_threads: list[int] = []
    reopened: list[config.Settings] = []
    continued: list[bool] = []
    shown = _capture_dialog(monkeypatch)

    def migrate(*, source_dir: Path) -> legacy_migration.Outcome:
        nonlocal calls
        assert source_dir == found.source
        calls += 1
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return outcome

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", lambda: found)
    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.migrate", migrate)
    monkeypatch.setattr("wall_in_one.ui.app.config.load_strict", lambda: settings)
    monkeypatch.setattr(application, "_replace_session_after_migration", reopened.append)
    monkeypatch.setattr(application, "_continue_first_activation", lambda: continued.append(True))
    monkeypatch.setattr(application, "window_report", lambda _message: None)

    try:
        assert application._prompt_for_legacy_migration()
        _spin_until(lambda: len(shown) == 2)
        choice = shown[-1]
        choice.emit("response", "import")
        choice.emit("response", "import")
        assert started.wait(1)
        processing = _Commands(application).authoring_gate()
        assert processing == Response.failure(
            "current-profile authoring is still opening; retry shortly",
            kind="authoring-busy",
        )

        pulses: list[bool] = []

        def heartbeat() -> bool:
            pulses.append(True)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(5, heartbeat)
        _spin_until(lambda: bool(pulses))
        assert reopened == []
        assert continued == []
        assert calls == 1

        release.set()
        _spin_until(lambda: bool(reopened))
        assert calls == 1
        assert worker_threads[0] != threading.get_ident()
        assert continued == [True]
    finally:
        release.set()
        _close(application, parent)


def test_closed_window_discards_a_late_import_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, parent = _application()
    found = legacy_migration.Probe("ready", "ready", tmp_path / "legacy", schema=5)
    started = threading.Event()
    release = threading.Event()
    reopened: list[config.Settings] = []
    continued: list[bool] = []
    reports: list[str] = []
    shown = _capture_dialog(monkeypatch)
    settings = config.Settings(roots=(tmp_path / "wallpapers",))

    def migrate(*, source_dir: Path) -> legacy_migration.Outcome:
        assert source_dir == found.source
        started.set()
        assert release.wait(2)
        return legacy_migration.Outcome(True, "imported")

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", lambda: found)
    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.migrate", migrate)
    monkeypatch.setattr("wall_in_one.ui.app.config.load_strict", lambda: settings)
    monkeypatch.setattr(application, "_replace_session_after_migration", reopened.append)
    monkeypatch.setattr(application, "_continue_first_activation", lambda: continued.append(True))
    monkeypatch.setattr(application, "window_report", reports.append)

    try:
        assert application._prompt_for_legacy_migration()
        _spin_until(lambda: len(shown) == 2)
        shown[-1].emit("response", "import")
        assert started.wait(1)
        assert not application._on_close_request(parent)
        release.set()
        _spin_until(lambda: application._legacy_migration_future is None)

        assert reopened == []
        assert continued == []
        assert reports == []
    finally:
        release.set()
        _close(application, parent)


def test_shutdown_invalidates_a_running_import_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, parent = _application()
    found = legacy_migration.Probe("ready", "ready", tmp_path / "legacy", schema=5)
    started = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    reopened: list[config.Settings] = []
    continued: list[bool] = []
    reports: list[str] = []
    deliveries: list[bool] = []
    shown = _capture_dialog(monkeypatch)
    settings = config.Settings(roots=(tmp_path / "wallpapers",))

    def migrate(*, source_dir: Path) -> legacy_migration.Outcome:
        assert source_dir == found.source
        started.set()
        assert release.wait(2)
        worker_finished.set()
        return legacy_migration.Outcome(True, "imported")

    real_finish = application._finish_legacy_migration_job

    def finish(*arguments: object) -> bool:
        deliveries.append(True)
        return real_finish(*arguments)  # type: ignore[arg-type]

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", lambda: found)
    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.migrate", migrate)
    monkeypatch.setattr("wall_in_one.ui.app.config.load_strict", lambda: settings)
    monkeypatch.setattr(application, "_finish_legacy_migration_job", finish)
    monkeypatch.setattr(application, "_replace_session_after_migration", reopened.append)
    monkeypatch.setattr(application, "_continue_first_activation", lambda: continued.append(True))
    monkeypatch.setattr(application, "window_report", reports.append)

    try:
        assert application._prompt_for_legacy_migration()
        _spin_until(lambda: len(shown) == 2)
        shown[-1].emit("response", "import")
        assert started.wait(1)

        application._shutdown_legacy_migration_jobs(wait=False)
        release.set()
        assert worker_finished.wait(1)
        _spin_until(lambda: bool(deliveries))

        assert reopened == []
        assert continued == []
        assert reports == []
        assert application._legacy_migration_future is None
    finally:
        release.set()
        _close(application, parent)


def test_import_failure_reopens_the_safe_choice_without_continuing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, parent = _application()
    found = legacy_migration.Probe("ready", "ready", tmp_path / "legacy", schema=5)
    shown = _capture_dialog(monkeypatch)
    reports: list[str] = []
    continued: list[bool] = []

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", lambda: found)
    monkeypatch.setattr(
        "wall_in_one.ui.app.legacy_migration.migrate",
        lambda **_arguments: (_ for _ in ()).throw(legacy_migration.MigrationError("blocked")),
    )
    monkeypatch.setattr(application, "window_report", reports.append)
    monkeypatch.setattr(application, "_continue_first_activation", lambda: continued.append(True))

    try:
        assert application._prompt_for_legacy_migration()
        _spin_until(lambda: len(shown) == 2)
        shown[-1].emit("response", "import")
        _spin_until(lambda: len(shown) == 4)

        retry = shown[-1]
        assert "blocked" in retry.get_body()
        assert retry.get_response_label("import") == "Import safely"
        assert retry.get_response_label("later") == "Not now"
        assert continued == []
        assert reports and "without overwriting" in reports[-1]
    finally:
        _close(application, parent)
