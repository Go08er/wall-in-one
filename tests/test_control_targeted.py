"""Connector-scoped runtime client framing and fail-closed validation."""

from __future__ import annotations

from typing import Any

import pytest

from wall_in_one.control import client
from wall_in_one.control.protocol import Response


def test_targeted_runtime_request_uses_one_on_snapshot_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, float | None]] = []

    def send_runtime(
        verb: str, argument: str | None = None, *, timeout: float | None = None
    ) -> Response:
        calls.append((verb, argument, timeout))
        return Response.success("changed")

    monkeypatch.setattr(client, "send_runtime", send_runtime)

    assert client.send_runtime_on("DP-1", "playlist-use", "Evening photos").ok
    assert calls == [("on", "DP-1 playlist-use Evening photos", client.RUNTIME_ACTION_TIMEOUT)]


@pytest.mark.parametrize(
    ("connector", "verb", "argument", "message"),
    (
        ("", "next", None, "connector cannot be empty"),
        ("DP 1", "next", None, "connector cannot contain whitespace"),
        ("DP-1\n", "next", None, "connector cannot contain whitespace"),
        ("DP-1", "reload", None, "unsupported display runtime verb"),
        ("DP-1", "playlist-use", None, "needs an argument"),
        ("DP-1", "next", "extra", "takes no argument"),
        ("DP-1", "shuffle", "maybe", "expects default|off|on"),
        ("DP-1", "playlist-use", " bad", "leading or trailing"),
        ("DP-1", "playlist-use", "bad\x7f", "control characters"),
    ),
)
def test_targeted_runtime_request_refuses_ambiguous_or_unsupported_input(
    monkeypatch: pytest.MonkeyPatch,
    connector: str,
    verb: str,
    argument: str | None,
    message: str,
) -> None:
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid request reached the socket")
        ),
    )

    with pytest.raises(client.ControlError, match=message):
        client.send_runtime_on(connector, verb, argument)


def test_targeted_runtime_request_has_no_legacy_authoring_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def absent(*_args: Any, **_kwargs: Any) -> Response:
        raise client.NotRunningError("runtime absent")

    monkeypatch.setattr(client, "send_runtime", absent)

    with pytest.raises(client.NotRunningError, match="runtime absent"):
        client.send_runtime_on("DP-1", "schedule-follow")


def test_ctl_on_keeps_a_multiword_playlist_as_one_final_argument(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wall_in_one import cli

    calls: list[tuple[str, str, str | None]] = []

    def send(connector: str, verb: str, argument: str | None = None) -> Response:
        calls.append((connector, verb, argument))
        return Response.success("changed")

    monkeypatch.setattr(client, "send_runtime_on", send)

    assert cli.main(["ctl", "on", "DP-1", "playlist-use", "Evening", "photos"]) == 0
    assert calls == [("DP-1", "playlist-use", "Evening photos")]
    assert capsys.readouterr().out == "changed\n"


def test_ctl_on_validates_before_reaching_either_socket(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wall_in_one import cli

    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("malformed targeted command reached the runtime socket")
        ),
    )
    monkeypatch.setattr(
        client,
        "send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("targeted command fell back to the authoring socket")
        ),
    )

    assert cli.main(["ctl", "on", "DP 1", "next"]) == 1
    assert "connector cannot contain whitespace" in capsys.readouterr().err


def test_ctl_on_reports_missing_nested_verb_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wall_in_one import cli

    assert cli.main(["ctl", "on", "DP-1"]) == 1
    assert "usage: on <connector>" in capsys.readouterr().err
