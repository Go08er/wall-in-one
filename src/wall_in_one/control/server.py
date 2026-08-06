"""Control socket server.

Runs inside the GTK main loop via `Gio.SocketService`, so handlers execute on
the main thread and can touch the UI directly without locking.

The verb table is kept separate from the transport (`Commands`) so it can be
tested without a socket or a display.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol

from wall_in_one import paths
from wall_in_one.control.protocol import ProtocolError, Request, Response

Handler = Callable[[str | None], Response]


class Commands(Protocol):
    """What the application must provide for the control surface to work.

    Mirrors the Noctalia plugin's control list: launch, transport, shuffle,
    cycle, cycle duration, and the dynamics pause.
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


def handle(verbs: dict[str, Handler], line: bytes) -> Response:
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
    except ValueError as error:
        return Response.failure(str(error))
    except Exception as error:
        return Response.failure(f"{type(error).__name__}: {error}")


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
        try:
            line, _length = stream.read_line_finish(result)  # type: ignore[arg-type]
        except GLib.Error:
            line = None

        response = (
            handle(self._verbs, bytes(line))
            if line is not None
            else Response.failure("empty request")
        )

        assert isinstance(connection, Gio.SocketConnection)
        try:
            connection.get_output_stream().write_all(response.encode(), None)
        except GLib.Error:
            # The client hung up before reading; nothing useful to do.
            pass
        finally:
            with contextlib.suppress(GLib.Error):
                connection.close(None)
