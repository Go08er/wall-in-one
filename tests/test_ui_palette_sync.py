"""Live palette synchronization at the GTK application boundary."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from wall_in_one import paths  # noqa: E402
from wall_in_one.ui.app import (  # noqa: E402
    APPLICATION_STYLE_PRIORITY,
    PALETTE_RELOAD_DEBOUNCE_MS,
    Application,
)


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def application() -> Iterator[Application]:
    instance = Application()
    yield instance
    instance._stop_palette_monitor()
    instance._stills.shutdown()
    instance._session.shutdown()


def _replace(path: Path, contents: str) -> None:
    temporary = path.with_name(f".{path.name}.test-tmp")
    temporary.write_text(contents, encoding="utf-8")
    os.replace(temporary, path)


def _spin_until(predicate: Callable[[], bool], seconds: float = 2.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return predicate()


def _spin_for(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)


def test_application_palette_wins_over_the_startup_user_stylesheet() -> None:
    assert APPLICATION_STYLE_PRIORITY == Gtk.STYLE_PROVIDER_PRIORITY_USER + 1


def test_atomic_palette_replacement_triggers_reload_without_the_socket(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(application, "reload_palette", lambda: calls.append(None))
    application._start_palette_monitor()

    _replace(paths.palette_path(), "first")

    assert _spin_until(lambda: len(calls) == 1)


def test_rapid_atomic_palette_replacements_are_debounced(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(application, "reload_palette", lambda: calls.append(None))
    application._start_palette_monitor()

    for index in range(4):
        _replace(paths.palette_path(), str(index))

    assert _spin_until(lambda: len(calls) == 1)
    _spin_for(PALETTE_RELOAD_DEBOUNCE_MS / 1000 * 3)
    assert len(calls) == 1
