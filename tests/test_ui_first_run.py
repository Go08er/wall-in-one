"""The first library write destination is a choice, never an inference.

The question is the application's, so it is asked the same way in both
interfaces: a dialog over the classic window, the welcome screen in the new one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.first_run_windows import (  # noqa: E402
    UIS,
    Ui,
    application_with_window,
    capture_questions,
    respond,
)
from tests.gtk_helpers import spin_until  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.library.model import Library  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _close(application: Application, parent: Gtk.Window | None = None) -> None:
    application._window = None
    if parent is not None:
        parent.destroy()
    application._shutdown_authoring_jobs()
    application._stills.shutdown()
    application.session.shutdown()


@pytest.mark.parametrize("ui", UIS)
def test_unconfigured_app_asks_once_with_both_choices_and_the_exact_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ui: Ui
) -> None:
    suggested = tmp_path / "Noctalia wallpapers"
    suggested.mkdir()
    application, parent = application_with_window(ui)
    selected: list[Path] = []
    monkeypatch.setattr("wall_in_one.ui.app.scan.default_roots", lambda: (suggested,))
    shown = capture_questions(monkeypatch)
    monkeypatch.setattr(application, "_save_initial_library_root", selected.append)

    try:
        application._prompt_for_library_root()
        application._prompt_for_library_root()

        assert len(shown) == 1
        dialog = shown[0]
        assert dialog.get_heading() == "Choose a library folder"
        assert "has not been configured" in dialog.get_body()
        assert str(suggested) in dialog.get_body()
        assert dialog.get_response_label("default") == "Use default"
        assert dialog.get_response_label("manual") == "Choose folder manually"
        assert dialog.get_close_response() == "later"
        if isinstance(parent, NextWindow):
            welcome = parent.welcome
            assert parent.welcome_shown and welcome.title_text == "Choose a library folder"
            assert str(suggested) in welcome.body_text, "the exact folder, before any choice"
            assert list(welcome.buttons) == ["default", "manual", "later"]

        respond(parent, dialog, "default")
        assert selected == [suggested]
        assert application._library_root_prompt is None
        if isinstance(parent, NextWindow):
            spin_until(lambda: not parent.welcome_shown)
    finally:
        _close(application, parent)


def test_a_chosen_default_is_persisted_and_prevents_the_next_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    application = Application(ui="classic")
    application._authoring_migration_ready = True
    # This test is about the durable settings boundary; a service is not
    # running in the isolated XDG fixture and does not need a runtime document.
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)

    try:
        application._save_initial_library_root(root)

        spin_until(
            lambda: (
                config.load().roots == (root,)
                and application.settings.roots == (root,)
                and not application._authoring_active
            )
        )
        assert config.load().roots == (root,)
        assert application.settings.roots == (root,)
        application._prompt_for_library_root()
        assert application._library_root_prompt is None
    finally:
        _close(application)


def test_unconfigured_state_never_generates_into_a_detected_scan_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detected = tmp_path / "detected"
    detected.mkdir()
    application = Application(ui="classic")
    application.session.adopt_library(Library(roots=(detected,), items=()))
    requested: list[object] = []
    monkeypatch.setattr(
        application._stills,
        "request",
        lambda *_arguments, **_keywords: requested.append(object()),
    )

    try:
        application._make_missing_stills()
        assert requested == []
    finally:
        _close(application)
