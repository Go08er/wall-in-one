"""Status unavailability is distinct from absence and uncertain mutations."""

from __future__ import annotations

import socket
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from wall_in_one import paths
from wall_in_one.control import client


@pytest.mark.parametrize("verb", ["status", "next"])
def test_real_client_silent_listener_exit_is_specific_to_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    # Keep a real five-second status timeout: the companion's compatibility
    # fallback depends on the actual public wording, not an injected mock error.
    monkeypatch.setattr(client, "RUNTIME_ACTION_TIMEOUT", 0.05)
    with tempfile.TemporaryDirectory(prefix="wio-status-") as directory:
        address = Path(directory) / "runtime.sock"
        monkeypatch.setattr(paths, "runtime_socket_path", lambda: address)
        release = threading.Event()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(address))
            listener.listen(1)
            listener.settimeout(8)

            def silent_peer() -> bytes:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(8)
                    request = connection.recv(4096)
                    release.wait(8)
                    return request

            with ThreadPoolExecutor(max_workers=1) as executor:
                received = executor.submit(silent_peer)
                try:
                    result = client.dispatch(verb, None)
                finally:
                    release.set()
                assert received.result(timeout=2).count(b"\n") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    if verb == "status":
        assert result == 75
        assert captured.err == "error: timed out after 5s\n"
    else:
        assert result == 1
        assert "outcome is unknown" in captured.err


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (client.ControlError("invalid runtime response"), 1),
        (client.ControlError("timed out after 5s"), 1),
        (client.NotRunningError("no instance listening"), 3),
    ],
)
def test_status_temporary_exit_requires_a_typed_deadline_error(
    monkeypatch: pytest.MonkeyPatch, error: client.ControlError, expected: int
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(client, "send", fail)
    assert client.dispatch("status", None) == expected


@pytest.mark.parametrize(("verb", "expected"), [("status", 75), ("next", 1)])
def test_connect_deadline_is_not_confirmed_absence(
    monkeypatch: pytest.MonkeyPatch, verb: str, expected: int
) -> None:
    def time_out(_connection: socket.socket, _address: str) -> None:
        raise TimeoutError("connect deadline")

    monkeypatch.setattr(socket.socket, "connect", time_out)
    assert client.dispatch(verb, None) == expected
