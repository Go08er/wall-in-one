from __future__ import annotations

import pytest

from wall_in_one.control.protocol import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    Request,
    Response,
)
from wall_in_one.control.server import build_verb_table, handle, parse_toggle


def test_request_round_trip() -> None:
    for request in (Request("next"), Request("cycle-interval", "600")):
        assert Request.decode(request.encode()) == request


def test_response_round_trip() -> None:
    for response in (Response.success("ok"), Response.failure("nope")):
        assert Response.decode(response.encode()) == response


def test_encoding_is_one_line() -> None:
    encoded = Request("shuffle", "on\nnot-a-second-message").encode()
    assert encoded.count(b"\n") == 1
    assert encoded.endswith(b"\n")


@pytest.mark.parametrize("line", [b"not json", b"[]", b'{"argument":"x"}', b'{"verb":""}'])
def test_request_rejects_malformed(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        Request.decode(line)


def test_oversized_message_is_refused() -> None:
    with pytest.raises(ProtocolError):
        Request.decode(b"x" * (MAX_MESSAGE_BYTES + 1))


@pytest.mark.parametrize(
    ("value", "current", "expected"),
    [
        ("on", False, True),
        ("off", True, False),
        ("toggle", False, True),
        ("toggle", True, False),
        (None, False, True),
        ("TRUE", False, True),
        ("disabled", True, False),
    ],
)
def test_parse_toggle(value: str | None, current: bool, expected: bool) -> None:
    assert parse_toggle(value, current) is expected


def test_parse_toggle_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="expected on, off or toggle"):
        parse_toggle("sideways", False)


class _StubCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def _record(self, verb: str, argument: str | None = None) -> Response:
        self.calls.append((verb, argument))
        return Response.success(verb)

    def next_wallpaper(self) -> Response:
        return self._record("next")

    def previous_wallpaper(self) -> Response:
        return self._record("prev")

    def random_wallpaper(self) -> Response:
        return self._record("random")

    def set_shuffle(self, value: str | None) -> Response:
        return self._record("shuffle", value)

    def set_cycle(self, value: str | None) -> Response:
        return self._record("cycle", value)

    def set_cycle_interval(self, value: str | None) -> Response:
        return self._record("cycle-interval", value)

    def set_dynamics(self, value: str | None) -> Response:
        return self._record("dynamics", value)

    def reload_palette(self) -> Response:
        return self._record("reload-palette")

    def report_status(self) -> Response:
        return self._record("status")

    def quit(self) -> Response:
        return self._record("quit")


def test_verb_table_covers_the_documented_cli_surface() -> None:
    from wall_in_one.cli import CTL_VERBS

    verbs = build_verb_table(_StubCommands())
    assert set(verbs) == set(CTL_VERBS)


def test_handle_dispatches_and_passes_the_argument() -> None:
    commands = _StubCommands()
    verbs = build_verb_table(commands)
    response = handle(verbs, Request("cycle-interval", "600").encode())
    assert response.ok
    assert commands.calls == [("cycle-interval", "600")]


def test_handle_reports_an_unknown_verb_without_raising() -> None:
    response = handle(build_verb_table(_StubCommands()), Request("fly").encode())
    assert not response.ok
    assert "unknown verb" in response.message


def test_handle_survives_a_handler_that_raises() -> None:
    class Exploding(_StubCommands):
        def next_wallpaper(self) -> Response:
            raise RuntimeError("disk on fire")

    response = handle(build_verb_table(Exploding()), Request("next").encode())
    assert not response.ok
    assert "disk on fire" in response.message


def test_handle_reports_malformed_input_without_raising() -> None:
    response = handle(build_verb_table(_StubCommands()), b"{{{")
    assert not response.ok
