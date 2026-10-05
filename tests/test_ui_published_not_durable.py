"""The application keeps, and says, what a not-durable write published.

`tests/test_published_not_durable.py` pins each writer; these pin the lanes
above them. A store transaction run on the authoring lane, a settings
control verb and the synchronous settings path each published new bytes and
then failed the folder sync, and each was treated as a refusal, so the
caller heard "failed" while the files said otherwise.
"""

from __future__ import annotations

import errno
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_runtime import (  # noqa: E402,F401
    FakeWindow,
    _application,
    _attach,
    _close,
    toolkit,
)
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.control import server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import playlists, state_file  # noqa: E402
from wall_in_one.ui.app import Application, _Commands  # noqa: E402

SAVED_BUT = "was saved, but syncing its folder failed"


def _sync_fails_for(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Path]:
    """Fail the folder sync after ``name`` is renamed into place, and only that one."""
    real = state_file.fsync_parent
    failed: list[Path] = []

    def sync(path: Path) -> None:
        if Path(path).name == name:
            failed.append(Path(path))
            raise OSError(errno.EIO, "injected post-replace directory sync failure")
        real(path)

    monkeypatch.setattr(state_file, "fsync_parent", sync)
    return failed


def _ctl(application: Application, verb: str, argument: str) -> Response:
    replies: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(application)),
        Request(verb, argument).encode(),
        replies.append,
    )
    spin_until(lambda: bool(replies) and not application._authoring_active, timeout=5)
    return replies[0]


def test_a_store_change_on_the_authoring_lane_is_kept_and_said_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    failed = _sync_fails_for(monkeypatch, "playlists.json")
    try:
        reply = _ctl(application, "playlist-new", "Evening")

        assert failed, "the playlist store's own publication failed its sync"
        assert reply.ok, reply.message  # before: a failure, "could not write"
        assert SAVED_BUT in reply.message, "the ctl caller is told too"
        assert application.session.playlists.by_name("Evening") is not None
        spin_until(lambda: any(SAVED_BUT in report for report in window.reports))
        assert not any("nothing changed" in report for report in window.reports)
    finally:
        _close(application)


def test_a_settings_verb_is_kept_and_said_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: None)
    failed = _sync_fails_for(monkeypatch, "settings.toml")
    try:
        reply = _ctl(application, "cycle-interval", "120")

        assert failed == [paths.settings_path()]
        assert reply.ok, reply.message  # before: a failure, and the old interval kept
        assert reply.message.startswith("cycle-interval 120")
        assert SAVED_BUT in reply.message
        assert config.load_strict().cycle_interval == 120
        assert application.settings.cycle_interval == 120
        spin_until(lambda: any(SAVED_BUT in report for report in window.reports))
    finally:
        _close(application)


def test_the_synchronous_settings_path_keeps_what_it_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: None)
    failed = _sync_fails_for(monkeypatch, "settings.toml")
    try:
        saved = application.update_settings(opacity=0.8)  # before: SettingsNotDurableError

        assert failed == [paths.settings_path()]
        assert saved.opacity == 0.8
        assert application.settings.opacity == 0.8
        assert config.load_strict().opacity == 0.8
    finally:
        _close(application)


def test_a_deleted_default_cleared_before_the_sync_failed_is_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the default playlist clears the saved default, in the same task."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    evening = playlists.Store.open().create("Evening")
    config.save(replace(config.Settings(), active_playlist=evening.id))
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    assert application.settings.active_playlist == evening.id
    failed = _sync_fails_for(monkeypatch, "settings.toml")
    try:
        reply = _ctl(application, "playlist-delete", "Evening")

        assert failed == [paths.settings_path()]
        assert reply.ok, reply.message
        # Before: "cleanup needs attention: saved default: ..." with the old default kept.
        assert "needs attention" not in reply.message
        assert SAVED_BUT in reply.message
        assert config.load_strict().active_playlist == ""
        assert application.settings.active_playlist == "", "the app holds what the file holds"
    finally:
        _close(application)


def test_startup_repair_that_cleared_the_default_before_the_sync_failed_opens_authoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config.save(replace(config.Settings(), active_playlist="deleted"))
    application = Application()
    failed = _sync_fails_for(monkeypatch, "settings.toml")
    try:
        repaired = application._repair_dangling_playlist_references()
        response = application._finish_playlist_reference_repair(repaired)

        assert failed == [paths.settings_path()]
        assert response.ok, response.message  # before: authoring-repair-required
        assert bool(application.authoring_ready)
        assert config.load_strict().active_playlist == ""
        assert application.settings.active_playlist == ""
    finally:
        _close(application)
