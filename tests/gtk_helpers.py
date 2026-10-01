"""Bounded waits on GLib's default main context, for the GUI tests.

``while context.pending(): context.iteration(False)`` reads as "drain what is
queued", but it is not bounded. ``pending()`` stays true for as long as anything
animates with frames that take longer than the refresh interval, because GTK
queues the next frame before the last one is done. Under the Vulkan renderer on
Xvfb a tile spinner did exactly that and the GUI suite hung at the first test
that drained while one was turning.

So every wait here checks its deadline inside the ``pending()`` loop as well as
around it, and a wait for a condition fails loudly, naming the condition,
rather than hanging or carrying on as if it held.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from gi.repository import GLib

#: The longest one drain runs before the condition is looked at again. Without
#: it, a main loop that never goes idle would keep a condition that already
#: holds waiting for the whole timeout.
_SLICE = 0.05
#: Between passes, so a wait on a worker thread does not spin a core.
_NAP = 0.002


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
