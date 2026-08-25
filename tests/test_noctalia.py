"""The Noctalia CLI boundary owns and can stop its subprocesses."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from wall_in_one.theme import noctalia, source


def _fake_noctalia(tmp_path: Path) -> Path:
    executable = tmp_path / "noctalia"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = hang ]; then\n'
        '  : > "$3"\n'
        "  sleep 30\n"
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
