"""Closing the window while its first activation is still checking for older data.

The check runs on a worker behind a progress dialog that can't be closed
(`Application._show_legacy_migration_progress`). libadwaita answers a
window's close by closing its open dialog instead, so in the classic window
the close was lost: the window, and the process with it, stayed until the
check ended, and on after it. The new interface asks in place, on its
welcome screen, and is covered here so it stays that way.

Each test runs the real `Application`, as `test_ui_next_window` does, with
the check held on its worker until the window has been closed, and fails
(rather than hangs) if the application is still running afterwards.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from tests.test_ui_next_window import (  # noqa: E402,F401
    application_lanes,
    sandboxed_runtime,
    toolkit,
)
from wall_in_one import legacy_migration  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.window_services import UiKind  # noqa: E402

#: Long enough for a loaded machine; a lost close never ends at all.
DEADLINE_SECONDS = 20


@pytest.mark.parametrize("ui", ["classic", "next"])
def test_closing_the_window_while_older_data_is_checked_ends_the_application(
    ui: UiKind, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    nothing = legacy_migration.probe()
    started = threading.Event()
    release = threading.Event()

    def held_probe() -> legacy_migration.Probe:
        started.set()
        release.wait(DEADLINE_SECONDS)
        return nothing

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", held_probe)
    application = Application(ui=ui)
    application.set_flags(application.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    seen: dict[str, object] = {}

    def close_when_checking(window: Gtk.Window) -> bool:
        if not started.is_set():
            return GLib.SOURCE_CONTINUE
        dialog = window.get_visible_dialog() if isinstance(window, Adw.ApplicationWindow) else None
        seen["dialog"] = dialog.get_heading() if isinstance(dialog, Adw.AlertDialog) else None
        seen["first activation done"] = application._first_activation_done
        window.close()  # what the window's close button does
        # The check ends only after the window has gone: it lands windowless.

        def end_the_check() -> bool:
            release.set()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(100, end_the_check)
        return GLib.SOURCE_REMOVE

    def activated(_application: Gio.Application) -> None:
        if "armed" in seen:
            return
        seen["armed"] = True
        window: object = application._window
        assert isinstance(window, Gtk.Window)

        def mapped(*_arguments: object) -> None:
            # A close before the window is realized is a no-op in GTK.
            GLib.timeout_add(10, close_when_checking, window)

        window.connect("map", mapped)
        if window.get_mapped():
            mapped()

    def watchdog() -> bool:
        seen["still running"] = [type(w).__name__ for w in application.get_windows()]
        release.set()
        application.quit()
        return GLib.SOURCE_REMOVE

    application.connect_after("activate", activated)
    dog = GLib.timeout_add_seconds(DEADLINE_SECONDS, watchdog)
    lanes: list[object] = []
    try:
        application.run([])
        lanes.extend(application_lanes(application))
    finally:
        release.set()
        for lane in lanes:
            lane.shutdown(wait=True)  # type: ignore[attr-defined]
    if "still running" not in seen:
        GLib.source_remove(dog)
    assert seen.get("first activation done") is False, "closed while the check was running"
    if ui == "classic":
        assert seen["dialog"] == "Checking older data", "behind its progress dialog"
    assert "still running" not in seen, f"the close was lost: {seen}"
    assert application._window is None
