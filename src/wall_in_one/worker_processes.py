"""Cancellable ownership for short-lived worker subprocesses.

``ThreadPoolExecutor.shutdown(wait=False)`` does not stop a running worker and
Python joins every executor thread at interpreter exit.  A thumbnail ffmpeg or
scene capture therefore needs an owner below the future: this object records
the actual child process and kills its process group when the owning UI surface
shuts down.

The cancellation is intentionally permanent.  A loader owns one instance for
its lifetime, and work submitted after that loader has closed must not race a
new child into existence.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from typing import Final

# A normal completion is noticed promptly even without cancellation.  More
# importantly, this is the fallback cancellation latency if a test double does
# not react to the process-group signal like a real kernel child does.
POLL_SECONDS: Final = 0.1
TERMINATE_GRACE_SECONDS: Final = 0.5


class ProcessCancelledError(Exception):
    """The owner stopped while a child was queued or running."""


def _signal_group(process: subprocess.Popen[bytes], requested: signal.Signals) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, requested)


def _collect_after_signal(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Reap a signalled child without introducing another unbounded wait."""
    try:
        return process.communicate(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        try:
            return process.communicate(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # A descendant which deliberately escaped the session can retain
            # a pipe, but it must not retain this worker.  Close our pipe ends;
            # the direct child has already received SIGKILL.
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            # Closing retained pipes lets us reap the direct child even when
            # an escaped descendant kept its duplicate descriptors open.
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=TERMINATE_GRACE_SECONDS)
            return b"", b""


class Cancellation:
    """Own and cooperatively cancel a bounded set of child process groups."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._active: dict[int, subprocess.Popen[bytes]] = {}

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def register(self, process: subprocess.Popen[bytes]) -> bool:
        """Track ``process`` unless shutdown won the spawn/register race."""
        with self._lock:
            if self._cancelled.is_set():
                accepted = False
            else:
                self._active[process.pid] = process
                accepted = True
        if not accepted:
            _signal_group(process, signal.SIGKILL)
        return accepted

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._active.pop(process.pid, None)

    def cancel(self) -> None:
        """Refuse new children and wake every currently running child."""
        self._cancelled.set()
        with self._lock:
            active = tuple(self._active.values())
        for process in active:
            _signal_group(process, signal.SIGKILL)

    def run(
        self,
        arguments: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one captured command, retaining normal timeout semantics.

        ``timeout`` remains the full command deadline.  The short polling
        interval is solely for observing owner cancellation and does not turn
        a temporarily quiet child into a timeout.
        """
        command = list(arguments)
        if self.cancelled():
            raise ProcessCancelledError("worker subprocess was cancelled")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if not self.register(process):
            _collect_after_signal(process)
            raise ProcessCancelledError("worker subprocess was cancelled")

        deadline = time.monotonic() + timeout
        pending_input = input
        try:
            while True:
                if self.cancelled():
                    _signal_group(process, signal.SIGKILL)
                    _collect_after_signal(process)
                    raise ProcessCancelledError("worker subprocess was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _signal_group(process, signal.SIGTERM)
                    stdout, stderr = _collect_after_signal(process)
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout,
                        output=stdout,
                        stderr=stderr,
                    )
                try:
                    stdout, stderr = process.communicate(
                        input=pending_input,
                        timeout=min(POLL_SECONDS, remaining),
                    )
                except subprocess.TimeoutExpired:
                    # ``communicate`` retains a partially-written input buffer
                    # and permits a follow-up call as long as input is not
                    # supplied twice.
                    pending_input = None
                    continue
                if self.cancelled():
                    raise ProcessCancelledError("worker subprocess was cancelled")
                return subprocess.CompletedProcess(
                    command,
                    process.returncode,
                    stdout,
                    stderr,
                )
        finally:
            self.unregister(process)
