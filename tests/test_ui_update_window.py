"""The window, between an update and the wallpaper service's restart.

The service still running is the older build. It refuses a document the new
app wrote that it cannot load (0.4 dropped `niri_program`, which 0.3.x
requires) and keeps its last good configuration; every save the window makes
until then is refused the same way. The window says what ends it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from wall_in_one import __version__
from wall_in_one.ui import app
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


def test_the_hint_follows_a_multiline_refusal_on_its_last_line() -> None:
    window = SimpleNamespace(runtime_status={"status_version": 2, "runtime_version": "0.0.1"})

    refusal = "TOML parse error\nmissing field `niri_program`\n"

    said = Application._rejected_configuration(window, refusal)  # type: ignore[arg-type]

    assert said.endswith(f"older than this app ({__version__}).")
    assert "missing field `niri_program` Restart the wallpaper service" in said


def test_every_refused_save_in_the_window_is_worded_in_one_place() -> None:
    """The four places the window reports a refused document all go through
    `_rejected_configuration`, so none can lose the restart hint."""
    source = Path(app.__file__).read_text(encoding="utf-8")

    assert source.count("Runtime rejected the new configuration") == 1
    assert source.count("self._rejected_configuration(") == 4
