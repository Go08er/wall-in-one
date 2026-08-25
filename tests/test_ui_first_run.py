"""The first library write destination is a choice, never an inference."""

from __future__ import annotations

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

from wall_in_one import config  # noqa: E402
from wall_in_one.library.model import Library  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402


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


def _spin_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not predicate():
        while context.pending():
            context.iteration(False)
        if time.monotonic() >= deadline:
            raise AssertionError("GLib callback did not arrive before the test deadline")
        time.sleep(0.002)


def test_unconfigured_app_asks_once_with_both_choices_and_the_exact_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suggested = tmp_path / "Noctalia wallpapers"
    suggested.mkdir()
    application = Application()
    parent = Gtk.Window()
    application._window = cast(MainWindow, parent)
    shown: list[Adw.AlertDialog] = []
    selected: list[Path] = []
    monkeypatch.setattr("wall_in_one.ui.app.scan.default_roots", lambda: (suggested,))
    monkeypatch.setattr(
        Adw.AlertDialog,
        "present",
        lambda dialog, _parent: shown.append(dialog),
    )
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

        dialog.emit("response", "default")
        assert selected == [suggested]
        assert application._library_root_prompt is None
    finally:
        _close(application, parent)


def test_a_chosen_default_is_persisted_and_prevents_the_next_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    application = Application()
    application._authoring_migration_ready = True
    # This test is about the durable settings boundary; a service is not
    # running in the isolated XDG fixture and does not need a runtime document.
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)

    try:
        application._save_initial_library_root(root)

        _spin_until(
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
    application = Application()
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
