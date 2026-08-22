"""One shared subinterpreter pool for measured pure-Python backend work.

The GTK main interpreter must stay responsive while providers turn untrusted
documents into bounded value objects. CPython 3.14's interpreter executor gives
each worker its own GIL, but each interpreter costs real memory; this is
therefore the only factory in the application.

Network and subprocess waits do not belong here. They already release the GIL,
and moving live clients or process objects across this boundary would be both
slower and unsafe. Callers submit importable module-level functions over
picklable data and must always observe ``Future.result()``.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, InterpreterPoolExecutor

# Four was measured to keep GTK's p95 frame latency below 16 ms without
# multiplying the roughly 12.7 MiB interpreter cost without bound. A smaller
# machine should not create more workers than CPUs it can actually schedule.
MAX_WORKERS = max(1, min(4, os.process_cpu_count() or 1))

_lock = threading.Lock()
_executor: InterpreterPoolExecutor | None = None


def executor() -> InterpreterPoolExecutor:
    """Return the process-wide backend executor, constructing it lazily."""
    global _executor
    with _lock:
        if _executor is None:
            _executor = InterpreterPoolExecutor(
                max_workers=MAX_WORKERS,
                thread_name_prefix="wall-in-one-backend",
            )
        return _executor


def submit[**P, R](function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> Future[R]:
    """Submit one importable worker function; callers must inspect its result."""
    return executor().submit(function, *args, **kwargs)


def run[**P, R](function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run CPU work in a subinterpreter and propagate every failure."""
    return submit(function, *args, **kwargs).result()


def shutdown(*, wait: bool = True) -> None:
    """Release the shared pool, primarily for deterministic test teardown."""
    global _executor
    with _lock:
        current, _executor = _executor, None
    if current is not None:
        current.shutdown(wait=wait, cancel_futures=True)
