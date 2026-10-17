"""Bounded waits on GLib's default main context, for the GUI tests.

``while context.pending(): context.iteration(False)`` reads as "drain what is
queued", but it is not bounded. ``pending()`` stays true for as long as anything
animates with frames that take longer than the refresh interval, because GTK
queues the next frame before the last one is done. Under a software Vulkan
renderer a tile spinner did exactly that and the GUI suite hung at the first
test that drained while one was turning.

So every wait here checks its deadline inside the ``pending()`` loop as well as
around it, and a wait for a condition fails loudly, naming the condition,
rather than hanging or carrying on as if it held.

A wait's two-second bound is for a delivery: a result that is ready, reaching
the main loop. Two things that are not deliveries get their own bound, each
on its own condition, so that their time is not counted against it:
`first_frame` (GTK's own first layout of a window, `FIRST_FRAME_S`) and work
on a worker that touches real storage (`REAL_IO_S`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from gi.repository import GLib

#: The longest one drain runs before the condition is looked at again. Without
#: it, a main loop that never goes idle would keep a condition that already
#: holds waiting for the whole timeout.
_SLICE = 0.05
#: Between passes, so a wait on a worker thread does not spin a core.
_NAP = 0.002

#: A wait that covers real storage on a worker (a scan, a save with its
#: fsyncs, a move into an archive) is bounded by this, on the worker's own
#: condition where the test can see one, and not by the bound a delivery gets:
#: on a loaded machine that work alone runs for seconds.
REAL_IO_S = 30.0
#: GTK lays out and paints a window's first frame in one main-loop dispatch,
#: and nothing queued for the main loop is delivered until it returns. In a
#: fresh process that frame is the styles, the fonts and the first layout: it
#: measured 0.6 to 2.3 s for a settings page and 2.3 to 4.4 s for the whole
#: window on a loaded machine. It is GTK's own work, not a delivery, so it gets
#: this bound (`first_frame`) before any delivery is timed.
FIRST_FRAME_S = 30.0


class _Framed(Protocol):
    def get_frame_clock(self) -> Any: ...


class _Window(Protocol):
    def get_surface(self) -> Any: ...
    def realize(self) -> None: ...
    def destroy(self) -> None: ...


def destroy_window(window: _Window) -> None:
    """Destroy a window a test built, whether or not it was ever shown.

    GTK 4.22 on Wayland crashes the process when a window that belongs to an
    application is destroyed before it was ever realized: removing it from
    the application's session dereferences the surface it never had
    (``gtk_application_window_removed``, then
    ``gdk_wayland_toplevel_remove_from_session``). X11 let that pass, and
    many tests build a window only to drive its methods. Wall-in-One itself
    presents every window in the activation that builds it, so it never
    destroys one unrealized. Realizing first gives the window its surface
    without showing it; it is cleanup, after everything a test asserts.
    """
    if window.get_surface() is None:
        window.realize()
    window.destroy()


def first_frame(window: _Framed) -> Callable[[], bool]:
    """A check that is true once ``window`` has painted its first frame.

    Ask for it once the window is presented, from outside a frame: right
    after ``present()``, or in a scenario's first step. Then wait for it with
    `FIRST_FRAME_S` before the first wait that times a delivery::

        painted = first_frame(window)
        spin_until(painted, FIRST_FRAME_S, what="the window's first frame")

    A window whose first frame has already gone by answers true at once (its
    frame clock counts frames from -1), so asking late is not a wait.
    """
    clock = window.get_frame_clock()
    assert clock is not None, "the window has no frame clock yet: present it first"
    if clock.get_frame_counter() >= 0:
        return lambda: True
    painted = False

    def after_paint(_clock: object) -> None:
        nonlocal painted
        painted = True
        clock.disconnect(handler)

    handler = clock.connect("after-paint", after_paint)
    return lambda: painted


def describe(predicate: Callable[[], object]) -> str:
    """Name a condition well enough to find it from a failure message."""
    name = getattr(predicate, "__qualname__", None) or repr(predicate)
    code = getattr(predicate, "__code__", None)
    if code is None:
        return name
    return f"{name} ({Path(code.co_filename).name}:{code.co_firstlineno})"


def spin_until(
    predicate: Callable[[], object],
    timeout: float = 2.0,
    *,
    what: str | None = None,
) -> None:
    """Run GLib until ``predicate()`` is true; fail after ``timeout`` seconds.

    Each pass drains what is pending (for at most `_SLICE`) and then checks
    again, so under a renderer that keeps up this behaves exactly like the
    unbounded drain it replaces. ``what`` overrides the description in the
    failure, which otherwise comes from the predicate's name and source line.
    """
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate():
        now = time.monotonic()
        if now >= deadline:
            condition = what if what is not None else describe(predicate)
            raise AssertionError(f"GLib did not deliver within {timeout:g}s: {condition}")
        slice_end = min(deadline, now + _SLICE)
        while context.pending():
            if time.monotonic() >= slice_end:
                break
            context.iteration(False)
        time.sleep(_NAP)


def settle(seconds: float) -> None:
    """Run GLib for ``seconds``: allocation, activation animations, idles.

    It runs the whole time, idle or not, because what it waits out (a button's
    activation animation, an allocation that needs a frame) is often not
    pending yet when it starts. There is no condition to fail on: a test that
    needs something to have happened asserts it afterwards, or uses
    `spin_until`.
    """
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            if time.monotonic() >= deadline:
                return
            context.iteration(False)
        time.sleep(_NAP)
