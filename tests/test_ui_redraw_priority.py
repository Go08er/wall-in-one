"""Worker completions that end a busy state must not wait below GTK's redraw.

A plain ``GLib.idle_add`` runs at priority 200, below GTK's redraw at 120.
When frames cost more than a refresh interval (a spinner under software
rendering, a VM, Xvfb), GTK paints back to back and such an idle never runs
until the animation stops -- and the animation is often the very spinner the
completion would stop. These tests reproduce that window with a real GTK
window, an ``Adw.Spinner`` and a tick callback that spends about 25 ms per
frame, then hand the production completion paths a finished worker result and
require it to land while the frames keep coming.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library.model import Library  # noqa: E402
from wall_in_one.session import LibraryRefreshPlan  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402

#: The review's reproduction: about 25 ms of work in every frame.
FRAME_COST_SECONDS = 0.025
#: How long a completion may take to land while frames are expensive. The
#: review pumped 0.65 s and saw neither completion; at the right priority
#: both land within a frame or two.
LANDING_SECONDS = 0.65


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def application() -> Iterator[Application]:
    instance = Application()
    yield instance
    instance._shutdown_authoring_jobs()
    if instance._runtime_jobs is not None:
        instance._runtime_jobs.shutdown(wait=True)
    instance._shutdown_library_scan_jobs(wait=True)
    instance._shutdown_theme_jobs(wait=True)
    instance._stills.shutdown()
    instance._session.shutdown()


class _Frames:
    """An animating window whose every frame is expensive."""

    def __init__(self) -> None:
        self.count = 0
        self.window = Gtk.Window()
        spinner = Adw.Spinner()
        spinner.set_size_request(64, 64)
        self.window.set_child(spinner)
        self.window.present()
        self._tick = self.window.add_tick_callback(self._expensive_frame)

    def _expensive_frame(self, _widget: Gtk.Widget, _clock: Gdk.FrameClock) -> bool:
        self.count += 1
        deadline = time.monotonic() + FRAME_COST_SECONDS
        while time.monotonic() < deadline:
            pass
        return GLib.SOURCE_CONTINUE

    def keep_coming(self) -> bool:
        """Whether GTK is still painting expensive frames after the delivery.

        A completion at the right priority can land before the next frame, so
        the count at landing proves nothing by itself; what matters is that
        the frames were coming before and still are after.
        """
        landed_at = self.count
        return _pump_until(lambda: self.count > landed_at, 1.0)

    def close(self) -> None:
        self.window.remove_tick_callback(self._tick)
        self.window.destroy()


@pytest.fixture
def frames() -> Iterator[_Frames]:
    animating = _Frames()
    try:
        # Warm up until GTK really is painting back to back.
        assert _pump_until(lambda: animating.count >= 5, 2.0), "the window never animated"
        yield animating
    finally:
        animating.close()


def _pump_until(predicate: Callable[[], bool], seconds: float) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        context.iteration(False)
    return predicate()


def test_a_finished_library_scan_is_reconciled_while_frames_are_expensive(
    application: Application, frames: _Frames, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reconciled: list[Library] = []

    def queue(generation: int, request: LibraryRefreshPlan, library: Library) -> None:
        del generation, request
        reconciled.append(library)

    monkeypatch.setattr(application, "_queue_library_reconciliation", queue)
    library = Library(roots=(tmp_path,), items=())
    future: Future[Library] = Future()
    future.set_result(library)
    request = application._session.prepare_library_refresh()
    before = frames.count

    application._post_library_scan(application._library_scan_generation, request, future)

    assert _pump_until(lambda: bool(reconciled), LANDING_SECONDS), (
        f"the scan result waited below redraw through {frames.count - before} frames"
    )
    assert reconciled == [library]
    assert frames.keep_coming(), "the window stopped animating, so this proved nothing"


def test_an_authoring_task_completes_and_the_next_one_starts_while_frames_are_expensive(
    application: Application, frames: _Frames
) -> None:
    adopted: list[int] = []
    before = frames.count

    for value in (42, 43):
        assert application.authoring_action_async(
            lambda value=value: value,  # type: ignore[misc]
            adopted.append,
            requires_migration=False,
            guard_migration_transaction=False,
        )

    assert _pump_until(lambda: adopted == [42, 43], LANDING_SECONDS), (
        f"adopted {adopted} through {frames.count - before} frames; "
        f"active={application._authoring_active}"
    )
    assert not application._authoring_active
    assert not application._authoring_queue
    assert frames.keep_coming(), "the window stopped animating, so this proved nothing"


def test_an_authoring_reply_chained_through_the_runtime_lane_lands_while_frames_are_expensive(
    application: Application, frames: _Frames
) -> None:
    """The authoring lane is held until a chained Deferred replies, so that reply
    (here from the ordered runtime worker) must not starve either."""
    replies: list[Response] = []
    adopted: list[int] = []
    before = frames.count

    deferred = application.authoring_off_thread(
        lambda: None,
        lambda _nothing: application.runtime_off_thread(lambda: Response.success("runtime")),
        requires_migration=False,
        guard_migration_transaction=False,
        queue_if_busy=True,
    )
    deferred.start(replies.append)
    assert application.authoring_action_async(
        lambda: 7,
        adopted.append,
        requires_migration=False,
        guard_migration_transaction=False,
    )

    assert _pump_until(lambda: bool(replies) and adopted == [7], LANDING_SECONDS), (
        f"replies {replies}, adopted {adopted} through {frames.count - before} frames"
    )
    assert [reply.message for reply in replies] == ["runtime"]
    assert not application._authoring_active
    assert frames.keep_coming(), "the window stopped animating, so this proved nothing"
