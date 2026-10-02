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
"""

from __future__ import annotations

import contextlib
import errno
import os
import resource
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest

from tests.process_helpers import handed_off, kill_and_reap, reaped, subreaper
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


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("caller", ["run", "noctalia"])
def test_every_way_out_of_an_exchange_ends_the_group_before_letting_go(
    caller: str, fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _stand_in(tmp_path)
    handoff = tmp_path / "handoff"
    mode = "exit" if fault == "fd-pressure" else "linger"
    spawned: list[subprocess.Popen[bytes]] = []
    selectors_made: list[selectors.EpollSelector] = []
    undo: list[Callable[[], None]] = []
    released: list[int | None] = []
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
        def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
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

    registry: MutableMapping[int, OwnedProcess]
    if caller == "run":
        owner = _Owner(interrupt, released)
        registry = owner._active

        def call() -> object:
            return owner.run([str(script), "msg", mode, str(handoff)], timeout=10.0)

    else:
        registry = _Registry(released)
        monkeypatch.setattr(noctalia, "_ACTIVE", registry)
        monkeypatch.setattr(noctalia, "_executable", lambda: str(script))

        def call() -> object:
            return noctalia.message(mode, str(handoff), cancelled=interrupt)

    subreaper(True)
    sleeper: int | None = None
    collected = False
    try:
        outcome: object = None
        failure: BaseException | None = None
        try:
            outcome = call()
        except BaseException as error:  # Asserted on below, KeyboardInterrupt included.
            failure = error
        finally:
            while undo:
                undo.pop()()

        if fault == "no-selector":
            # The selector comes first, so there is no child to lose.
            assert spawned == [], "a child was started before its selector existed"
            cause = failure.__cause__ if isinstance(failure, noctalia.NoctaliaError) else failure
            if caller == "noctalia":
                assert isinstance(failure, noctalia.NoctaliaError), failure
            assert isinstance(cause, OSError) and cause.errno == errno.EMFILE, failure
            assert not registry
            return

        (process,) = spawned
        _leader, sleeper = handed_off(handoff)
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

        assert process.returncode is not None, "the owner let go of a leader it never reaped"
        status = reaped(sleeper, 3.0)
        assert status is not None, "the stand-in's sleeper outlived the call"
        collected = True
        assert os.WIFSIGNALED(status)
        assert released, "the owner never let go of the child"
        assert all(code is not None for code in released), (
            "the registry let go of the child before its group was ended"
        )
        assert not registry
        for stream in (process.stdin, process.stdout, process.stderr):
            assert stream is None or stream.closed
        for made in selectors_made:
            with pytest.raises(ValueError):
                made.fileno()  # Closed.
    finally:
        while undo:
            undo.pop()()
        for process in spawned:
            if process.returncode is None:
                # Never reaped, so its pid, and so its group id, are still
                # this test's own child's. That also ends a leaked sleeper.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        if spawned and not collected:
            # Not reaped yet, so its pid is still reserved; once its leader is
            # gone this test, the subreaper, is its parent.
            if sleeper is None:
                with contextlib.suppress(FileNotFoundError, ValueError):
                    _leader, sleeper = handed_off(handoff, 0.5)
            if sleeper is not None:
                kill_and_reap(sleeper)
        subreaper(False)


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
