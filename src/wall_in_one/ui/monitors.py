"""The window's own view of the screens: GTK's monitors.

The wallpaper service's display snapshot is the source of truth
(`wallpaper.outputs.discover`). This is the fallback for when no service
answers: GTK knows the monitors it draws on from the same compositor, on any
compositor. GTK may only be touched on the main thread, so a job that needs
displays takes this snapshot there and hands it to its worker.
"""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")

from gi.repository import Gdk

from wall_in_one.wallpaper import outputs


def snapshot() -> tuple[outputs.Output, ...]:
    """GTK's monitors right now, as outputs; empty without a display.

    Main thread only.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return ()
    model = display.get_monitors()
    return outputs.from_monitors(model.get_item(index) for index in range(model.get_n_items()))
