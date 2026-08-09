"""Wire format for the control socket.

One JSON object per line, in each direction. Line-oriented so a partial read is
unambiguous, and JSON so adding a field later does not break older clients.

Kept deliberately dull: the only thing on the other end is our own `ctl` client,
which the Noctalia plugin shells out to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Self

#: Authoring requests and replies stay small and are often handled in a GTK
#: callback, so keep their original tight bound.
MAX_MESSAGE_BYTES: Final = 64 * 1024
#: A runtime status snapshot may contain the configured maximum of 512
#: playlists and 512 schedule rules. It remains bounded independently.
MAX_RUNTIME_MESSAGE_BYTES: Final = 1024 * 1024

ENCODING: Final = "utf-8"


class ProtocolError(Exception):
    """A message could not be encoded or decoded."""


@dataclass(frozen=True, slots=True)
class Request:
    verb: str
    argument: str | None = None

    def encode(self) -> bytes:
        payload: dict[str, Any] = {"verb": self.verb}
        if self.argument is not None:
            payload["argument"] = self.argument
        return _encode(payload)

    @classmethod
    def decode(cls, line: bytes) -> Self:
        payload = _decode(line)
        verb = payload.get("verb")
        if not isinstance(verb, str) or not verb:
            raise ProtocolError("request has no verb")
        argument = payload.get("argument")
        if argument is not None and not isinstance(argument, str):
            raise ProtocolError("request argument must be a string")
        return cls(verb=verb, argument=argument)


@dataclass(frozen=True, slots=True)
class Response:
    ok: bool
    message: str = ""
    #: Why it failed, for a caller rather than for a reader: a
    #: `ProviderError.kind` where one caused it, empty everywhere else. It rides
    #: in its own field so that branching on `rate-limit` never means parsing
    #: the English sentence next to it.
    kind: str = ""

    def encode(self) -> bytes:
        payload: dict[str, Any] = {"ok": self.ok, "message": self.message}
        if self.kind:
            # Omitted when empty, so an older client sees exactly the two fields
            # it always saw.
            payload["kind"] = self.kind
        return _encode(payload)

    @classmethod
    def decode(cls, line: bytes, *, max_bytes: int = MAX_MESSAGE_BYTES) -> Self:
        payload = _decode(line, max_bytes=max_bytes)
        ok = payload.get("ok")
        if not isinstance(ok, bool):
            raise ProtocolError("response has no ok flag")
        message = payload.get("message", "")
        kind = payload.get("kind", "")
        return cls(
            ok=ok,
            message=message if isinstance(message, str) else "",
            kind=kind if isinstance(kind, str) else "",
        )

    @classmethod
    def success(cls, message: str = "ok") -> Self:
        return cls(ok=True, message=message)

    @classmethod
    def failure(cls, message: str, kind: str = "") -> Self:
        return cls(ok=False, message=message, kind=kind)


def _encode(payload: dict[str, Any]) -> bytes:
    try:
        # No embedded newline can survive json.dumps, so the line framing holds.
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ProtocolError(f"cannot encode message: {error}") from error
    encoded = text.encode(ENCODING) + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message is {len(encoded)} bytes, over the {MAX_MESSAGE_BYTES} limit")
    return encoded


def _decode(line: bytes, *, max_bytes: int = MAX_MESSAGE_BYTES) -> dict[str, Any]:
    if len(line) > max_bytes:
        raise ProtocolError(f"message is {len(line)} bytes, over the {max_bytes} limit")
    try:
        payload = json.loads(line.decode(ENCODING))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProtocolError(f"cannot decode message: {error}") from error
    if not isinstance(payload, dict):
        raise ProtocolError("message must be a JSON object")
    return payload
