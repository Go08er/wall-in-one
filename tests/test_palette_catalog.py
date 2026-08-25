"""The palette catalogue's single-worker, newest-generation contract."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from wall_in_one.theme import palettes
from wall_in_one.ui.palette_catalog import CatalogState, PaletteCatalog


class Dispatcher:
    """A deterministic stand-in for ``GLib.idle_add``."""

    def __init__(self) -> None:
        self.pending: queue.Queue[Callable[[], bool]] = queue.Queue()

    def __call__(self, callback: Callable[[], bool]) -> object:
        self.pending.put(callback)
        return object()

    def deliver(self) -> None:
        self.pending.get(timeout=2)()


def _discovery(name: str) -> palettes.Discovery:
    return palettes.Discovery((palettes.PaletteEntry(name, palettes.Origin.BUILTIN, None, None),))


def test_discovery_runs_off_the_caller_and_publishes_on_the_dispatcher() -> None:
    dispatcher = Dispatcher()
    caller = threading.get_ident()
    worker: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def discover(_cancelled: palettes.CancelCheck) -> palettes.Discovery:
        worker.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return _discovery("new")

    catalog = PaletteCatalog(discover=discover, dispatcher=dispatcher)
    observed: list[CatalogState] = []
    catalog.subscribe(observed.append)

    catalog.refresh()

    assert started.wait(2)
    assert worker == [worker[0]] and worker[0] != caller
    assert catalog.state.phase == "loading"
    assert [state.phase for state in observed] == ["idle", "loading"]

    release.set()
    dispatcher.deliver()
    current = catalog.state

    assert current.phase == "ready"
    assert current.discovery.entries[0].name == "new"
    assert [state.phase for state in observed] == ["idle", "loading", "ready"]
    catalog.shutdown()


def test_invalidating_a_running_pass_discards_it_and_publishes_only_the_newest() -> None:
    dispatcher = Dispatcher()
    first_started = threading.Event()
    calls = 0

    def discover(cancelled: palettes.CancelCheck) -> palettes.Discovery:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            assert threading.Event().wait(0.001) is False
            while not cancelled():
                threading.Event().wait(0.001)
            return _discovery("stale")
        return _discovery("current")

    catalog = PaletteCatalog(discover=discover, dispatcher=dispatcher)
    ready_names: list[str] = []
    catalog.subscribe(
        lambda state: (
            ready_names.append(state.discovery.entries[0].name) if state.phase == "ready" else None
        )
    )
    first = catalog.refresh()
    assert first_started.wait(2)
    second = catalog.invalidate()
    assert second > first
    assert catalog.state.discovery.entries == ()

    # The stale completion only starts the coalesced newest pass.
    dispatcher.deliver()
    dispatcher.deliver()

    assert calls == 2
    assert ready_names == ["current"]
    assert catalog.state.generation == second
    catalog.shutdown()


def test_unexpected_worker_error_becomes_a_retryable_error_state() -> None:
    dispatcher = Dispatcher()

    def discover(_cancelled: palettes.CancelCheck) -> palettes.Discovery:
        raise RuntimeError("catalogue exploded")

    catalog = PaletteCatalog(discover=discover, dispatcher=dispatcher)
    catalog.refresh()
    dispatcher.deliver()

    assert catalog.state.phase == "error"
    assert catalog.state.discovery.entries == ()
    assert catalog.state.error == "catalogue exploded"
    catalog.shutdown()


def test_shutdown_cooperatively_stops_the_active_pass_and_suppresses_delivery() -> None:
    dispatcher = Dispatcher()
    started = threading.Event()
    stopped = threading.Event()

    def discover(cancelled: palettes.CancelCheck) -> palettes.Discovery:
        started.set()
        while not cancelled():
            stopped.wait(0.001)
        stopped.set()
        return _discovery("must not publish")

    catalog = PaletteCatalog(discover=discover, dispatcher=dispatcher)
    catalog.refresh()
    assert started.wait(2)

    catalog.shutdown()

    assert stopped.wait(0.5)
    assert dispatcher.pending.empty()
