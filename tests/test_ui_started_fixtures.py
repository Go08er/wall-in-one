"""The suite's window fixtures build their windows over a started application.

GTK only adds a window to an application that has emitted ``startup``;
otherwise it says "Gtk-CRITICAL: New application windows must be added
after the GApplication::startup signal has been emitted" and the window is
never the application's. The review found that critical in the X-1 run,
from the new window's Settings fixture; a scan of the GUI suite found it in
47 tests. Each fixture now starts its application first (`tests.started`):
these pin that the windows they hand out belong to a registered one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.first_run_windows import application_with_window  # noqa: E402
from tests.started import StartedApplication, started_application  # noqa: E402
from tests.test_ui_next_settings import _close, _open  # noqa: E402
from tests.test_ui_next_window import sandboxed_runtime, toolkit  # noqa: E402,F401
from wall_in_one import runtime_compatibility  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


def test_the_settings_fixture_window_belongs_to_a_started_application(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one the review saw, through the X-1 test that uses it."""
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    opened = _open(monkeypatch)
    try:
        assert opened.application.get_is_registered()  # before: never started
        assert opened.window in opened.application.get_windows()
    finally:
        _close(opened)


def test_the_first_run_fixture_window_belongs_to_a_started_application() -> None:
    application, window = application_with_window("next")
    try:
        assert application.get_is_registered()
        assert window in application.get_windows()
    finally:
        application._window = None
        window.destroy()
        application._stills.shutdown()
        application.session.shutdown()


def test_a_started_application_does_no_process_work() -> None:
    """Started for GTK only: no control socket, palette monitor or older-data check."""
    application = started_application("next")
    try:
        assert isinstance(application, StartedApplication)
        assert application.get_is_registered()
        assert application._control is None
        assert application._palette_monitor is None
        assert application._legacy_migration_future is None
        window = NextWindow(application, application.settings)
        assert window in application.get_windows()
        window.destroy()
    finally:
        application._stills.shutdown()
        application.session.shutdown()
