"""The window, between an update and the wallpaper service's restart.

The service still running is the older build. It refuses a document the new
app wrote that it cannot load (0.4 dropped `niri_program`, which 0.3.x
requires) and keeps its last good configuration; every save the window makes
until then is refused the same way. The window says what ends it.
"""

from __future__ import annotations

from types import SimpleNamespace

from wall_in_one import __version__
from wall_in_one.ui.app import Application

REFUSAL = "config is not valid TOML: missing field `niri_program`"


def test_a_refused_save_says_to_restart_a_service_older_than_the_app() -> None:
    window = SimpleNamespace(runtime_status={"status_version": 2, "runtime_version": "0.0.1"})

    said = Application._rejected_configuration(window, REFUSAL)  # type: ignore[arg-type]

    assert said.startswith(f"Runtime rejected the new configuration: {REFUSAL}")
    assert "Restart the wallpaper service to finish updating" in said


def test_a_refused_save_from_a_current_service_says_only_why() -> None:
    window = SimpleNamespace(runtime_status={"status_version": 2, "runtime_version": __version__})

    said = Application._rejected_configuration(window, REFUSAL)  # type: ignore[arg-type]

    assert said == f"Runtime rejected the new configuration: {REFUSAL}"
