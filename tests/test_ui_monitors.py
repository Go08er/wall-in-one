"""The window's own view of the screens, read from the run's own compositor.

Every GUI run draws on a private headless Weston (``tools/headless-wayland.sh``:
one 1600x1200 output at scale 1). GTK's monitors are what the window falls
back to when no wallpaper service answers (``docs/display-discovery.md``);
these pin that they read back as an output a scene still can be sized for,
on a compositor that is not niri.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from wall_in_one.ui import monitors  # noqa: E402
from wall_in_one.wallpaper import outputs, scenes  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    Gtk.init()


def test_the_test_compositors_monitor_reads_back_as_an_output() -> None:
    found = monitors.snapshot()

    assert len(found) == 1, found
    screen = found[0]
    assert screen.name, "GTK names the connector"
    assert (screen.width, screen.height) == (1600, 1200)
    assert screen.scale == 1.0
    assert (screen.physical_width, screen.physical_height) == (1600, 1200)
    assert screen.enabled


def test_without_a_service_a_scene_still_is_sized_for_that_monitor() -> None:
    assert outputs.ask_service() is None, "no wallpaper service runs in a test"

    found = outputs.discover(monitors.snapshot())

    assert scenes.measured_capture_size("", found) == (1600, 1200)
