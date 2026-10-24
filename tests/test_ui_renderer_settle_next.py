"""The next UI's details: a number's settle timer and the reset beside it (L2).

A number nudged and then "Use defaults" within `dial_rows.SETTLE_MS` must stay at
the default, and one nudged on a wallpaper must not be written once the details
show another.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw  # noqa: E402

from tests.test_ui_next_slice import finish, library_page, run  # noqa: E402
from tests.test_ui_next_window import Step, sandboxed_runtime, settled  # noqa: E402
from tests.test_ui_renderer_dials import _own, _reset_of, toolkit  # noqa: E402,F401
from wall_in_one import runtime_compatibility  # noqa: E402
from wall_in_one.library.model import Kind  # noqa: E402
from wall_in_one.ui import dial_rows  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


def _after(seconds: float) -> Callable[[], bool]:
    deadline = time.monotonic() + seconds
    return lambda: time.monotonic() >= deadline


@pytest.mark.parametrize("how", ["use defaults", "per-dial reset", "another wallpaper"])
def test_a_pending_number_is_not_written_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    folder = tmp_path / "wallpapers"
    clip = folder / "clip.mp4"
    other = folder / "other.mp4"
    for path in (clip, other):
        path.write_bytes(b"video")
        path.with_name(path.stem + "-still.png").write_bytes(b"placeholder image bytes")
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        state = window.state
        yield (
            "the first scan",
            lambda: (
                settled(application)
                and state.has_wallpaper(str(clip))
                and state.has_wallpaper(str(other))
            ),
        )
        page = library_page(window)
        page.inspect(state.wallpaper(str(clip)))
        # Something of its own, so the resets are live.
        deband = page.inspector.renderer_rows["dial:video:deband"]
        assert isinstance(deband, Adw.SwitchRow)
        deband.set_active(True)
        yield (
            "deband to be saved",
            lambda: settled(application) and _own(clip, Kind.VIDEO).dial("video", "deband") is True,
        )
        yield "the details to be built again", _after(0.6)
        speed = page.inspector.renderer_rows["dial:video:speed"]
        assert isinstance(speed, Adw.SpinRow)
        if how == "per-dial reset":
            speed.set_value(1.25)
            yield (
                "the first speed to be saved",
                lambda: (
                    settled(application) and _own(clip, Kind.VIDEO).dial("video", "speed") == 1.25
                ),
            )
            yield "the details to be built again", _after(0.6)
            speed = page.inspector.renderer_rows["dial:video:speed"]
            assert isinstance(speed, Adw.SpinRow)
        speed.set_value(1.5)
        if how == "use defaults":
            reset = page.inspector.renderer_reset
            assert reset is not None and reset.get_sensitive()
            reset.emit("clicked")
        elif how == "per-dial reset":
            _reset_of(speed).emit("clicked")
        else:
            page.inspect(state.wallpaper(str(other)))
        yield "the settle time to pass", _after((dial_rows.SETTLE_MS + 400) / 1000)
        yield "the saves to finish", lambda: settled(application)
        own = _own(clip, Kind.VIDEO)
        if how == "another wallpaper":
            # Saved for the wallpaper it was entered for; never for the one shown now.
            assert own.dial("video", "speed") == 1.5, f"{how}: speed lost: {own}"
        else:
            assert own.dial("video", "speed") is None, f"{how}: speed written back: {own}"
        assert not _own(other, Kind.VIDEO)
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


@pytest.mark.parametrize(
    "gap", [0.3, 0.7], ids=["second-nudge-inside-first-settle", "control-after"]
)
def test_two_numbers_nudged_close_together_are_both_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gap: float
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    folder = tmp_path / "wallpapers"
    clip = folder / "clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"video")
    clip.with_name("clip-still.png").write_bytes(b"placeholder image bytes")
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        state = window.state
        yield "the first scan", lambda: settled(application) and state.has_wallpaper(str(clip))
        page = library_page(window)
        page.inspect(state.wallpaper(str(clip)))
        speed = page.inspector.renderer_rows["dial:video:speed"]
        assert isinstance(speed, Adw.SpinRow)
        speed.set_value(1.5)
        yield "the second nudge", _after(gap)
        brightness = page.inspector.renderer_rows["dial:video:brightness"]
        assert isinstance(brightness, Adw.SpinRow)
        brightness.set_value(20)
        yield "the settle time to pass", _after((dial_rows.SETTLE_MS + 900) / 1000)
        yield "the saves to finish", lambda: settled(application)
        own = _own(clip, Kind.VIDEO)
        assert own.dial("video", "speed") == 1.5, f"speed lost: {own}"
        assert own.dial("video", "brightness") == 20, f"brightness lost: {own}"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)
