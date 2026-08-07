"""Control socket server.

Runs inside the GTK main loop via `Gio.SocketService`, so handlers execute on
the main thread and can touch the UI directly without locking.

That is the whole difficulty with `search` and `download`: a handler that waits
for a website on this thread freezes every frame the app draws, for seconds or
for minutes. So a handler may answer with a `Deferred` instead of a `Response`,
which hands the reply to a callback later; the connection simply stays open
until then. `ui.browse_dialog` already puts provider calls on a worker pool and
comes back through `GLib.idle_add`, and the application's implementation of
these verbs does exactly the same -- none of that machinery lives here, because
this module has to stay importable and testable with no display attached.

The verb table is kept separate from the transport (`Commands`) so it can be
tested without a socket or a display.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from wall_in_one import paths
from wall_in_one.control.protocol import ProtocolError, Request, Response
from wall_in_one.providers import registry
from wall_in_one.providers.base import ProviderError, SearchResult

#: Handed a response, exactly once, whenever it turns up.
Reply = Callable[[Response], None]


@dataclass(frozen=True, slots=True)
class Deferred:
    """An answer that is not ready yet.

    `start` is called with a `Reply` and must arrange for it to be invoked once,
    back on the main thread, when the answer arrives. Returning one of these is
    a handler saying "not on this thread": the caller keeps the client's
    connection open and writes whatever the reply eventually carries.

    A `start` that never calls its reply leaves that client waiting until its
    own timeout, which is the price of not blocking the compositor's idea of a
    responsive window.
    """

    start: Callable[[Reply], None]


#: What a handler answers with: a response now, or the promise of one later.
Outcome = Response | Deferred

Handler = Callable[[str | None], Outcome]


class Commands(Protocol):
    """What the application must provide for the control surface to work.

    Mirrors the Noctalia plugin's control list: launch, transport, shuffle,
    cycle, cycle duration, and the dynamics pause. The three browsing verbs are
    not on the plugin's list -- they exist so that searching for a wallpaper and
    pulling it into the library are reachable from a terminal or a script
    without opening the window.
    """

    def next_wallpaper(self) -> Response: ...
    def previous_wallpaper(self) -> Response: ...
    def random_wallpaper(self) -> Response: ...
    def set_shuffle(self, value: str | None) -> Response: ...
    def set_cycle(self, value: str | None) -> Response: ...
    def set_cycle_interval(self, value: str | None) -> Response: ...
    def set_dynamics(self, value: str | None) -> Response: ...
    def reload_palette(self) -> Response: ...
    def report_status(self) -> Response: ...
    def list_providers(self) -> Response: ...
    def search(self, value: str | None) -> Outcome: ...
    def download(self, value: str | None) -> Outcome: ...
    def quit(self) -> Response: ...


def build_verb_table(commands: Commands) -> dict[str, Handler]:
    return {
        "next": lambda _: commands.next_wallpaper(),
        "prev": lambda _: commands.previous_wallpaper(),
        "random": lambda _: commands.random_wallpaper(),
        "shuffle": commands.set_shuffle,
        "cycle": commands.set_cycle,
        "cycle-interval": commands.set_cycle_interval,
        "dynamics": commands.set_dynamics,
        "reload-palette": lambda _: commands.reload_palette(),
        "status": lambda _: commands.report_status(),
        "providers": lambda _: commands.list_providers(),
        "search": commands.search,
        "download": commands.download,
        "quit": lambda _: commands.quit(),
    }


def parse_toggle(value: str | None, current: bool) -> bool:
    """Interpret ``on``/``off``/``toggle`` (and the usual synonyms)."""
    if value is None or value == "toggle":
        return not current
    lowered = value.strip().lower()
    if lowered in ("on", "true", "1", "yes", "enable", "enabled"):
        return True
    if lowered in ("off", "false", "0", "no", "disable", "disabled"):
        return False
    raise ValueError(f"expected on, off or toggle, got {value!r}")


def parse_search(value: str | None) -> tuple[str, str]:
    """Split ``<provider> [query]``.

    The query is everything after the first word, spaces and all, so that
    quoting it is optional at a shell prompt. An empty query is not an error:
    both providers answer it with whatever they are showing today.
    """
    parts = (value or "").split(maxsplit=1)
    if not parts:
        raise ValueError("usage: search <provider> [query]")
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def parse_download(value: str | None) -> tuple[str, str, str]:
    """Split ``<provider> <identifier> [variant]``.

    The variant is MotionBGS's `hd` or `4k`. Left off, the provider takes the
    best it is offered, which is what the browse dialog's button does too.
    """
    parts = (value or "").split()
    if not 2 <= len(parts) <= 3:
        raise ValueError("usage: download <provider> <identifier> [variant]")
    return parts[0], parts[1], parts[2] if len(parts) == 3 else ""


# -- rendering for a terminal --------------------------------------------

#: Rows are one line each with tab-separated fields, because that is the format
#: both readers of this output already understand: a person sees columns, and
#: `cut -f1`, `awk -F'\t'` and `while read -r id kind rest` all get the fields
#: out with nothing installed. Tabs rather than spaces because a wallpaper title
#: is full of spaces and would otherwise read as several columns.
SEPARATOR: Final = "\t"

#: Everything that is not a row -- the summary, the column names -- is a comment
#: line, which is the one convention those same tools already know how to skip.
COMMENT: Final = "# "

#: Stands in for a field the provider left empty, so a column is never
#: invisible.
BLANK: Final = "-"


def _field(text: str) -> str:
    """One column's worth of text from an untrusted title.

    Tabs separate the columns and newlines separate the rows, so neither may
    survive inside a string a website chose; a title carrying either would
    otherwise invent columns or whole rows in a script's input.
    """
    collapsed = " ".join(text.split())
    return collapsed or BLANK


def render_providers(infos: Sequence[registry.ProviderInfo]) -> str:
    """The provider list, with what each one cannot currently do."""
    lines = [f"{COMMENT}fields: name, media, usable, limitations"]
    lines.extend(
        SEPARATOR.join(
            (
                _field(info.name),
                _field(info.media_kind.value),
                "yes" if info.usable else "no",
                # Semicolons rather than a row each: the name in column one has
                # to stay the key of the line it is on.
                _field("; ".join(info.limitations)),
            )
        )
        for info in infos
    )
    return "\n".join(lines)


def summarise(result: SearchResult) -> str:
    """The same sentence the browse dialog puts under its grid.

    Deliberately word-for-word: `dropped` counts results the provider returned
    that we refused to normalise, and a sudden jump in it means the remote's
    markup moved. That is worth noticing from a script too.
    """
    parts = [f"{len(result)} result{'' if len(result) == 1 else 's'}"]
    if result.total_hint:
        parts.append(f"of about {result.total_hint}")
    parts.append(f"page {result.page}")
    if result.dropped:
        parts.append(f"{result.dropped} unreadable")
    if result.cached:
        parts.append("cached")
    return " - ".join(parts)


def render_search(result: SearchResult) -> str:
    """One page of results: a summary, the column names, then a row each.

    The identifier comes first because it is the field with a use -- it is what
    `download` takes back.
    """
    lines = [
        f"{COMMENT}{result.provider}: {summarise(result)}",
        f"{COMMENT}fields: identifier, kind, resolution, title",
    ]
    lines.extend(
        SEPARATOR.join(
            (
                _field(item.identifier),
                _field(item.kind.value),
                _field(item.resolution),
                _field(item.title or item.identifier),
            )
        )
        for item in result.items
    )
    return "\n".join(lines)


# -- dispatch ------------------------------------------------------------


def failed(error: Exception) -> Response:
    """One failed response for any exception, keeping a provider's reason.

    A `ProviderError` is the interesting case and the reason this exists: its
    `kind` is the machine-readable half the browse dialog branches on, and a
    caller holding a socket deserves the same. Its `str` already reads
    ``kind: message``, which is the sentence the dialog toasts, so the client
    prints one thing and switches on another.
    """
    if isinstance(error, ProviderError):
        return Response.failure(str(error), kind=error.kind)
    if isinstance(error, ValueError):
        # Argument validation. The message is the whole point of it.
        return Response.failure(str(error))
    # Anything else is a bug or a broken machine, and the type name is the only
    # part of it a person can act on.
    return Response.failure(f"{type(error).__name__}: {error}")


def handle(verbs: dict[str, Handler], line: bytes) -> Outcome:
    """Decode one request line and run it. Never raises."""
    try:
        request = Request.decode(line)
    except ProtocolError as error:
        return Response.failure(str(error))

    handler = verbs.get(request.verb)
    if handler is None:
        known = ", ".join(sorted(verbs))
        return Response.failure(f"unknown verb {request.verb!r}; known verbs: {known}")

    try:
        return handler(request.argument)
    except Exception as error:
        # Broad on purpose: a handler must never take the app down, and an
        # unreachable network raises whatever the transport underneath felt like.
        return failed(error)


def dispatch(verbs: dict[str, Handler], line: bytes, reply: Reply) -> None:
    """Run one request and hand its answer to ``reply``, exactly once.

    ``reply`` runs before this returns for every verb that can answer at once,
    and some time later for the ones that go to the network. Guarding the
    once-ness here rather than trusting each handler keeps a double reply -- two
    responses down a connection framed one-per-line -- impossible by
    construction.
    """
    outcome = handle(verbs, line)
    if isinstance(outcome, Response):
        reply(outcome)
        return

    answered = False

    def once(response: Response) -> None:
        nonlocal answered
        if answered:
            return
        answered = True
        reply(response)

    try:
        outcome.start(once)
    except Exception as error:
        # A deferral that fails to even start still owes the client an answer.
        once(failed(error))


class SocketServer:
    """Binds the control socket and answers requests from the GTK main loop."""

    #: Backlog is small on purpose: clients are one-shot `ctl` invocations.
    BACKLOG: Final = 8

    def __init__(self, commands: Commands, path: Path | None = None) -> None:
        self._verbs = build_verb_table(commands)
        self._path = path if path is not None else paths.socket_path()
        self._service: object | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _clear_stale_socket(self) -> None:
        """Remove a socket left behind by a crashed instance.

        Only when nothing answers on it -- a live socket means another instance
        is running and we must not steal its address.
        """
        if not self._path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(self._path))
        except (ConnectionRefusedError, FileNotFoundError):
            self._path.unlink(missing_ok=True)
            return
        except OSError:
            self._path.unlink(missing_ok=True)
            return
        else:
            raise RuntimeError(f"another instance is already listening on {self._path}")
        finally:
            probe.close()

    def start(self) -> None:
        from gi.repository import Gio, GLib

        paths.ensure_directory(self._path.parent)
        self._clear_stale_socket()

        service = Gio.SocketService.new()
        address = Gio.UnixSocketAddress.new(str(self._path))
        try:
            service.add_address(
                address,
                Gio.SocketType.STREAM,
                Gio.SocketProtocol.DEFAULT,
                None,
            )
        except GLib.Error as error:
            raise RuntimeError(f"cannot bind {self._path}: {error.message}") from error

        service.connect("incoming", self._on_incoming)
        service.start()
        self._service = service
        # The socket carries control of the wallpaper; no reason for anyone
        # else on the system to reach it.
        os.chmod(self._path, 0o600)

    def stop(self) -> None:
        service = self._service
        if service is not None:
            service.stop()  # type: ignore[attr-defined]
            self._service = None
        self._path.unlink(missing_ok=True)

    def _on_incoming(self, _service: object, connection: object, _source: object) -> bool:
        from gi.repository import Gio

        assert isinstance(connection, Gio.SocketConnection)
        stream = Gio.DataInputStream.new(connection.get_input_stream())
        stream.set_close_base_stream(True)
        stream.read_line_async(0, None, self._on_line, connection)
        # True keeps the connection alive past this callback; the async read
        # owns it from here.
        return True

    def _on_line(self, stream: object, result: object, connection: object) -> None:
        from gi.repository import Gio, GLib

        assert isinstance(stream, Gio.DataInputStream)
        assert isinstance(connection, Gio.SocketConnection)
        try:
            line, _length = stream.read_line_finish(result)  # type: ignore[arg-type]
        except GLib.Error:
            line = None

        if line is None:
            self._answer(connection, Response.failure("empty request"))
            return
        # `dispatch` writes the reply itself, which for a search or a download
        # happens once the worker running it has come back to this thread. The
        # connection is held open in the meantime and closed by `_answer`.
        dispatch(self._verbs, bytes(line), lambda response: self._answer(connection, response))

    def _answer(self, connection: object, response: Response) -> None:
        from gi.repository import Gio, GLib

        assert isinstance(connection, Gio.SocketConnection)
        try:
            payload = response.encode()
        except ProtocolError as error:
            # A reply too large for the frame: say so rather than send half of
            # it, which the client would read as a malformed message.
            payload = Response.failure(f"reply could not be sent: {error}").encode()
        try:
            connection.get_output_stream().write_all(payload, None)
        except GLib.Error:
            # The client hung up before reading; nothing useful to do.
            pass
        finally:
            with contextlib.suppress(GLib.Error):
                connection.close(None)
