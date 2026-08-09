"""Client for the control socket -- the implementation behind `wall-in-one ctl`.

This is what the Noctalia plugin reaches: every plugin control is one
`runAsync` of a `ctl` verb, so no socket code has to live in Luau.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from wall_in_one import paths
from wall_in_one.control.protocol import MAX_MESSAGE_BYTES, ProtocolError, Request, Response

#: The app answers control messages from its main loop, so a slow answer means
#: a busy UI, not a dead one. Still bounded -- `ctl` must never hang a plugin
#: callback.
TIMEOUT: Final = 5.0

#: A search is answered on a worker, so the wait here is the website's rather
#: than the window's -- the app stays responsive throughout. Generous enough to
#: cover a rate limiter's own spacing between requests.
SEARCH_TIMEOUT: Final = 60.0

#: A MotionBGS wallpaper is a video, and the ceiling on one is 512 MB. Minutes
#: is the honest number on a domestic line.
DOWNLOAD_TIMEOUT: Final = 600.0

#: The verbs that wait on a remote site. Everything else answers immediately or
#: is not answering at all.
TIMEOUTS: Final[Mapping[str, float]] = {
    "search": SEARCH_TIMEOUT,
    "download": DOWNLOAD_TIMEOUT,
}

RUNTIME_VERBS: Final[frozenset[str]] = frozenset(
    {
        "playlist-use",
        "schedule-follow",
        "play",
        "pause",
        "toggle",
        "shuffle",
        "next",
        "previous",
        "prev",
        "random",
        "status",
        "reload",
        "quit",
    }
)

# Verbs the retained Python --service mode already understands. They are a
# compatibility bridge while installations move to the Rust runtime.
PYTHON_RUNTIME_FALLBACKS: Final[frozenset[str]] = frozenset(
    {"playlist-use", "shuffle", "next", "prev", "random", "status", "quit"}
)

#: Exit code for "the app is not running". Distinct from a failed command so a
#: caller can react by launching it.
EXIT_NOT_RUNNING: Final = 3


class ControlError(Exception):
    """The control request could not be delivered or was refused."""


class NotRunningError(ControlError):
    """No app is listening on the control socket."""


def send(request: Request, *, path: Path | None = None, timeout: float | None = None) -> Response:
    target = path if path is not None else paths.socket_path()
    wait = timeout if timeout is not None else TIMEOUTS.get(request.verb, TIMEOUT)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(wait)
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
            raise ControlError(f"timed out after {wait}s") from error
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
    request_verb = "previous" if verb == "prev" else verb
    request_argument = argument
    if verb == "playlist-use" and (argument or "").strip().casefold() in ("", "none"):
        request_verb = "schedule-follow"
        request_argument = None
    try:
        if verb in RUNTIME_VERBS:
            try:
                response = send(
                    Request(verb=request_verb, argument=request_argument),
                    path=paths.runtime_socket_path(),
                )
            except NotRunningError:
                if verb not in PYTHON_RUNTIME_FALLBACKS:
                    raise
                response = send(Request(verb=verb, argument=argument))
        else:
            response = send(Request(verb=verb, argument=argument))
    except NotRunningError as error:
        if verb == "open" and argument:
            try:
                subprocess.Popen(
                    [sys.argv[0], "--open-page", argument],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as launch_error:
                print(f"error: cannot open Wall-in-One: {launch_error}", file=sys.stderr)
                return 1
            print(f"opened {argument}")
            return 0
        print(f"{error}", file=sys.stderr)
        return EXIT_NOT_RUNNING
    except ControlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if response.kind == "runtime-not-running":
        if response.message:
            print(response.message, file=sys.stderr)
        return EXIT_NOT_RUNNING

    stream = sys.stdout if response.ok else sys.stderr
    if response.message:
        # A provider failure already reads `kind: message`, the same sentence
        # the browse dialog toasts, so printing the message prints the kind
        # with it. `Response.kind` is there for a caller that wants to branch on
        # the reason rather than read it.
        print(response.message, file=stream)
    return 0 if response.ok else 1


def send_runtime(
    verb: str, argument: str | None = None, *, timeout: float | None = None
) -> Response:
    """Talk to the Rust runtime directly, without an authoring-socket fallback."""
    return send(
        Request(verb=verb, argument=argument),
        path=paths.runtime_socket_path(),
        timeout=timeout,
    )
