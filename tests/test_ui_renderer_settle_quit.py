"""A number nudged just before the application quits is saved for its wallpaper (review r13b, G1).

A number waits out `dial_rows.SETTLE_MS` before it is handed on. A window that closes hands every
waiting one on at once (L8), but a quit never did: ``ctl quit`` in ``--service`` mode
(`Control.quit` → `request_quit` → ``quit()``) and a ``quit()`` from elsewhere (SIGINT, through
PyGObject's fallback) end the loop with the window open, and `do_shutdown` destroys it with no
close-request, so the number was lost. Each now hands it on through the same queued write, and
the application waits for that write.

Each kind: ``ctl`` (the real socket), ``request`` (what `Control.quit` does on GTK, at once),
``quit`` (``Application.quit()``, as SIGINT does), all in ``--service`` mode, whose publication
is synchronous; and ``quit-windowed``, the same in an ordinary windowed application, whose
publication runs on the runtime lane. Control: the same after the number was saved.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib  # noqa: E402

from tests.test_ui_next_slice import library_page  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    STEP_SECONDS,
    Step,
    application_lanes,
    control,
    run_application,
    sandboxed_runtime,
    settled,
)
from tests.test_ui_renderer_dials import _own, toolkit  # noqa: E402,F401
from wall_in_one import paths, runtime_compatibility  # noqa: E402
from wall_in_one.library.model import Kind  # noqa: E402
from wall_in_one.ui import dial_rows  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402


def _after(seconds: float) -> Callable[[], bool]:
    deadline = time.monotonic() + seconds
    return lambda: time.monotonic() >= deadline


def _clip(tmp_path: Path) -> Path:
    folder = tmp_path / "wallpapers"
    clip = folder / "clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"video")
    clip.with_name("clip-still.png").write_bytes(b"placeholder image bytes")
    return clip


def _scanned(application: Application, clip: Path) -> Callable[[], bool]:
    def ready() -> bool:
        if not settled(application):
            return False
        window = application._window
        if isinstance(window, NextWindow):
            return window.state.has_wallpaper(str(clip))
        return application.session.library.find(clip) is not None

    return ready


def _rows(application: Application, clip: Path) -> tuple[Adw.SpinRow, Adw.SpinRow]:
    """Open the wallpaper's own settings, the way a person does: its Speed and Brightness."""
    window = application._window
    if isinstance(window, NextWindow):
        page = library_page(window)
        page.inspect(window.state.wallpaper(str(clip)))
        rows = page.inspector.renderer_rows
    else:
        assert isinstance(window, MainWindow)
        item = application.session.library.find(clip)
        assert item is not None
        window._on_tile_activated(item)
        rows = window._pairings_page.renderer_rows
    speed, brightness = rows["dial:video:speed"], rows["dial:video:brightness"]
    assert isinstance(speed, Adw.SpinRow) and isinstance(brightness, Adw.SpinRow)
    return speed, brightness


@pytest.mark.parametrize("ui", ["classic", "next"])
@pytest.mark.parametrize("how", ["ctl", "request", "quit", "quit-windowed"])
@pytest.mark.parametrize("wait_first", [False, True], ids=["quit-at-once", "control"])
def test_a_quit_saves_a_number_nudged_just_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ui: Any, how: str, wait_first: bool
) -> None:
    """Before: speed None after each quit-at-once, ``ctl``'s only when its round trip beat the
    timer (it lost in about two runs of three)."""
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    clip = _clip(tmp_path)
    windowed = how == "quit-windowed"
    application = Application(service=not windowed, ui=ui)
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")
    lanes: list[Any] = []
    answers: list[Future[Any]] = []

    def scenario() -> Iterator[Step]:
        yield "the first scan", _scanned(application, clip)
        speed, brightness = _rows(application, clip)
        # Two numbers waiting: two writes, the second queued behind the first.
        speed.set_value(1.5)
        brightness.set_value(20)
        if wait_first:
            yield "the settle time to pass", _after((dial_rows.SETTLE_MS + 400) / 1000)
            yield "the saves to finish", lambda: settled(application)
        elif how == "ctl":
            yield "a moment", _after(0.1)
        lanes.extend(application_lanes(application))
        if how == "ctl":
            answers.append(control(requests, "quit"))
        elif how == "request":
            GLib.idle_add(application.request_quit)
        else:
            application.quit()

    def launch() -> bool:
        application.activate()  # a resident application is activated by a launch
        return GLib.SOURCE_REMOVE

    if not windowed:
        GLib.idle_add(launch)  # a windowed one is activated by its own run
    try:
        assert run_application(application, scenario()) == 0
        if how == "ctl":
            assert answers and answers[0].result(timeout=STEP_SECONDS).ok
    finally:
        requests.shutdown(wait=True)
        for lane in lanes:
            lane.shutdown(wait=True)
    left = dict(dial_rows._PENDING)
    dial_rows._PENDING.clear()  # a timer of a loop that has ended: nothing may reach the next test
    own = _own(clip, Kind.VIDEO)
    assert own.dial("video", "speed") == 1.5, f"{ui}/{how}: speed after the quit: {own}"
    assert own.dial("video", "brightness") == 20, f"{ui}/{how}: brightness after the quit: {own}"
    assert not left, f"{ui}/{how}: left waiting: {left}"
    # And published, as a close's is: the write's own tail ran before the loop ended.
    document = paths.runtime_config_path().read_text(encoding="utf-8")
    lines = [line for line in document.splitlines() if "speed" in line or "brightness" in line]
    assert any("1.5" in line for line in lines), f"{ui}/{how}: the published document: {lines}"
    assert any("20" in line for line in lines), f"{ui}/{how}: the published document: {lines}"
