"""Client for the control socket -- the implementation behind `wall-in-one ctl`.

This is what the Noctalia plugin reaches: every plugin control is one
`runAsync` of a `ctl` verb, so no socket code has to live in Luau.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
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

#: Cheap read-only authoring queries and non-applying runtime commands should
#: answer from already-owned state.  Keep their bound short so ``ctl status``
#: remains useful as a liveness probe.
TIMEOUT: Final = 5.0

#: One ordinary durable authoring transaction may spend five seconds acquiring
#: its cross-process mutation lock before the atomic write and parent-directory
#: fsync begin.  The old five-second client deadline could therefore expire at
#: the exact moment a successful commit started.  Fifteen seconds retains a
#: finite UI/plugin boundary while leaving honest I/O margin after the lock.
AUTHORING_TIMEOUT: Final = 15.0

# Applying an entry is deliberately synchronous on the Rust wire: the reply
# means the still, palette and renderer hand-over actually happened. Each
# external desktop helper has its own three-second bound, and a multi-display
# apply can legitimately outlive the authoring socket's five-second budget.
# Companion clients need a callback ceiling above this 45-second wire budget;
# otherwise the UI can report failure while the confirmed handover completes.
# Verify the bundled companion against this boundary when changing its pin;
# an older client deadline can otherwise turn a confirmed apply into an error.
RUNTIME_ACTION_TIMEOUT: Final = 45.0

#: ``ctl displays`` runs compositor discovery on the ordered runtime worker.
#: Discovery itself has a five-second ceiling; allow an equal margin for worker
#: hand-off and response delivery instead of reproducing that inner boundary at
#: the client.
DISPLAY_DISCOVERY_TIMEOUT: Final = 10.0

#: Playlist deletion is a cross-store cascade.  Each store has its own bounded
#: five-second mutation lock, so it cannot honestly share a one-store deadline.
CASCADE_TIMEOUT: Final = 45.0

#: Delete/trash first journals authority, performs and durably records the
#: filesystem operation, then cleans several independently durable stores and
#: the journal.  A timeout cannot cancel that sequence safely.
REMOVAL_TIMEOUT: Final = 60.0

#: Select and the authoring-socket playlist compatibility path may perform one
#: store transaction, take the compiler lock, reload a changed runtime document
#: and finally apply the selected playlist.  The two runtime exchanges each
#: have a 45-second wire ceiling; this is their bounded end-to-end envelope with
#: the two five-second locks and a small delivery margin.
COMPOSED_RUNTIME_TIMEOUT: Final = 110.0

#: With no rendered template, live palette resolution can make three ten-second
#: Noctalia queries, one 30-second generation, and a final ten-second fallback
#: query.  A newly requested reload cannot unsafely kill an older resolution
#: already executing on the single theme worker: its strict queue bound is the
#: old 70-second resolution, one ten-second explicit action, one ten-second
#: wallpaper query, then the new 70-second resolution.  Twenty seconds of
#: delivery/reap margin keeps that worst 160-second chain inside the contract.
PALETTE_RELOAD_TIMEOUT: Final = 180.0

#: A search is answered on a worker, so the wait here is the website's rather
#: than the window's -- the app stays responsive throughout. Generous enough to
#: cover a rate limiter's own spacing between requests.
SEARCH_TIMEOUT: Final = 60.0

#: A MotionBGS wallpaper is a video, and the ceiling on one is 512 MB. Minutes
#: is the honest number on a domestic line.
DOWNLOAD_TIMEOUT: Final = 600.0

#: Complete authoring-socket deadline table.  Keeping every protocol verb here
#: makes a new verb choose its latency contract deliberately instead of falling
#: back unnoticed to the historical five-second authoring deadline.  Runtime
#: requests use their separate branch in :func:`send`, even where a verb name
#: overlaps (for example ``shuffle`` or ``playlist-use``).
TIMEOUTS: Final[Mapping[str, float]] = {
    "next": RUNTIME_ACTION_TIMEOUT,
    "prev": RUNTIME_ACTION_TIMEOUT,
    "random": RUNTIME_ACTION_TIMEOUT,
    "shuffle": AUTHORING_TIMEOUT,
    "cycle": AUTHORING_TIMEOUT,
    "cycle-interval": AUTHORING_TIMEOUT,
    "dynamics": AUTHORING_TIMEOUT,
    "reload-palette": PALETTE_RELOAD_TIMEOUT,
    "open": TIMEOUT,
    "status": TIMEOUT,
    "list": TIMEOUT,
    "select": COMPOSED_RUNTIME_TIMEOUT,
    "favourites": TIMEOUT,
    "favourite": AUTHORING_TIMEOUT,
    "unfavourite": AUTHORING_TIMEOUT,
    "remove": REMOVAL_TIMEOUT,
    "pairing": TIMEOUT,
    "still": AUTHORING_TIMEOUT,
    "palette": AUTHORING_TIMEOUT,
    "reset-pairing": AUTHORING_TIMEOUT,
    "playlists": TIMEOUT,
    "playlist-new": AUTHORING_TIMEOUT,
    "playlist-delete": CASCADE_TIMEOUT,
    "playlist-add": AUTHORING_TIMEOUT,
    "playlist-remove": AUTHORING_TIMEOUT,
    "playlist-use": COMPOSED_RUNTIME_TIMEOUT,
    "displays": DISPLAY_DISCOVERY_TIMEOUT,
    "display-assign": AUTHORING_TIMEOUT,
    "display-clear": AUTHORING_TIMEOUT,
    "schedule": TIMEOUT,
    "schedule-add": AUTHORING_TIMEOUT,
    "schedule-remove": AUTHORING_TIMEOUT,
    "providers": TIMEOUT,
    "search": SEARCH_TIMEOUT,
    "download": DOWNLOAD_TIMEOUT,
    "quit": TIMEOUT,
}

#: Authoring requests for which a client timeout can occur after an atomic
#: durability boundary.  Closing the socket cannot safely cancel an fsync or
#: roll back a committed filesystem operation, so callers must verify rather
#: than treating a timeout as a refusal and blindly retrying.  ``cycle-interval``
#: is handled specially below because its argument-less form is a read.
DURABLE_MUTATION_VERBS: Final[frozenset[str]] = frozenset(
    {
        "shuffle",
        "cycle",
        "cycle-interval",
        "dynamics",
        "select",
        "favourite",
        "unfavourite",
        "remove",
        "still",
        "palette",
        "reset-pairing",
        "playlist-new",
        "playlist-delete",
        "playlist-add",
        "playlist-remove",
        "display-assign",
        "display-clear",
        "schedule-add",
        "schedule-remove",
        "download",
    }
)

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

# A response deadline does not roll back work which already crossed the
# receiver.  These commands can still change playback, external palette state,
# application presentation, or process lifetime after their client socket has
# closed even when they do not write an app-owned state file.
AUTHORING_SIDE_EFFECT_VERBS: Final[frozenset[str]] = (
    DURABLE_MUTATION_VERBS | (PYTHON_RUNTIME_FALLBACKS - {"status"}) | {"open", "reload-palette"}
)
RUNTIME_SIDE_EFFECT_VERBS: Final[frozenset[str]] = (RUNTIME_VERBS - {"status"}) | {"on"}

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
# Only `ctl status` maps a typed deadline failure to this temporary result.
# Mutating commands still return 1: their outcome may be unknown, not retryable.
EXIT_STATUS_UNAVAILABLE: Final = 75


class ControlError(Exception):
    """The control request could not be delivered or was refused."""


class NotRunningError(ControlError):
    """No app is listening on the control socket."""


class ControlTimeoutError(ControlError):
    """The peer did not answer within the control exchange deadline."""


class Cancellation:
    """Close registered requests and refuse requests after app shutdown.

    Executor shutdown does not terminate a running thread; Python joins it at
    interpreter exit.  A runtime apply could therefore retain the authoring
    process for its entire 45-second wire deadline.  Registration and
    cancellation share one lock, so a socket is either closed by ``cancel`` or
    observes the stopped state before performing I/O.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._connections: set[socket.socket] = set()

    def register(self, connection: socket.socket) -> bool:
        """Register ``connection`` unless this request group has stopped."""
        with self._lock:
            if self._cancelled:
                return False
            self._connections.add(connection)
            return True

    def unregister(self, connection: socket.socket) -> None:
        """Forget one request which reached its ordinary completion path."""
        with self._lock:
            self._connections.discard(connection)

    def cancel(self) -> None:
        """Atomically stop accepting requests and wake every socket waiter."""
        with self._lock:
            self._cancelled = True
            connections = tuple(self._connections)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            # A connect still in progress or a peer which already closed may
            # not have a full-duplex stream to shut down. Closing is still the
            # wake-up boundary and is normally idempotent.
            with suppress(OSError):
                connection.close()


def _gui_launch_command(page: str) -> list[str]:
    """Return a stable invocation for a fresh authoring application.

    A console-script launch has a useful executable in ``argv[0]``. Module
    launches instead point at ``wall_in_one/__main__.py``, which is normally
    neither executable nor a valid standalone entry point, so reproduce that
    invocation through the current interpreter.
    """
    argv0 = sys.argv[0]
    executable = shutil.which(argv0) if os.sep not in argv0 else argv0
    if executable is not None and Path(executable).is_file() and os.access(executable, os.X_OK):
        return [executable, "--open-page", page]
    return [sys.executable, "-m", "wall_in_one", "--open-page", page]


def send(
    request: Request,
    *,
    path: Path | None = None,
    timeout: float | None = None,
    cancellation: Cancellation | None = None,
) -> Response:
    target = path if path is not None else paths.socket_path()
    max_reply = (
        MAX_RUNTIME_MESSAGE_BYTES if target == paths.runtime_socket_path() else MAX_MESSAGE_BYTES
    )
    if timeout is not None:
        wait = timeout
    elif target == paths.runtime_socket_path():
        wait = RUNTIME_ACTION_TIMEOUT if request.verb in RUNTIME_APPLY_VERBS else TIMEOUT
    else:
        wait = TIMEOUTS.get(request.verb, TIMEOUT)
    deadline = time.monotonic() + wait
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(wait)
    if cancellation is not None and not cancellation.register(connection):
        connection.close()
        raise ControlError("control request cancelled during shutdown")
    try:
        try:
            connection.connect(str(target))
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise NotRunningError(f"no instance listening on {target}") from error
        except TimeoutError as error:
            raise ControlTimeoutError(
                f"cannot connect to {target}: timed out after {wait:g}s"
            ) from error
        except OSError as error:
            raise ControlError(f"cannot connect to {target}: {error}") from error

        try:
            _set_remaining_timeout(connection, deadline)
            connection.sendall(request.encode())
            line = _read_line(connection, max_bytes=max_reply, deadline=deadline)
        except TimeoutError as error:
            raise ControlTimeoutError(
                _timeout_message(request, target=target, wait=wait)
            ) from error
        except OSError as error:
            raise ControlError(f"control connection failed: {error}") from error
    finally:
        if cancellation is not None:
            cancellation.unregister(connection)
        connection.close()

    if not line:
        raise ControlError("instance closed the connection without replying")
    try:
        return Response.decode(line, max_bytes=max_reply)
    except ProtocolError as error:
        raise ControlError(str(error)) from error


def _timeout_message(request: Request, *, target: Path, wait: float) -> str:
    """Describe a deadline without pretending an in-flight commit was refused."""
    elapsed = f"{wait:g}"
    if _is_durable_mutation(request, target=target):
        return (
            f"timed out after {elapsed}s; this durable change may already have committed, "
            "so its outcome is unknown. Verify the current state before retrying"
        )
    if _has_side_effect(request, target=target):
        return (
            f"timed out after {elapsed}s; this command may still complete, so its outcome is "
            "unknown. Verify the current state before retrying"
        )
    return f"timed out after {elapsed}s"


def _is_durable_mutation(request: Request, *, target: Path) -> bool:
    """Whether an authoring request can cross a durable boundary before reply."""
    if target == paths.runtime_socket_path() or request.verb not in DURABLE_MUTATION_VERBS:
        return False
    # With no operand this is the one read hidden behind an otherwise mutating
    # verb.  ``shuffle``/``cycle``/``dynamics`` default to toggle and therefore
    # remain mutations when their operand is absent.
    return request.verb != "cycle-interval" or request.argument is not None


def _has_side_effect(request: Request, *, target: Path) -> bool:
    """Whether a timed-out receiver may still produce an observable change."""
    if target == paths.runtime_socket_path():
        return request.verb in RUNTIME_SIDE_EFFECT_VERBS
    if request.verb == "cycle-interval" and request.argument is None:
        return False
    return request.verb in AUTHORING_SIDE_EFFECT_VERBS


def _set_remaining_timeout(connection: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("control exchange deadline expired")
    connection.settimeout(remaining)


def _read_line(
    connection: socket.socket,
    *,
    max_bytes: int = MAX_MESSAGE_BYTES,
    deadline: float | None = None,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        # socket.settimeout alone is per recv: a peer sending one byte just
        # before each timeout could retain a settings/update worker forever.
        # Connect, send and every response fragment share one exchange budget.
        if deadline is not None:
            _set_remaining_timeout(connection, deadline)
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
                    _gui_launch_command(page),
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
        return (
            EXIT_STATUS_UNAVAILABLE
            if verb == "status" and isinstance(error, ControlTimeoutError)
            else 1
        )

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
    verb: str,
    argument: str | None = None,
    *,
    timeout: float | None = None,
    cancellation: Cancellation | None = None,
) -> Response:
    """Talk to the Rust runtime directly, without an authoring-socket fallback."""
    if cancellation is None:
        return send(
            Request(verb=verb, argument=argument),
            path=paths.runtime_socket_path(),
            timeout=timeout,
        )
    return send(
        Request(verb=verb, argument=argument),
        path=paths.runtime_socket_path(),
        timeout=timeout,
        cancellation=cancellation,
    )


def send_runtime_on(
    connector: str,
    verb: str,
    argument: str | None = None,
    *,
    timeout: float | None = None,
    cancellation: Cancellation | None = None,
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
    if cancellation is None:
        return send_runtime(
            "on",
            wire,
            timeout=RUNTIME_ACTION_TIMEOUT if timeout is None else timeout,
        )
    return send_runtime(
        "on",
        wire,
        timeout=RUNTIME_ACTION_TIMEOUT if timeout is None else timeout,
        cancellation=cancellation,
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
