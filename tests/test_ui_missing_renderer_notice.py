"""A missing renderer is said once, in both windows, and in the setup wording.

The service keeps playing a playlist that mixes videos and stills with
mpvpaper absent: each video comes with ``renderer_failed`` and the same
``last_error``, and a still in between clears it. Both windows say a new
renderer error as a toast; a missing renderer's must be said once while the
service's list of missing renderers stays the same, not on every rotation
onto a video (review of r9-integration, finding 2, its probe folded in).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw  # noqa: E402

from tests.gtk_helpers import destroy_window  # noqa: E402
from tests.started import started, started_application  # noqa: E402
from wall_in_one import setup_check  # noqa: E402

MISSING = (
    "mpvpaper is missing: cannot start /usr/bin/mpvpaper: No such file or directory "
    "(os error 2); paired still is active"
)
CRASHED = 'video entry "entry-0" stopped because mpvpaper exited on ALL (exit status: 1)'


def _status(
    entry: str,
    *,
    error: str = "",
    missing: tuple[str, ...] = ("mpvpaper",),
) -> dict[str, object]:
    return {
        "playlist_id": "clips",
        "playlist": "Clips",
        "source": "default",
        "entry_id": entry,
        "playback_state": "playing",
        "paused": False,
        "cycle_enabled": True,
        "shuffle": False,
        "renderer_failed": bool(error),
        "last_error": error,
        "taboo_entries": [],
        "missing_renderers": [
            {"program": program, "path": f"/usr/bin/{program}", "reason": "not-found"}
            for program in missing
        ],
    }


def _rotation() -> list[dict[str, object]]:
    """Video, still, video, still, video: one rotation each."""
    return [
        _status(f"entry-{index}", error=MISSING if index % 2 == 0 else "") for index in range(5)
    ]


def _classic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    from wall_in_one import config
    from wall_in_one.library.model import Library
    from wall_in_one.session import Session
    from wall_in_one.theme import source

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.MissingRendererNotice")
            self.settings = config.Settings()
            self.resolved_palette = source.resolve()
            self.session = Session(
                self.settings, scanner=lambda _roots: Library(roots=(), items=())
            )
            self.session.refresh()

        def refresh_library(self) -> None: ...

        def runtime_action_async(self, verb: str, argument: str | None = None) -> bool:
            return True

        def with_task_durability(self, message: str) -> str:
            return message

    return started(FakeApp())


def test_the_classic_window_says_a_missing_renderer_once_and_in_the_setup_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.ui.window import MainWindow

    application = _classic(tmp_path, monkeypatch)
    window = MainWindow(application, application.settings)  # type: ignore[arg-type, attr-defined]
    reports: list[str] = []
    monkeypatch.setattr(window, "report", reports.append)
    try:
        for status in _rotation():
            window.show_runtime_status(status)
        assert reports == [MISSING]
        window.show_runtime_status(_status("entry-5", error=MISSING))
        assert window._runtime_status_text.startswith(setup_check.RENDERER_MISSING_STATE)
        assert window._runtime_summary.startswith(setup_check.RENDERER_MISSING_SUMMARY)
        # Another renderer goes missing too: its error is said.
        scene = MISSING.replace("mpvpaper", "linux-wallpaperengine")
        both = _status("entry-6", error=scene, missing=("mpvpaper", "linux-wallpaperengine"))
        window.show_runtime_status(both)
        assert reports == [MISSING, scene]
        # A renderer that crashed is still said, and still offers Retry.
        window.show_runtime_status(_status("entry-7", error=CRASHED))
        assert reports[-1] == CRASHED
        assert window._runtime_status_text.startswith("Renderer stopped · Retry available")
        # The service went away and came back: said again.
        window.show_runtime_unavailable()
        window.show_runtime_status(_status("entry-8", error=MISSING))
        assert reports[-1] == MISSING and len(reports) == 4, reports
    finally:
        destroy_window(window)
        application.session.shutdown()  # type: ignore[attr-defined]


def test_the_new_window_says_a_missing_renderer_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from wall_in_one.ui.next.window import NextWindow

    application = started_application(ui="next")
    window = NextWindow(application, application.settings)
    reports: list[str] = []
    monkeypatch.setattr(window, "report", reports.append)
    try:
        for status in _rotation():
            window.show_runtime_status(status)
        assert reports == [MISSING]
        window.show_runtime_status(_status("entry-5", error=CRASHED))
        assert reports == [MISSING, CRASHED]
        window.show_runtime_unavailable()
        window.show_runtime_status(_status("entry-6", error=MISSING))
        assert reports == [MISSING, CRASHED, MISSING]
    finally:
        destroy_window(window)
        application._stills.shutdown()
        application.session.shutdown()
