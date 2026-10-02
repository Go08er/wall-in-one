"""A settings save that published but couldn't be confirmed durable (audit B-2).

The atomic replacement succeeds and only the folder sync fails: the file now
holds the new value, so the window must keep it and must not say "nothing
changed".
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.library import state_file  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def application(tmp_path: Path) -> Iterator[Application]:
    root = tmp_path / "Library"
    root.mkdir()
    config.save(config.Settings(roots=(root,), scan_workshop=False, opacity=0.4))
    app = Application()
    app._authoring_migration_ready = True
    try:
        yield app
    finally:
        app._runtime_shutdown = True
        jobs = app._authoring_jobs
        app._shutdown_authoring_jobs()
        if jobs is not None:
            jobs.shutdown(wait=True, cancel_futures=True)
        app._stills.shutdown()
        app.session.shutdown()


def test_a_save_published_before_its_sync_failed_is_kept_and_reported_honestly(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports: list[str] = []
    monkeypatch.setattr(
        Application, "window_report", lambda _self, message: reports.append(message)
    )

    def sync_fails(_path: Path) -> None:
        raise OSError(5, "injected post-replace directory sync failure")

    monkeypatch.setattr(state_file, "fsync_parent", sync_fails)
    saved: list[float] = []

    assert application.update_settings_async(
        opacity=0.8, on_success=lambda settings: saved.append(settings.opacity)
    )
    spin_until(lambda: not application._settings_authoring_running and bool(reports), timeout=5)

    assert config.load_strict().opacity == 0.8, "the new bytes are on disk"
    assert application.settings.opacity == 0.8, "the window keeps what the file holds"
    assert saved == [0.8]
    assert not any("nothing changed" in report for report in reports)
    assert reports[-1].startswith("Settings were saved, but Wall-in-One couldn't confirm")
    assert "may not survive a power loss" in reports[-1]
