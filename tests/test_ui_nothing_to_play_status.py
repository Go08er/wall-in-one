"""The classic window says what the service reports when every display plays nothing.

The service's own reply for the app's web-off document (``service/tests/fixtures/
web-off/status-nothing-to-play.json``, captured from it; `service/tests/service.rs`
keeps its shape): rows with empty entry fields, and ``nothing_to_play`` at the top.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import destroy_window  # noqa: E402
from tests.test_ui_missing_renderer_notice import _classic  # noqa: E402

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "service/tests/fixtures/web-off/status-nothing-to-play.json"
)


def test_the_classic_window_says_nothing_to_play_not_playing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.ui.window import MainWindow

    application = _classic(tmp_path, monkeypatch)
    window = MainWindow(application, application.settings)  # type: ignore[arg-type, attr-defined]
    reports: list[str] = []
    monkeypatch.setattr(window, "report", reports.append)
    try:
        window.show_runtime_status(json.loads(FIXTURE.read_text(encoding="utf-8")))
        assert window._runtime_status_text.startswith(
            "Nothing to play: web wallpapers are off · cycle off"
        ), window._runtime_status_text
        # (It was captured without a compositor, which the window does report.)
        assert not any("nothing to play" in said.lower() for said in reports), reports
    finally:
        destroy_window(window)
        application.session.shutdown()  # type: ignore[attr-defined]
