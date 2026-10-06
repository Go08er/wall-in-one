"""The application keeps, and says, what a not-durable write published.

`tests/test_published_not_durable.py` pins each writer; these pin the lanes
above them. A store transaction run on the authoring lane, a settings
control verb and the synchronous settings path each published new bytes and
then failed the folder sync, and each was treated as a refusal, so the
caller heard "failed" while the files said otherwise.
"""

from __future__ import annotations

import errno
import logging
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
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
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


def test_a_rename_that_repointed_the_default_before_the_sync_failed_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An older profile's default names the playlist by name; the rename repoints it first."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    dock = playlists.Store.open().create("Dock")
    config.save(replace(config.Settings(), active_playlist="Dock"))
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    assert application.settings.active_playlist == "Dock"
    failed = _sync_fails_for(monkeypatch, "settings.toml")
    # Run here, off the lane, a warning goes to the log unless a sink is set.
    monkeypatch.setattr(state_file, "_durability_sink", None, raising=False)
    caplog.set_level(logging.WARNING)
    try:
        work = application.prepare_playlist_rename(dock.id, "Harbour")
        result = work()  # before: SettingsNotDurableError, and the rename never ran

        assert failed == [paths.settings_path()]
        assert result.renamed is not None and result.renamed.name == "Harbour"
        assert result.settings is not None and result.settings.active_playlist == dock.id
        assert sum(SAVED_BUT in message for message in caplog.messages) == 1
        application.adopt_playlist_rename(result)
        assert application.settings.active_playlist == dock.id
        assert config.load_strict().active_playlist == dock.id
    finally:
        _close(application)


def test_a_damaged_list_moved_aside_and_rewritten_without_syncs_is_one_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X-2c's move aside and the save after it: two warnings, one task, one message."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = paths.app_state_dir() / "favourites.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"{ not json")
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    path = tmp_path / "library" / "paper.png"
    item = MediaItem(path=path, kind=Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library(roots=(path.parent,), items=(item,)))
    assert application.session.favourites.fault is not None
    failed = _sync_fails_for(monkeypatch, "favourites.json")
    try:
        heard = len(window.reports)
        reply = _ctl(application, "favourite", str(path))
        spin_until(lambda: len(window.reports) > heard)

        assert len(failed) == 2, "the move aside, then the new file"
        assert reply.ok, reply.message
        assert "was moved aside to favourites.json.broken" in reply.message
        assert f"favourites.json {SAVED_BUT}" in reply.message
        new = window.reports[heard:]
        assert len(new) == 1, new
        assert "moved aside" in new[0] and SAVED_BUT in new[0]
        assert application.session.favourites.is_favourite(path)
    finally:
        _close(application)
