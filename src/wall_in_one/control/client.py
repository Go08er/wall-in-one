"""Client for the control socket -- the implementation behind `wall-in-one ctl`.

This is what the Noctalia plugin reaches: every plugin control is one
`runAsync` of a `ctl` verb, so no socket code has to live in Luau.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import Final

from wall_in_one import paths
from wall_in_one.control.protocol import MAX_MESSAGE_BYTES, ProtocolError, Request, Response

#: The app answers control messages from its main loop, so a slow answer means
#: a busy UI, not a dead one. Still bounded -- `ctl` must never hang a plugin
#: callback.
TIMEOUT: Final = 5.0

#: Exit code for "the app is not running". Distinct from a failed command so a
#: caller can react by launching it.
EXIT_NOT_RUNNING: Final = 3


class ControlError(Exception):
    """The control request could not be delivered or was refused."""


class NotRunningError(ControlError):
    """No app is listening on the control socket."""


def send(request: Request, *, path: Path | None = None) -> Response:
    target = path if path is not None else paths.socket_path()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(TIMEOUT)
    try:
        try:
            connection.connect(str(target))
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise NotRunningError(f"no instance listening on {target}") from error
        except OSError as error:
            raise ControlError(f"cannot connect to {target}: {error}") from error

        try:
            connection.sendall(request.encode())
            line = _read_line(connection)
        except TimeoutError as error:
            raise ControlError(f"timed out after {TIMEOUT}s") from error
        except OSError as error:
            raise ControlError(f"control connection failed: {error}") from error
    finally:
        connection.close()

    if not line:
        raise ControlError("instance closed the connection without replying")
    try:
        return Response.decode(line)
    except ProtocolError as error:
        raise ControlError(str(error)) from error


def _read_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = connection.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk:
            break
        if total > MAX_MESSAGE_BYTES:
            raise ControlError("reply exceeded the message size limit")
    return b"".join(chunks).split(b"\n", 1)[0]


def dispatch(verb: str, argument: str | None) -> int:
    """Run one `ctl` verb and turn the outcome into an exit code."""
    try:
        response = send(Request(verb=verb, argument=argument))
    except NotRunningError as error:
        print(f"{error}", file=sys.stderr)
        return EXIT_NOT_RUNNING
    except ControlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    stream = sys.stdout if response.ok else sys.stderr
    if response.message:
        print(response.message, file=stream)
    return 0 if response.ok else 1
