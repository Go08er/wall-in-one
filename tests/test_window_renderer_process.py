"""GPU acceleration in a fresh process: GTK really starts under each choice.

The renderer can only be chosen before GTK loads, so this runs a child
Python: it makes the launch's choice (`cli._choose_window_renderer`) from the
test's ui.toml, then initialises GTK, shows a window and reports the renderer
GTK picked. ``GDK_DISABLE=gl,vulkan`` stands for a machine without a usable
GPU: with acceleration on GTK must still fall back to Cairo by itself, and
with it off Cairo is what was asked for.

The child inherits this test's isolated profile (``HOME`` and the XDG
directories under ``tmp_path``) and the dead session-bus address the conftest
guard installs, and is bounded by a timeout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")

from wall_in_one import ui_prefs  # noqa: E402

CHILD = """
import json, os, time
from wall_in_one import cli
cli._choose_window_renderer()
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk
Gtk.init()
window = Gtk.Window(title="renderer probe")
window.present()
context = GLib.MainContext.default()
deadline = time.monotonic() + 20
while not window.get_mapped() and time.monotonic() < deadline:
    context.iteration(False)
renderer = window.get_renderer()
print(json.dumps({
    "variable": os.environ.get("GSK_RENDERER"),
    "mapped": window.get_mapped(),
    "renderer": type(renderer).__name__ if renderer is not None else None,
}))
window.destroy()
"""


@pytest.mark.parametrize(("gpu", "variable"), [(True, None), (False, "cairo")])
def test_a_window_starts_without_a_gpu_whether_acceleration_is_on_or_off(
    tmp_path: Path, gpu: bool, variable: str | None
) -> None:
    assert os.environ["HOME"].startswith(str(tmp_path)), "the child must inherit the sandbox"
    assert os.environ["XDG_CONFIG_HOME"].startswith(str(tmp_path))
    ui_prefs.update({"gpu_acceleration": gpu})
    environ = dict(os.environ)
    environ.pop("GSK_RENDERER", None)  # the launch decides, as for a real start
    environ["GDK_DISABLE"] = "gl,vulkan"

    finished = subprocess.run(
        [sys.executable, "-c", CHILD],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
    report = json.loads(finished.stdout.strip().splitlines()[-1])
    assert report["variable"] == variable, "on sets nothing; off asks for cairo"
    assert report["mapped"] is True, finished.stderr
    assert "Cairo" in (report["renderer"] or ""), report
