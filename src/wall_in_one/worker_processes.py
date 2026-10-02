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
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, NoReturn

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

    def exited(self) -> bool:
        """Whether the leader has exited, observed without reaping it.

        ``waitid(..., WNOWAIT)`` reports an exit but leaves the leader a
        zombie, and that zombie keeps its pid, and so the group id, reserved.
        A liveness check must use this rather than :meth:`poll`: reaping here
        would leave any descendant still in the group beyond reach, since a
        reaped group may no longer be signalled. :meth:`end_group` reaps.
        """
        with self.lock:
            if self.process.returncode is not None:
                return True
            try:
                found = os.waitid(
                    os.P_PID,
                    self.process.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except ChildProcessError:
                # Already reaped outside this owner: nothing left to signal.
                return True
            return found is not None

    def end_group(self, *, immediate: bool, grace: float) -> None:
        """Stop the whole group, and only then reap the leader.

        ``SIGTERM`` (or ``SIGKILL`` when ``immediate``) to the group; up to
        ``grace`` seconds for the leader to exit, observed without reaping;
        then ``SIGKILL`` to the group, which reaches every descendant still in
        it, those that outlived their leader included; then reap. Every signal
        is sent while the leader is unreaped (see :meth:`signal_group`), so the
        group id is still this child's.
        """
        with self.lock:
            self.signal_group(signal.SIGKILL if immediate else signal.SIGTERM)
            deadline = time.monotonic() + (0.0 if immediate else grace)
            while not self.exited() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.signal_group(signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=grace)

    def poll(self) -> int | None:
        """Reap the leader if it has exited. Not a liveness check: see :meth:`exited`."""
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


#: Read/write slice for :func:`exchange`.
_CHUNK: Final = 65536
#: How often the leader is checked once every pipe has closed.
_EXIT_POLL_SECONDS: Final = 0.005


def spawn(
    command: Sequence[str], *, stdin: int | None = None
) -> tuple[OwnedProcess, selectors.BaseSelector]:
    """Start ``command`` owned, in its own session, with its output piped.

    The selector :func:`exchange` will use is built first. It is the one
    descriptor the exchange needs beyond the pipes, so running out of
    descriptors fails here, before a child exists, rather than after, with a
    child to lose.
    """
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            list(command),
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except BaseException:
        selector.close()
        raise
    return OwnedProcess(process), selector


def release(owned: OwnedProcess, selector: selectors.BaseSelector | None = None) -> None:
    """End ``owned``'s group if that hasn't happened, then close what it holds.

    After a finished :meth:`OwnedProcess.end_group` the leader is reaped, so
    this signals nothing and returns at once; otherwise it kills the group
    and reaps. The selector and every pipe are closed even if that fails.
    """
    process = owned.process
    try:
        owned.end_group(immediate=True, grace=TERMINATE_GRACE_SECONDS)
    finally:
        if selector is not None:
            with contextlib.suppress(OSError):
                selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


def abandon(
    owned: OwnedProcess, selector: selectors.BaseSelector | None, error: BaseException
) -> NoReturn:
    """:func:`release` ``owned``, then raise ``error``, the reason for letting go.

    The caller must see that original error. If the release fails as well,
    its error is chained to the original (as ``__context__``) rather than
    raised in its place.
    """
    try:
        release(owned, selector)
    except BaseException:
        # Deliberately the original, not ``from``: raised in this handler, it
        # gets the release error as its implicit ``__context__`` and keeps
        # whatever ``__cause__`` it already had.
        raise error  # noqa: B904
    raise error


def exchange(
    owned: OwnedProcess,
    *,
    selector: selectors.BaseSelector | None = None,
    input: bytes | None = None,
    timeout: float,
    cancelled: Callable[[], bool] = lambda: False,
) -> subprocess.CompletedProcess[bytes]:
    """Feed and read an owned child, then end its whole group, then reap it.

    ``Popen.communicate`` reaps the leader as soon as its pipes close, after
    which its group may no longer be signalled: a descendant that dropped the
    pipes but stayed in the group would outlive the command. So this reads
    the pipes itself and treats the leader's exit, observed without reaping
    (:meth:`OwnedProcess.exited`), as completion: then
    :meth:`OwnedProcess.end_group` terminates whatever it left in its group
    and reaps it, and the output still buffered in the pipes is collected.

    ``timeout`` is the whole command's deadline: on expiry the group is ended
    (TERM, then KILL) and :class:`subprocess.TimeoutExpired` carries what was
    read. ``cancelled`` is checked at least every :data:`POLL_SECONDS`; when it
    is set the group is killed and :class:`ProcessCancelledError` raised. Every
    wait is bounded, and a descendant that escaped the session can't hold the
    caller: our pipe ends are closed regardless.

    Every way out ends the group first, an unexpected error or an interrupt
    included, so a caller that then lets go of ``owned`` lets go of nothing
    still running; the selector and pipes are closed and the original error
    is what propagates (see :func:`abandon`). ``selector``, when given, is
    this call's to close; :func:`spawn` builds it before the child.
    """
    process = owned.process
    chunks: dict[int, list[bytes]] = {}
    readers: dict[int, str] = {}
    pending = memoryview(input if input is not None else b"")
    written = 0
    writer: int | None = None
    try:
        if selector is None:
            selector = selectors.DefaultSelector()
        watched = selector
        for name in ("stdout", "stderr"):
            stream = getattr(process, name)
            if stream is not None:
                descriptor = stream.fileno()
                os.set_blocking(descriptor, False)
                watched.register(descriptor, selectors.EVENT_READ)
                chunks[descriptor] = []
                readers[descriptor] = name
        if process.stdin is not None:
            if len(pending):
                writer = process.stdin.fileno()
                os.set_blocking(writer, False)
                watched.register(writer, selectors.EVENT_WRITE)
            else:
                with contextlib.suppress(OSError):
                    process.stdin.close()

        def stop_writing() -> None:
            nonlocal writer
            if writer is not None:
                watched.unregister(writer)
                writer = None
                if process.stdin is not None:
                    with contextlib.suppress(OSError):
                        process.stdin.close()

        def pump(wait: float) -> None:
            nonlocal written
            if not watched.get_map():
                time.sleep(min(wait, _EXIT_POLL_SECONDS))
                return
            for key, _events in watched.select(wait):
                descriptor = key.fd
                if descriptor == writer:
                    try:
                        written += os.write(descriptor, pending[written : written + _CHUNK])
                    except BlockingIOError:
                        continue
                    except OSError:
                        stop_writing()  # The child stopped reading; its output still counts.
                        continue
                    if written >= len(pending):
                        stop_writing()
                    continue
                try:
                    data = os.read(descriptor, _CHUNK)
                except BlockingIOError:
                    continue
                if data:
                    chunks[descriptor].append(data)
                else:
                    watched.unregister(descriptor)

        def drain(seconds: float) -> None:
            stop_writing()
            end = time.monotonic() + seconds
            while watched.get_map() and (left := end - time.monotonic()) > 0:
                pump(min(POLL_SECONDS, left))

        deadline = time.monotonic() + timeout
        while True:
            if cancelled():
                owned.end_group(immediate=True, grace=TERMINATE_GRACE_SECONDS)
                raise ProcessCancelledError("worker subprocess was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                owned.end_group(immediate=False, grace=TERMINATE_GRACE_SECONDS)
                drain(TERMINATE_GRACE_SECONDS)
                raise subprocess.TimeoutExpired(
                    process.args,
                    timeout,
                    output=_joined(chunks, readers, "stdout"),
                    stderr=_joined(chunks, readers, "stderr"),
                )
            if owned.exited():
                break
            pump(min(POLL_SECONDS, remaining))
        # The leader has exited and is still unreaped, so its group id is
        # still ours: end anything it left there, then reap it.
        owned.end_group(immediate=False, grace=TERMINATE_GRACE_SECONDS)
        drain(TERMINATE_GRACE_SECONDS)
    except BaseException as error:
        abandon(owned, selector, error)
    release(owned, selector)
    returncode = process.returncode
    return subprocess.CompletedProcess(
        process.args,
        returncode if returncode is not None else -int(signal.SIGKILL),
        _joined(chunks, readers, "stdout"),
        _joined(chunks, readers, "stderr"),
    )


def _joined(chunks: dict[int, list[bytes]], readers: dict[int, str], name: str) -> bytes:
    return b"".join(
        b"".join(chunks[descriptor]) for descriptor, reader in readers.items() if reader == name
    )


def _already_cancelled() -> bool:
    return True


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
        a temporarily quiet child into a timeout. The command is done when its
        leader exits: whatever it left running in its process group is ended
        before the leader is reaped (see :func:`exchange`).
        """
        command = list(arguments)
        if self.cancelled():
            raise ProcessCancelledError("worker subprocess was cancelled")
        child, selector = spawn(
            command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL
        )
        try:
            registered = self.register(child)
        except BaseException as error:
            abandon(child, selector, error)
        try:
            # Refused registration (shutdown won the race) goes through the
            # same exit: the group is killed and everything closed.
            completed = exchange(
                child,
                selector=selector,
                input=input,
                timeout=timeout,
                cancelled=self.cancelled if registered else _already_cancelled,
            )
            completed.args = command
            if self.cancelled():
                raise ProcessCancelledError("worker subprocess was cancelled")
            return completed
        finally:
            # Only now: exchange() has ended the group on every way out.
            if registered:
                self.unregister(child)
