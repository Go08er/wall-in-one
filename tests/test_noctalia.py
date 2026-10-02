"""The Noctalia CLI boundary owns and can stop its subprocesses."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.process_helpers import handed_off, is_alive, kill_and_reap, reaped, subreaper
from wall_in_one.theme import noctalia, source


def _fake_noctalia(tmp_path: Path) -> Path:
    executable = tmp_path / "noctalia"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = hang ]; then\n'
        '  : > "$3"\n'
        "  sleep 30\n"
        'elif [ "$2" = orphan ] || [ "$2" = linger ]; then\n'
        # A same-group sleeper that inherits, and so holds, the output pipes.
        "  sleep 120 &\n"
        '  echo "$$ $!" > "$3.tmp"\n'
        '  mv "$3.tmp" "$3"\n'
        '  [ "$2" = linger ] && exec sleep 30\n'
        "  printf 'ready\\n'\n"
        "else\n"
        "  printf 'ready\\n'\n"
        "fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def test_shutdown_cancels_a_running_cli_and_does_not_poison_later_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_noctalia(tmp_path)
    monkeypatch.setattr(noctalia, "_executable", lambda: str(executable))
    started = tmp_path / "started"
    failures: list[BaseException] = []

    def run() -> None:
        try:
            noctalia.message("hang", str(started))
        except BaseException as error:  # captured and asserted on the test thread
            failures.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 2.0
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists(), "the fake CLI never reached its blocking phase"

    noctalia.cancel_pending()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], noctalia.NoctaliaError)
    assert "cancelled" in str(failures[0])

    assert noctalia.message("quick") == "ready"


def test_cancelled_resolution_does_not_spawn_a_fallback_cli_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = threading.Event()
    calls: list[str] = []

    def interrupted(**_keywords: object) -> Path | None:
        calls.append("wallpaper")
        cancelled.set()
        raise noctalia.NoctaliaError("cancelled")

    def too_late(**_keywords: object) -> str:
        calls.append("mode")
        raise AssertionError("shutdown must not start a fallback Noctalia call")

    monkeypatch.setattr(source, "from_template", lambda: None)
    monkeypatch.setattr(noctalia, "current_wallpaper", interrupted)
    monkeypatch.setattr(noctalia, "current_mode", too_late)

    resolved = source.resolve(cancelled=cancelled.is_set)

    assert resolved.origin is source.Origin.FALLBACK
    assert calls == ["wallpaper"]


class _GroupSignals:
    """Stands in for ``os.killpg`` while the stand-in CLI runs.

    A signal goes through only to the stand-in's own group, and only while its
    leader is unreaped (``waitid`` with ``WNOWAIT`` still finds it), so the
    group id is still ours. Any other attempt is recorded and refused, so a
    test never signals a reused id.
    """

    def __init__(self, handoff: Path) -> None:
        self.handoff = handoff
        self.forwarded: list[tuple[int, int]] = []
        self.refused: list[tuple[int, int]] = []
        self._killpg = os.killpg

    def __call__(self, group: int, requested: int) -> None:
        try:
            leader = int(self.handoff.read_text().split()[0])
        except FileNotFoundError, ValueError, IndexError:
            leader = None
        try:
            os.waitid(os.P_PID, group, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            unreaped = True
        except ChildProcessError:
            unreaped = False
        if group != leader or not unreaped:
            self.refused.append((group, int(requested)))
            raise ProcessLookupError(group)
        self.forwarded.append((group, int(requested)))
        self._killpg(group, requested)


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
def test_a_cli_that_exits_leaving_its_group_does_not_leave_a_sleeper(
    ending: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stand-in starts a sleeper in its own group, which holds the output
    pipes, and exits. The old wrapper reaped the exited leader before it would
    signal, so neither a cancellation nor the timeout reached the sleeper, and
    its last, unbounded wait sat on the sleeper's pipes. The leader's exit now
    ends the call: the group is ended before the leader is reaped."""
    executable = _fake_noctalia(tmp_path)
    monkeypatch.setattr(noctalia, "_executable", lambda: str(executable))
    monkeypatch.setattr(noctalia, "MESSAGE_TIMEOUT", 1.0)
    handoff = tmp_path / "handoff"
    signals = _GroupSignals(handoff)
    monkeypatch.setattr(os, "killpg", signals)
    subreaper(True)
    sleeper: int | None = None
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(noctalia.message, "orphan", str(handoff))
        leader, sleeper = handed_off(handoff)
        if ending == "cancel":
            # Only once the leader has exited: a zombie, or already reaped.
            deadline = time.monotonic() + 3.0
            while is_alive(leader) and time.monotonic() < deadline:
                time.sleep(0.01)
            noctalia.cancel_pending()

        # Either the leader's exit finished the call, or the cancellation or
        # the timeout did; what matters is that it returns, promptly.
        try:
            future.result(timeout=5.0)
        except noctalia.NoctaliaError as error:
            assert "was cancelled" in str(error) or "timed out" in str(error)

        status = reaped(sleeper, 3.0)
        assert status is not None, "the CLI's sleeper outlived the call"
        sleeper = None
        assert os.WIFSIGNALED(status)
        assert signals.refused == [], "a group was signalled after its leader was reaped"
        assert not noctalia._ACTIVE
    finally:
        if sleeper is not None:
            # Only the exact sleeper this test started; its pid is held until
            # it is reaped here. This also frees a call still on its pipes.
            kill_and_reap(sleeper)
        pool.shutdown(wait=True, cancel_futures=True)
        subreaper(False)


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
def test_a_cancel_or_timeout_ends_a_running_cli_and_its_group(
    ending: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_noctalia(tmp_path)
    monkeypatch.setattr(noctalia, "_executable", lambda: str(executable))
    if ending == "timeout":
        monkeypatch.setattr(noctalia, "MESSAGE_TIMEOUT", 1.0)
    handoff = tmp_path / "handoff"
    signals = _GroupSignals(handoff)
    monkeypatch.setattr(os, "killpg", signals)
    subreaper(True)
    sleeper: int | None = None
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(noctalia.message, "linger", str(handoff))
        _leader, sleeper = handed_off(handoff)
        if ending == "cancel":
            # Nothing else ends the group first: the leader runs for 30s.
            assert is_alive(sleeper), "the sleeper was gone before the cancellation"
            noctalia.cancel_pending()
            expected = "was cancelled"
        else:
            expected = r"timed out after 1\.0s"

        with pytest.raises(noctalia.NoctaliaError, match=expected):
            future.result(timeout=5.0)

        status = reaped(sleeper, 3.0)
        assert status is not None, "the CLI's sleeper outlived the call"
        sleeper = None
        assert os.WIFSIGNALED(status)
        assert signals.forwarded, "the group was never signalled"
        assert signals.refused == [], "a group was signalled after its leader was reaped"
        assert not noctalia._ACTIVE
    finally:
        if sleeper is not None:
            kill_and_reap(sleeper)
        pool.shutdown(wait=True, cancel_futures=True)
        subreaper(False)
