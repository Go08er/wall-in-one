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
from wall_in_one.control.protocol import (
    MAX_MESSAGE_BYTES,
    MAX_RUNTIME_MESSAGE_BYTES,
    ProtocolError,
    Request,
    Response,
)

#: The app answers control messages from its main loop, so a slow answer means
#: a busy UI, not a dead one. Still bounded -- `ctl` must never hang a plugin
#: callback.
TIMEOUT: Final = 5.0

# Applying an entry is deliberately synchronous on the Rust wire: the reply
# means the still, palette and renderer hand-over actually happened. Each
# external desktop helper has its own three-second bound, and a multi-display
# apply can legitimately outlive the authoring socket's five-second budget.
# Keep this below the plugin's 55-second command ceiling while avoiding the
# worse outcome where ctl reports failure and the wallpaper changes afterward.
RUNTIME_ACTION_TIMEOUT: Final = 45.0

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
        "stop",
        "toggle",
        "shuffle",
        "cycle",
        "next",
        "previous",
        "prev",
        "random",
        "status",
        "reload",
        "quit",
    }
)

RUNTIME_APPLY_VERBS: Final[frozenset[str]] = frozenset(
    {
        "playlist-use",
        "schedule-follow",
        "play",
        "pause",
        "stop",
        "toggle",
        "next",
        "previous",
        "prev",
        "random",
        "reload",
        "quit",
    }
)

# Connector-scoped commands are deliberately narrower than the global runtime
# surface.  Configuration, reload, status and quit have no meaningful
# per-display form.  Keeping the allow-list here prevents a typo in GTK from
# turning into an ambiguous line which a newer daemon might interpret
# differently.
TARGETED_RUNTIME_ARGUMENTS: Final[Mapping[str, frozenset[str] | None]] = {
    "playlist-use": None,
    "schedule-follow": frozenset(),
    "play": frozenset(),
    "pause": frozenset(),
    "stop": frozenset(),
    "toggle": frozenset(),
    "shuffle": frozenset({"on", "off", "default"}),
    "cycle": frozenset({"on", "off", "default"}),
    "next": frozenset(),
    "previous": frozenset(),
    "random": frozenset(),
}
MAX_TARGET_CONNECTOR_BYTES: Final = 256
MAX_TARGET_ARGUMENT_BYTES: Final = 120 * 4

# Verbs the retained Python --service mode already understands. They are a
# compatibility bridge while installations move to the Rust runtime.
PYTHON_RUNTIME_FALLBACKS: Final[frozenset[str]] = frozenset(
    {
        "playlist-use",
        "schedule-follow",
        "shuffle",
        "cycle",
        "next",
        "previous",
        "prev",
        "random",
        "status",
        "quit",
    }
)

OPEN_PAGE_ALIASES: Final[Mapping[str, str]] = {
    "browse": "browse",
    "media": "media",
    "pairings": "media",
    "playlists": "playlists",
    "schedules": "schedules",
    "displays": "schedules",
    "settings": "settings",
}

#: Exit code for "the app is not running". Distinct from a failed command so a
#: caller can react by launching it.
EXIT_NOT_RUNNING: Final = 3


class ControlError(Exception):
    """The control request could not be delivered or was refused."""


class NotRunningError(ControlError):
    """No app is listening on the control socket."""


def send(request: Request, *, path: Path | None = None, timeout: float | None = None) -> Response:
    target = path if path is not None else paths.socket_path()
    max_reply = (
        MAX_RUNTIME_MESSAGE_BYTES if target == paths.runtime_socket_path() else MAX_MESSAGE_BYTES
    )
    if timeout is not None:
        wait = timeout
    elif target == paths.runtime_socket_path() and request.verb in RUNTIME_APPLY_VERBS:
        wait = RUNTIME_ACTION_TIMEOUT
    else:
        wait = TIMEOUTS.get(request.verb, TIMEOUT)
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
            line = _read_line(connection, max_bytes=max_reply)
        except TimeoutError as error:
            raise ControlError(f"timed out after {wait}s") from error
        except OSError as error:
            raise ControlError(f"control connection failed: {error}") from error
    finally:
        connection.close()

    if not line:
        raise ControlError("instance closed the connection without replying")
    try:
        return Response.decode(line, max_bytes=max_reply)
    except ProtocolError as error:
        raise ControlError(str(error)) from error


def _read_line(connection: socket.socket, *, max_bytes: int = MAX_MESSAGE_BYTES) -> bytes:
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
        if total > max_bytes:
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
        if verb == "on":
            if argument is None:
                raise ControlError(
                    "usage: on <connector> <playlist-use|schedule-follow|play|pause|toggle|"
                    "stop|shuffle|cycle|next|previous|random> [argument]"
                )
            words = argument.split(maxsplit=2)
            if len(words) < 2:
                raise ControlError(
                    "usage: on <connector> <playlist-use|schedule-follow|play|pause|toggle|"
                    "stop|shuffle|cycle|next|previous|random> [argument]"
                )
            connector, targeted_verb = words[:2]
            targeted_argument = words[2] if len(words) == 3 else None
            response = send_runtime_on(connector, targeted_verb, targeted_argument)
        elif verb in RUNTIME_VERBS:
            try:
                response = send(
                    Request(verb=request_verb, argument=request_argument),
                    path=paths.runtime_socket_path(),
                )
            except NotRunningError:
                if verb not in PYTHON_RUNTIME_FALLBACKS:
                    raise
                legacy_verb = "prev" if verb == "previous" else verb
                legacy_argument = argument
                if verb == "schedule-follow":
                    legacy_verb = "playlist-use"
                    legacy_argument = "none"
                response = send(Request(verb=legacy_verb, argument=legacy_argument))
        else:
            response = send(Request(verb=verb, argument=argument))
    except NotRunningError as error:
        if verb == "open" and argument:
            requested = argument.strip().casefold()
            page = OPEN_PAGE_ALIASES.get(requested)
            if page is None:
                choices = "|".join(OPEN_PAGE_ALIASES)
                print(f"error: usage: open <{choices}>", file=sys.stderr)
                return 1
            try:
                subprocess.Popen(
                    [sys.argv[0], "--open-page", page],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as launch_error:
                print(f"error: cannot open Wall-in-One: {launch_error}", file=sys.stderr)
                return 1
            print(f"launch requested for {page}")
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


def dispatch_on(connector: str, verb: str, argument: str | None) -> int:
    """Run one validated connector-scoped ``ctl on`` command."""
    try:
        response = send_runtime_on(connector, verb, argument)
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


def send_runtime(
    verb: str, argument: str | None = None, *, timeout: float | None = None
) -> Response:
    """Talk to the Rust runtime directly, without an authoring-socket fallback."""
    return send(
        Request(verb=verb, argument=argument),
        path=paths.runtime_socket_path(),
        timeout=timeout,
    )


def send_runtime_on(
    connector: str,
    verb: str,
    argument: str | None = None,
    *,
    timeout: float | None = None,
) -> Response:
    """Send one strictly validated connector-scoped runtime command.

    The Rust wire stays the existing two-field request: ``verb`` is ``on`` and
    its single argument is ``<connector> <verb> [argument]``.  Connector names
    and every app-authored runtime identity are protocol tokens, so accepting
    whitespace here would make the boundary ambiguous before it reached Rust.
    There is intentionally no Python-service fallback: the retained Python
    runtime has one global cursor and cannot honestly emulate this operation.
    """
    _runtime_token(
        connector,
        label="display connector",
        maximum_bytes=MAX_TARGET_CONNECTOR_BYTES,
    )
    allowed_arguments = TARGETED_RUNTIME_ARGUMENTS.get(verb)
    if verb not in TARGETED_RUNTIME_ARGUMENTS:
        choices = ", ".join(TARGETED_RUNTIME_ARGUMENTS)
        raise ControlError(f"unsupported display runtime verb {verb!r}; expected one of {choices}")

    if allowed_arguments is None:
        if argument is None:
            raise ControlError(f"display runtime verb {verb!r} needs an argument")
        _runtime_text(
            argument,
            label=f"{verb} argument",
            maximum_bytes=MAX_TARGET_ARGUMENT_BYTES,
        )
    elif not allowed_arguments:
        if argument is not None:
            raise ControlError(f"display runtime verb {verb!r} takes no argument")
    elif argument not in allowed_arguments:
        choices = "|".join(sorted(allowed_arguments))
        raise ControlError(f"display runtime verb {verb!r} expects {choices}")

    wire = f"{connector} {verb}" + (f" {argument}" if argument is not None else "")
    return send_runtime(
        "on",
        wire,
        timeout=RUNTIME_ACTION_TIMEOUT if timeout is None else timeout,
    )


def _runtime_token(value: str, *, label: str, maximum_bytes: int) -> None:
    """Validate one whitespace-delimited token before building the wire line."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ControlError(f"{label} must be valid UTF-8") from error
    if not value:
        raise ControlError(f"{label} cannot be empty")
    if len(encoded) > maximum_bytes:
        raise ControlError(f"{label} must be at most {maximum_bytes} UTF-8 bytes")
    if any(character.isspace() for character in value):
        raise ControlError(f"{label} cannot contain whitespace")
    if any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value):
        raise ControlError(f"{label} cannot contain control characters")


def _runtime_text(value: str, *, label: str, maximum_bytes: int) -> None:
    """Validate the nonempty final remainder accepted by ``playlist-use``."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ControlError(f"{label} must be valid UTF-8") from error
    if not value:
        raise ControlError(f"{label} cannot be empty")
    if value != value.strip():
        raise ControlError(f"{label} cannot have leading or trailing whitespace")
    if len(encoded) > maximum_bytes:
        raise ControlError(f"{label} must be at most {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value):
        raise ControlError(f"{label} cannot contain control characters")
