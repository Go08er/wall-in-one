"""Closing the window during the first activation ends the application.

The backlog item: a window closed during the first activation could leave
the process alive with no window, "from the first scan or the migration
check". Each phase is driven here in both interfaces, through the real
`Application`, as `test_ui_next_window` does:

* ``check``: while the older-data check runs on its worker;
* ``decision``: while the older-data question is asked;
* ``import``: while an import the user chose runs;
* ``scan``: while the first library scan runs;
* ``root``: while the first library folder is asked for.

The work in progress is held on its worker until the window has gone, so
each close lands mid-phase, and released 100 ms later; the test fails
(rather than hangs) if the application is still running afterwards.

What held it was the classic window's progress dialog
(`Application._show_legacy_migration_progress`), shown for the check and
for every migration operation: it can't be closed, and libadwaita answers
a window's close by closing its open dialog instead, so every close was
lost. The new interface asks on its welcome screen and shows no such
dialog, and neither the first scan nor the folder question ever held the
process; they are pinned here so they stay that way.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal

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
from wall_in_one import config, legacy_migration  # noqa: E402
from wall_in_one.session import LibraryRefreshPlan  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.window_services import UiKind  # noqa: E402

Phase = Literal["check", "decision", "import", "scan", "root"]

#: Long enough for a loaded machine; a lost close never ends at all.
DEADLINE_SECONDS = 20
#: How often the user tries the close button again while the window is up.
CLOSE_EVERY_MS = 200


def _answer(window: Gtk.Window, question: Adw.AlertDialog, response: str) -> None:
    """Press ``response``'s button, on the welcome screen or in the dialog."""
    label = question.get_response_label(response)
    if isinstance(window, NextWindow):
        window.welcome.buttons[response].emit("clicked")
        return
    pending: list[Gtk.Widget] = [question]
    while pending:
        widget = pending.pop()
        if isinstance(widget, Gtk.Button) and widget.get_label() == label:
            widget.emit("clicked")
            return
        child = widget.get_first_child()
        while child is not None:
            pending.append(child)
            child = child.get_next_sibling()
    raise AssertionError(f"no {label!r} button in {question.get_heading()!r}")


@pytest.mark.parametrize("phase", ["check", "decision", "import", "scan", "root"])
@pytest.mark.parametrize("ui", ["classic", "next"])
def test_closing_the_window_during_the_first_activation_ends_the_application(
    ui: UiKind, phase: Phase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    if phase == "root":
        config.update({"roots": ()})
    started = threading.Event()
    release = threading.Event()
    nothing = legacy_migration.probe()
    found = legacy_migration.Probe(status="ready", detail="found", source=tmp_path / "legacy")

    def held[Result](result: Result) -> Callable[..., Result]:
        def work(*_arguments: object, **_keywords: object) -> Result:
            started.set()
            release.wait(DEADLINE_SECONDS)
            return result

        return work

    probe_result = found if phase in ("decision", "import") else nothing
    monkeypatch.setattr(
        "wall_in_one.ui.app.legacy_migration.probe",
        held(nothing) if phase == "check" else lambda: probe_result,
    )
    monkeypatch.setattr(legacy_migration, "migrate", held(legacy_migration.Outcome(True, "in")))
    monkeypatch.setattr(legacy_migration, "postpone", held(legacy_migration.Outcome(False, "")))
    real_scan = LibraryRefreshPlan.scan

    def scan(plan: LibraryRefreshPlan) -> object:
        if phase == "scan":
            held(None)()
        return real_scan(plan)

    monkeypatch.setattr(LibraryRefreshPlan, "scan", scan)
    application = Application(ui=ui)
    application.set_flags(application.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    seen: dict[str, object] = {}

    expected = "Choose a library folder" if phase == "root" else "Older Wall-in-One data found"

    def asked() -> Adw.AlertDialog | None:
        """The phase's question, once asked (the check's progress is shown first)."""
        question = application._legacy_migration_prompt or application._library_root_prompt
        if isinstance(question, Adw.AlertDialog) and question.get_heading() == expected:
            return question
        return None

    def end_the_work() -> bool:
        release.set()
        return GLib.SOURCE_REMOVE

    def keep_closing(window: Gtk.Window) -> bool:
        """The close button, again while the window is still up; the work ends after."""
        if application._window is None:
            GLib.timeout_add(100, end_the_work)
            return GLib.SOURCE_REMOVE
        window.close()
        return GLib.SOURCE_CONTINUE

    def mid_phase(window: Gtk.Window) -> bool:
        question = asked()
        if phase in ("decision", "root"):
            if question is None:
                return GLib.SOURCE_CONTINUE
            seen["question"] = question.get_heading()
        elif phase == "import" and not started.is_set():
            on_screen = not isinstance(window, NextWindow) or (
                window.welcome_shown and window.welcome.question is question
            )
            if question is not None and on_screen and "question" not in seen:
                seen["question"] = question.get_heading()
                _answer(window, question, "import")
            return GLib.SOURCE_CONTINUE
        elif not started.is_set():
            return GLib.SOURCE_CONTINUE
        seen["first activation done"] = application._first_activation_done
        window.close()  # what the window's close button does
        GLib.timeout_add(CLOSE_EVERY_MS, keep_closing, window)
        return GLib.SOURCE_REMOVE

    def activated(_application: Gio.Application) -> None:
        if "armed" in seen:
            return
        seen["armed"] = True
        window: object = application._window
        assert isinstance(window, Gtk.Window)

        def mapped(*_arguments: object) -> None:
            # A close before the window is realized is a no-op in GTK.
            GLib.timeout_add(10, mid_phase, window)

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
    assert "first activation done" in seen, f"the close never came mid-phase: {seen}"
    assert seen["first activation done"] is (phase in ("scan", "root"))
    if phase in ("decision", "import", "root"):
        assert seen["question"] == expected
    assert "still running" not in seen, f"the close was lost: {seen}"
    assert application._window is None
