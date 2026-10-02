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
from dataclasses import dataclass, field
from typing import Any, Final

# A normal completion is noticed promptly even without cancellation.  More
# importantly, this is the fallback cancellation latency if a test double does
# not react to the process-group signal like a real kernel child does.
POLL_SECONDS: Final = 0.1
TERMINATE_GRACE_SECONDS: Final = 0.5


class ProcessCancelledError(Exception):
    """The owner stopped while a child was queued or running."""


@dataclass(slots=True, eq=False)
class OwnedProcess:
    """One owned child and the lock that keeps its group id ours.

    Every call that can reap the leader (``poll``, ``wait``, ``communicate``)
    goes through these methods, which hold ``lock``, and so does the
    check-and-signal in :meth:`signal_group`. The leader therefore can't be
    reaped between seeing it unreaped and signalling its group, whichever
    thread does which.

    A pidfd would not do instead: ``pidfd_send_signal`` reaches the leader
    only, not its group, and holding a pidfd doesn't keep the numeric id
    reserved once the leader is reaped. The unreaped leader is the reservation,
    so reaping and signalling must not overlap.
    """

    process: subprocess.Popen[bytes]
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def pid(self) -> int:
        return self.process.pid

    def signal_group(self, requested: signal.Signals) -> None:
        """Signal the child's whole process group, even if its leader has exited.

        The group is the child's own (``start_new_session``), so its id is the
        leader's pid. A leader that has exited but has not been reaped is a
        zombie that still holds that pid, and Linux never hands out a pid while
        a zombie, or any live group member, still uses it as a pid or group id.
        So while the leader is unreaped, the group id can only name this
        child's group, and ``killpg`` reaches every member still in it,
        including descendants that outlived the leader and keep its pipes open.

        The check and the signal happen under ``lock``, which every reap also
        holds, so another thread can't reap the leader in between: a canceller
        delayed after the check still signals a group this module owns. Once
        the leader has been reaped (``returncode`` set) the id may belong to
        someone else, and the group is left alone.
        """
        with self.lock:
            if self.process.returncode is not None:
                return
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(self.process.pid, requested)

    def poll(self) -> int | None:
        with self.lock:
            return self.process.poll()

    def wait(self, timeout: float) -> int:
        with self.lock:
            return self.process.wait(timeout=timeout)

    def communicate(self, **keywords: Any) -> tuple[bytes, bytes]:
        with self.lock:
            return self.process.communicate(**keywords)


def _signal_group(child: OwnedProcess, requested: signal.Signals) -> None:
    child.signal_group(requested)


def _collect_after_signal(child: OwnedProcess) -> tuple[bytes, bytes]:
    """Reap a signalled child without introducing another unbounded wait."""
    process = child.process
    try:
        return child.communicate(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(child, signal.SIGKILL)
        try:
            return child.communicate(timeout=TERMINATE_GRACE_SECONDS)
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
                child.wait(TERMINATE_GRACE_SECONDS)
            return b"", b""


class Cancellation:
    """Own and cooperatively cancel a bounded set of child process groups."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._active: dict[int, OwnedProcess] = {}

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def register(self, child: OwnedProcess) -> bool:
        """Track ``child`` unless shutdown won the spawn/register race."""
        with self._lock:
            if self._cancelled.is_set():
                accepted = False
            else:
                self._active[child.process.pid] = child
                accepted = True
        if not accepted:
            _signal_group(child, signal.SIGKILL)
        return accepted

    def unregister(self, child: OwnedProcess) -> None:
        with self._lock:
            self._active.pop(child.process.pid, None)

    def cancel(self) -> None:
        """Refuse new children and wake every currently running child."""
        self._cancelled.set()
        with self._lock:
            active = tuple(self._active.values())
        for child in active:
            _signal_group(child, signal.SIGKILL)

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
        child = OwnedProcess(process)
        if not self.register(child):
            _collect_after_signal(child)
            raise ProcessCancelledError("worker subprocess was cancelled")

        deadline = time.monotonic() + timeout
        pending_input = input
        try:
            while True:
                if self.cancelled():
                    _signal_group(child, signal.SIGKILL)
                    _collect_after_signal(child)
                    raise ProcessCancelledError("worker subprocess was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _signal_group(child, signal.SIGTERM)
                    stdout, stderr = _collect_after_signal(child)
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout,
                        output=stdout,
                        stderr=stderr,
                    )
                try:
                    stdout, stderr = child.communicate(
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
            self.unregister(child)
