"""Sweep 5 U-1: no way out of an exchange lets go of a live process group.

Both owners of the shared exchange, ``Cancellation.run`` and the Noctalia CLI
wrapper, run a stand-in that starts a sleeper in its own process group (so the
sleeper holds the output pipes) and records both pids. Each case then makes the
exchange end unexpectedly: descriptor exhaustion right after the spawn, a
selector that can't be built or can't watch the pipes, an ``EIO`` from a pipe
read, or a ``KeyboardInterrupt`` in the loop. Before the fix those exits closed
the pipes but never ended the group, and the owner then dropped the child from
its registry, so not even a later shutdown could stop it.

Every case checks the original error propagates, the leader was reaped by its
owner (which only reaps after signalling the group), the sleeper was killed,
the registry let go of the child only once its leader was reaped, and the
selector and pipes were closed. Only the stand-in's own pids are ever signalled
or reaped here.

Sweep 5 V-1 adds a real SIGINT delivered while that cleanup runs, and a
cleanup that can't finish at all: the owner must then keep the child, so a
later cancellation still ends it.
"""

from __future__ import annotations

import contextlib
import errno
import linecache
import os
import resource
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path
from types import CodeType, FrameType
from typing import Any

import pytest

from tests.process_helpers import handed_off, is_alive, kill_and_reap, reaped, subreaper
from wall_in_one import worker_processes
from wall_in_one.theme import noctalia
from wall_in_one.worker_processes import OwnedProcess

FAULTS = ("fd-pressure", "no-selector", "unwatchable", "read-eio", "interrupt")


def _stand_in(tmp_path: Path) -> Path:
    """``<script> msg <linger|exit> <handoff>``, the Noctalia CLI's argv shape."""
    script = tmp_path / "stand-in"
    script.write_text(
        "#!/bin/sh\n"
        # Same group, and it inherits, so holds, the output pipes.
        "sleep 120 &\n"
        'echo "$$ $!" > "$3.tmp"\n'
        'mv "$3.tmp" "$3"\n'
        "printf 'ready\\n'\n"
        '[ "$2" = linger ] && exec sleep 30\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


class _Owner(worker_processes.Cancellation):
    """Records the leader's state each time the owner lets go of a child."""

    def __init__(self, interrupt: Callable[[], bool], released: list[int | None]) -> None:
        super().__init__()
        self._interrupt = interrupt
        self._released = released

    def cancelled(self) -> bool:
        return self._interrupt() or super().cancelled()

    def unregister(self, child: OwnedProcess) -> None:
        self._released.append(child.process.returncode)
        super().unregister(child)


class _Registry(MutableMapping[int, OwnedProcess]):
    """Stands in for ``noctalia._ACTIVE`` and records the same."""

    def __init__(self, released: list[int | None]) -> None:
        self._children: dict[int, OwnedProcess] = {}
        self._released = released

    def __getitem__(self, pid: int) -> OwnedProcess:
        return self._children[pid]

    def __setitem__(self, pid: int, child: OwnedProcess) -> None:
        self._children[pid] = child

    def __delitem__(self, pid: int) -> None:
        self._released.append(self._children[pid].process.returncode)
        del self._children[pid]

    def __iter__(self) -> Iterator[int]:
        return iter(self._children)

    def __len__(self) -> int:
        return len(self._children)


def _exhaust_descriptors() -> Callable[[], None]:
    """Use up every descriptor this process may open; return the undo.

    The soft limit drops to a few above what is open now, so filling it takes
    a handful of ``/dev/null`` opens. Nothing outside this process is touched.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (len(os.listdir("/proc/self/fd")) + 8, hard))
    fillers: list[int] = []
    with contextlib.suppress(OSError):
        while len(fillers) < 4096:
            fillers.append(os.open("/dev/null", os.O_RDONLY))

    def undo() -> None:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        for descriptor in fillers:
            os.close(descriptor)

    return undo


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 3.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


class _Harness:
    """One stand-in run through ``caller`` with ``fault`` armed; see the module."""

    def __init__(
        self, caller: str, fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = _stand_in(tmp_path)
        self.handoff = handoff = tmp_path / "handoff"
        mode = "exit" if fault == "fd-pressure" else "linger"
        self.spawned: list[subprocess.Popen[bytes]] = []
        self.selectors_made: list[selectors.EpollSelector] = []
        self.released: list[int | None] = []
        self._undo: list[Callable[[], None]] = []
        self._sleeper: int | None = None
        self._collected = False
        spawned, selectors_made, undo = self.spawned, self.selectors_made, self._undo
        real_read = os.read

        def arm(process: subprocess.Popen[bytes]) -> None:
            """Runs right after the real spawn returns, before the exchange."""
            if fault == "fd-pressure":
                undo.append(_exhaust_descriptors())
            elif fault == "read-eio":
                assert process.stdout is not None
                target = process.stdout.fileno()

                def failing_read(descriptor: int, length: int) -> bytes:
                    if descriptor == target:
                        raise OSError(errno.EIO, "injected read error")
                    return real_read(descriptor, length)

                monkeypatch.setattr(os, "read", failing_read)

        class ArmedPopen(subprocess.Popen[bytes]):
            def __init__(self, *arguments: Any, **options: Any) -> None:
                super().__init__(*arguments, **options)
                spawned.append(self)
                arm(self)

        class RecordedSelector(selectors.EpollSelector):
            def __init__(self) -> None:
                super().__init__()
                selectors_made.append(self)

        class UnwatchableSelector(RecordedSelector):
            def register(
                self, fileobj: Any, events: int, data: Any = None
            ) -> selectors.SelectorKey:
                _wait_for(handoff)  # So the sleeper exists and is known.
                raise OSError(errno.ENOSPC, "injected: no room to watch the pipe")

        def no_selector() -> selectors.BaseSelector:
            raise OSError(errno.EMFILE, "injected: no descriptor for the selector")

        factory: Callable[[], selectors.BaseSelector] = {
            "no-selector": no_selector,
            "unwatchable": UnwatchableSelector,
        }.get(fault, RecordedSelector)
        monkeypatch.setattr(selectors, "DefaultSelector", factory)
        monkeypatch.setattr(subprocess, "Popen", ArmedPopen)

        def interrupt() -> bool:
            if fault == "interrupt" and handoff.exists():
                raise KeyboardInterrupt
            return False

        self.registry: MutableMapping[int, OwnedProcess]
        self.call: Callable[[], object]
        self.cancel: Callable[[], None]
        if caller == "run":
            owner = _Owner(interrupt, self.released)
            self.registry = owner._active
            self.call = lambda: owner.run([str(script), "msg", mode, str(handoff)], timeout=10.0)
            self.cancel = owner.cancel
        else:
            self.registry = _Registry(self.released)
            monkeypatch.setattr(noctalia, "_ACTIVE", self.registry)
            monkeypatch.setattr(noctalia, "_executable", lambda: str(script))
            self.call = lambda: noctalia.message(mode, str(handoff), cancelled=interrupt)
            self.cancel = noctalia.cancel_pending

    def run(self) -> tuple[object, BaseException | None]:
        """The call's result, or what it raised, KeyboardInterrupt included."""
        try:
            return self.call(), None
        except BaseException as error:  # Asserted on by the tests.
            return None, error
        finally:
            while self._undo:
                self._undo.pop()()

    def sleeper(self) -> int:
        if self._sleeper is None:
            _leader, self._sleeper = handed_off(self.handoff)
        return self._sleeper

    def reap_sleeper(self) -> int | None:
        status = reaped(self.sleeper(), 3.0)
        self._collected = status is not None
        return status

    def assert_ended(self) -> None:
        """The group was ended, and only then let go of, and nothing is left open."""
        (process,) = self.spawned
        assert process.returncode is not None, "the owner let go of a leader it never reaped"
        status = self.reap_sleeper()
        assert status is not None, "the stand-in's sleeper outlived the call"
        assert os.WIFSIGNALED(status)
        assert self.released, "the owner never let go of the child"
        assert all(code is not None for code in self.released), (
            "the registry let go of the child before its group was ended"
        )
        assert not self.registry
        for stream in (process.stdin, process.stdout, process.stderr):
            assert stream is None or stream.closed
        for made in self.selectors_made:
            with pytest.raises(ValueError):
                made.fileno()  # Closed.

    def close(self) -> None:
        """Undo the fault, then end and reap whatever of the stand-in's is left."""
        while self._undo:
            self._undo.pop()()
        for process in self.spawned:
            if process.returncode is None:
                # Never reaped, so its pid, and so its group id, are still
                # this test's own child's. That also ends a leaked sleeper.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        if self.spawned and not self._collected:
            # Not reaped yet, so its pid is still reserved; once its leader is
            # gone this test, the subreaper, is its parent.
            with contextlib.suppress(FileNotFoundError, ValueError):
                if self._sleeper is None:
                    _leader, self._sleeper = handed_off(self.handoff, 0.5)
                kill_and_reap(self._sleeper)


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("caller", ["run", "noctalia"])
def test_every_way_out_of_an_exchange_ends_the_group_before_letting_go(
    caller: str, fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(caller, fault, tmp_path, monkeypatch)
    subreaper(True)
    try:
        outcome, failure = harness.run()

        if fault == "no-selector":
            # The selector comes first, so there is no child to lose.
            assert harness.spawned == [], "a child was started before its selector existed"
            cause = failure.__cause__ if isinstance(failure, noctalia.NoctaliaError) else failure
            if caller == "noctalia":
                assert isinstance(failure, noctalia.NoctaliaError), failure
            assert isinstance(cause, OSError) and cause.errno == errno.EMFILE, failure
            assert not harness.registry
            return

        if fault == "fd-pressure":
            # Nothing past the spawn needs a new descriptor any more: the call
            # finishes as it did before the shared exchange.
            assert failure is None, failure
            assert outcome == "ready" or getattr(outcome, "returncode", None) == 0, outcome
        elif fault == "interrupt":
            assert isinstance(failure, KeyboardInterrupt), failure
        else:
            expected = errno.EIO if fault == "read-eio" else errno.ENOSPC
            assert isinstance(failure, OSError) and failure.errno == expected, failure
        harness.assert_ended()
    finally:
        harness.close()
        subreaper(False)


@contextlib.contextmanager
def _sigint_at(code: CodeType, source: str | None = None) -> Iterator[list[int]]:
    """Deliver one real SIGINT to this thread at the first line run in ``code``,
    or at its first line whose text contains ``source``.

    The handler is Python's own ``KeyboardInterrupt`` one, installed here in
    case SIGINT is ignored where the tests run, and restored afterwards with
    the trace function. Yields the line numbers it fired at.
    """
    fired: list[int] = []

    def at_line(frame: FrameType, event: str, _arg: Any) -> Any:
        if event == "line" and not fired:
            text = linecache.getline(code.co_filename, frame.f_lineno)
            if source is None or source in text:
                fired.append(frame.f_lineno)
                signal.raise_signal(signal.SIGINT)
        return at_line

    def on_call(frame: FrameType, _event: str, _arg: Any) -> Any:
        return at_line if frame.f_code is code and not fired else None

    previous_trace = sys.gettrace()
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.settrace(on_call)
    try:
        yield fired
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGINT, previous_handler)


def _caused_by_interrupt(error: BaseException | None) -> bool:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, KeyboardInterrupt):
            return True
        seen.add(id(error))
        error = error.__context__
    return False


#: Where the SIGINT lands: on ``release()``'s first line, before anything in it
#: is protected; in ``end_group()`` just before it would send its first kill;
#: or on entry to the selector's ``close()``, ahead of the pipes' (V-2).
BOUNDARIES: dict[str, tuple[CodeType, str | None]] = {
    "release-entry": (worker_processes.release.__code__, None),
    "before-first-kill": (OwnedProcess.end_group.__code__, "self.signal_group("),
    "selector-close": (selectors.EpollSelector.close.__code__, None),
}


@pytest.mark.parametrize("boundary", sorted(BOUNDARIES))
@pytest.mark.parametrize("caller", ["run", "noctalia"])
def test_an_interrupted_cleanup_still_ends_the_group_before_letting_go(
    caller: str, boundary: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sweep 5 V-1 and V-2: a read error starts the cleanup, then a real SIGINT
    cuts it off, before it has signalled anything or while it closes. The
    group still ends, only then does the owner let go of it, and the
    selector and every pipe are still closed."""
    harness = _Harness(caller, "read-eio", tmp_path, monkeypatch)
    code, source = BOUNDARIES[boundary]
    subreaper(True)
    try:
        with _sigint_at(code, source) as fired:
            _outcome, failure = harness.run()

        assert fired, "the SIGINT was never delivered"
        assert isinstance(failure, OSError) and failure.errno == errno.EIO, failure
        assert _caused_by_interrupt(failure.__context__), "the interrupt was not chained"
        harness.assert_ended()
    finally:
        harness.close()
        subreaper(False)


@pytest.mark.parametrize("caller", ["run", "noctalia"])
def test_a_group_that_could_not_be_ended_stays_owned_until_cancelled(
    caller: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sweep 5 V-1: if even the retried termination is cut off every time, the
    owner keeps the child registered, so a later cancellation still ends it."""
    harness = _Harness(caller, "read-eio", tmp_path, monkeypatch)

    def interrupted(_self: OwnedProcess, *_arguments: object, **_options: object) -> None:
        raise KeyboardInterrupt  # Before any signal, every time.

    monkeypatch.setattr(OwnedProcess, "end_group", interrupted)
    monkeypatch.setattr(OwnedProcess, "kill_group", interrupted, raising=False)
    subreaper(True)
    try:
        _outcome, failure = harness.run()

        assert isinstance(failure, OSError) and failure.errno == errno.EIO, failure
        assert _caused_by_interrupt(failure.__context__), "the interrupt was not chained"
        (process,) = harness.spawned
        sleeper = harness.sleeper()
        assert process.returncode is None
        assert is_alive(process.pid) and is_alive(sleeper)
        assert process.pid in harness.registry, "the owner let go of a live group"
        assert harness.released == []

        harness.cancel()

        # Still unreaped, so still this test's own child to collect.
        assert process.wait(timeout=3) == -signal.SIGKILL
        status = harness.reap_sleeper()
        assert status is not None, "the cancellation did not reach the sleeper"
        assert os.WIFSIGNALED(status)
    finally:
        harness.close()
        subreaper(False)


def test_an_interrupted_close_still_closes_the_selector_and_every_pipe() -> None:
    """Sweep 5 V-2, on release() itself: a real SIGINT on entry to the
    selector's close() used to skip every pipe's close. Each is now closed on
    its own, the interrupted one tried again, and the interrupt still raised."""
    owned, selector = worker_processes.spawn(["sleep", "30"], stdin=subprocess.PIPE)
    process = owned.process
    assert isinstance(selector, selectors.EpollSelector)
    try:
        with (
            _sigint_at(selectors.EpollSelector.close.__code__) as fired,
            pytest.raises(KeyboardInterrupt),
        ):
            worker_processes.release(owned, selector)

        assert fired, "the SIGINT was never delivered"
        assert process.returncode == -signal.SIGKILL, "the group was not ended first"
        with pytest.raises(ValueError):
            selector.fileno()  # Closed.
        for stream in (process.stdin, process.stdout, process.stderr):
            assert stream is not None and stream.closed
    finally:
        if process.returncode is None:
            # Unreaped, so still this test's own child.
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        selector.close()


def test_a_failed_release_is_chained_to_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ending the group fails too, the caller still sees the original error,
    with the cleanup failure chained to it, and the pipes are still closed."""
    real_end_group = OwnedProcess.end_group
    children: list[OwnedProcess] = []

    def end_then_fail(self: OwnedProcess, *, immediate: bool, grace: float) -> None:
        real_end_group(self, immediate=immediate, grace=grace)  # The child still goes.
        raise RuntimeError("injected release failure")

    released: list[int | None] = []

    def interrupt() -> bool:
        if children:
            raise KeyboardInterrupt
        return False

    class Recording(_Owner):
        def register(self, child: OwnedProcess) -> bool:
            children.append(child)
            return super().register(child)

    owner = Recording(interrupt, released)
    monkeypatch.setattr(OwnedProcess, "end_group", end_then_fail)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            owner.run(["sleep", "30"], timeout=10.0)

        assert isinstance(caught.value.__context__, RuntimeError)
        (child,) = children
        assert child.process.returncode is not None
        assert released == [child.process.returncode]
        for stream in (child.process.stdout, child.process.stderr):
            assert stream is not None and stream.closed
    finally:
        for child in children:
            if child.process.returncode is None:
                # Unreaped, so still this test's own child.
                child.process.kill()
                child.process.wait(timeout=5)
