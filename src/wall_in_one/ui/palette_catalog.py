"""One bounded, asynchronous owner for the installed-palette catalogue.

Palette discovery walks several user-controlled directories and parses every
accepted JSON document.  GTK callers consume immutable snapshots from this
object instead of doing that work in menu construction or a page refresh.

There is deliberately one worker.  More workers would race the same small
directories, increase peak memory, and make a just-saved palette easier to
replace with an older result.  Generations and cooperative cancellation fold
refresh bursts into the newest request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from typing import Final, Literal

from gi.repository import GLib

from wall_in_one.theme import palettes

CatalogPhase = Literal["idle", "loading", "ready", "error"]
CatalogCallback = Callable[["CatalogState"], None]
Discover = Callable[[palettes.CancelCheck], palettes.Discovery]
Dispatcher = Callable[[Callable[[], bool]], object]

_EMPTY: Final = palettes.Discovery(())
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CatalogState:
    """One immutable, generation-owned view of palette discovery."""

    generation: int = 0
    phase: CatalogPhase = "idle"
    discovery: palettes.Discovery = _EMPTY
    error: str = ""

    @property
    def loading(self) -> bool:
        return self.phase == "loading"

    @property
    def usable(self) -> bool:
        return bool(self.discovery.entries) and self.phase in {"loading", "ready"}


@dataclass(frozen=True, slots=True)
class _Outcome:
    generation: int
    discovery: palettes.Discovery | None
    error: str = ""


def _discover(cancelled: palettes.CancelCheck) -> palettes.Discovery:
    return palettes.discover(cancelled=cancelled)


class PaletteCatalog:
    """Discover palettes off-thread and publish only the newest generation.

    Public methods are intended for the GTK thread.  Worker completion is
    marshalled through ``dispatcher`` (``GLib.idle_add`` in production), so a
    subscriber never has to defend its widgets from a pool thread.
    """

    def __init__(
        self,
        *,
        discover: Discover = _discover,
        dispatcher: Dispatcher = GLib.idle_add,
        initial: palettes.Discovery | None = None,
    ) -> None:
        self._discover = discover
        self._dispatch = dispatcher
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="palette-catalog")
        self._state = CatalogState(
            phase="ready" if initial is not None else "idle",
            discovery=initial if initial is not None else _EMPTY,
        )
        self._requested_generation = 0
        self._future: Future[palettes.Discovery] | None = None
        self._active_generation = 0
        self._cancel = Event()
        self._listeners: dict[int, CatalogCallback] = {}
        self._next_listener = 1
        self._closed = False

    @property
    def state(self) -> CatalogState:
        return self._state

    def subscribe(self, callback: CatalogCallback, *, replay: bool = True) -> int:
        """Return an opaque listener id which may later be unsubscribed."""
        if self._closed:
            return 0
        listener = self._next_listener
        self._next_listener += 1
        self._listeners[listener] = callback
        if replay:
            try:
                callback(self._state)
            except Exception:
                _LOG.exception("palette catalogue subscriber failed during replay")
        return listener

    def unsubscribe(self, listener: int) -> None:
        self._listeners.pop(listener, None)

    def ensure_loaded(self) -> None:
        """Start the first pass, without turning an existing pass into churn."""
        if self._state.phase == "idle":
            self.refresh()

    def refresh(self, *, invalidate: bool = False) -> int:
        """Queue the newest discovery pass and return its generation.

        A normal rescan keeps the last-good catalogue usable while an honest
        "Refreshing" indicator is shown.  A known filesystem mutation uses
        ``invalidate=True`` so an edited or removed entry is not actionable
        until the post-write snapshot lands.
        """
        if self._closed:
            return self._requested_generation
        self._requested_generation += 1
        generation = self._requested_generation
        discovery = _EMPTY if invalidate else self._state.discovery
        self._state = CatalogState(generation, "loading", discovery)
        self._notify()
        if self._future is None:
            self._start(generation)
        else:
            # The running result is now ineligible.  Encourage it to stop at
            # the next directory/file boundary so the newest pass starts soon.
            self._cancel.set()
        return generation

    def invalidate(self) -> int:
        """Forget a known-stale snapshot and discover the post-write state."""
        return self.refresh(invalidate=True)

    def _start(self, generation: int) -> None:
        if self._closed:
            return
        self._active_generation = generation
        self._cancel = Event()
        future = self._pool.submit(self._discover, self._cancel.is_set)
        self._future = future
        future.add_done_callback(lambda done: self._post(generation, done))

    def _post(self, generation: int, future: Future[palettes.Discovery]) -> None:
        try:
            outcome = _Outcome(generation, future.result())
        except Exception as error:
            # Filesystem and palette errors are normally represented in
            # Discovery.skipped.  This catches an unexpected worker failure and
            # exposes it instead of leaving the UI spinning forever.
            outcome = _Outcome(generation, None, str(error) or type(error).__name__)
        if self._closed:
            return

        def deliver() -> bool:
            self._finish(future, outcome)
            return GLib.SOURCE_REMOVE

        try:
            self._dispatch(deliver)
        except RuntimeError:
            # GLib may already be torn down during interpreter/window exit.
            return

    def _finish(self, future: Future[palettes.Discovery], outcome: _Outcome) -> None:
        if self._closed or self._future is not future:
            return
        self._future = None
        if outcome.generation != self._requested_generation:
            self._start(self._requested_generation)
            return
        if outcome.discovery is None:
            self._state = CatalogState(
                outcome.generation,
                "error",
                _EMPTY,
                outcome.error or "palette discovery stopped unexpectedly",
            )
        else:
            self._state = CatalogState(outcome.generation, "ready", outcome.discovery)
        self._notify()

    def _notify(self) -> None:
        state = self._state
        for callback in tuple(self._listeners.values()):
            try:
                callback(state)
            except Exception:
                # One damaged surface must not strand every other subscriber
                # in Loading or prevent the filesystem pass from starting.
                _LOG.exception("palette catalogue subscriber failed")

    def shutdown(self) -> None:
        """Cancel queued work and cooperatively stop the one active pass."""
        if self._closed:
            return
        self._closed = True
        self._requested_generation += 1
        self._listeners.clear()
        self._cancel.set()
        if self._future is not None:
            self._future.cancel()
            self._future = None
        self._pool.shutdown(wait=False, cancel_futures=True)
