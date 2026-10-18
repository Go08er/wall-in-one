"""A Wayland compositor that is only a display inventory, for the RSS check.

    fake-wayland-outputs.py <socket> <count> <ready-file>

Serves <count> outputs named OUT-0 ... OUT-<count-1>, side by side at
1920x1080 and 60 Hz, over wl_output (version 4) and zxdg_output_manager_v1
(version 3), on a socket of the check's own. The service's display discovery
then runs as it does on a desktop: one connection, a registry, the outputs
bound, their xdg-outputs asked for, a sync every probe. Nothing else exists
here: no surfaces, no input, no other global. Writes READY to <ready-file>
once it listens; serves until it is killed.
"""

from __future__ import annotations

import selectors
import socket
import struct
import sys

DISPLAY = 1
XDG_GLOBAL = 1
FIRST_OUTPUT_GLOBAL = 100
WIDTH, HEIGHT, REFRESH = 1920, 1080, 60000


def event(object_id: int, opcode: int, *arguments: int | str) -> bytes:
    body = b""
    for argument in arguments:
        if isinstance(argument, str):
            text = argument.encode() + b"\0"
            body += struct.pack("<I", len(text)) + text + b"\0" * (-len(text) % 4)
        else:
            body += struct.pack("<i" if argument < 0 else "<I", argument)
    return struct.pack("<II", object_id, (8 + len(body)) << 16 | opcode) + body


class Client:
    def __init__(self, connection: socket.socket, count: int) -> None:
        self.connection = connection
        self.count = count
        self.objects: dict[int, tuple[str, int]] = {}
        self.incoming = b""
        self.serial = 0

    def feed(self, data: bytes) -> None:
        self.incoming += data
        out = b""
        while len(self.incoming) >= 8:
            object_id, word = struct.unpack_from("<II", self.incoming)
            size = word >> 16
            if size < 8 or len(self.incoming) < size:
                break
            body = self.incoming[8:size]
            self.incoming = self.incoming[size:]
            out += self.handle(object_id, word & 0xFFFF, body)
        if out:
            self.connection.sendall(out)

    def handle(self, object_id: int, opcode: int, body: bytes) -> bytes:
        if object_id == DISPLAY:
            (new_id,) = struct.unpack_from("<I", body)
            if opcode == 0:  # sync
                self.serial += 1
                return event(new_id, 0, self.serial) + event(DISPLAY, 1, new_id)
            if opcode == 1:  # get_registry
                self.objects[new_id] = ("registry", 0)
                out = b"".join(
                    event(new_id, 0, FIRST_OUTPUT_GLOBAL + index, "wl_output", 4)
                    for index in range(self.count)
                )
                return out + event(new_id, 0, XDG_GLOBAL, "zxdg_output_manager_v1", 3)
            return b""
        kind, index = self.objects.get(object_id, ("", 0))
        if kind == "registry" and opcode == 0:  # bind
            (name, length) = struct.unpack_from("<II", body)
            after = 8 + length + (-length % 4)
            _version, new_id = struct.unpack_from("<II", body, after)
            if name == XDG_GLOBAL:
                self.objects[new_id] = ("xdg-manager", 0)
                return b""
            output = name - FIRST_OUTPUT_GLOBAL
            if not 0 <= output < self.count:
                return b""
            self.objects[new_id] = ("output", output)
            connector = f"OUT-{output}"
            return (
                event(new_id, 0, WIDTH * output, 0, 530, 300, 0, "Fake", "Inventory", 0)
                + event(new_id, 1, 3, WIDTH, HEIGHT, REFRESH)
                + event(new_id, 3, 1)
                + event(new_id, 4, connector)
                + event(new_id, 5, f"Fake output {output}")
                + event(new_id, 2)
            )
        if kind == "xdg-manager" and opcode == 1:  # get_xdg_output
            new_id, output_id = struct.unpack_from("<II", body)
            _kind, output = self.objects.get(output_id, ("", 0))
            self.objects[new_id] = ("xdg-output", output)
            return (
                event(new_id, 0, WIDTH * output, 0)
                + event(new_id, 1, WIDTH, HEIGHT)
                + event(new_id, 3, f"OUT-{output}")
                + event(new_id, 4, f"Fake output {output}")
                # Version 3 ends the batch with wl_output.done.
                + event(output_id, 2)
            )
        if kind in ("output", "xdg-output", "xdg-manager") and opcode == 0:  # release/destroy
            del self.objects[object_id]
            return event(DISPLAY, 1, object_id)
        return b""


def main() -> None:
    path, count, ready = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(16)
    selector = selectors.DefaultSelector()
    selector.register(listener, selectors.EVENT_READ, None)
    with open(ready, "w", encoding="utf-8") as marker:
        marker.write("READY\n")
    while True:
        for key, _events in selector.select():
            if key.data is None:
                connection, _address = listener.accept()
                selector.register(connection, selectors.EVENT_READ, Client(connection, count))
                continue
            client: Client = key.data
            try:
                data = client.connection.recv(65536)
            except OSError:
                data = b""
            if not data:
                selector.unregister(client.connection)
                client.connection.close()
                continue
            try:
                client.feed(data)
            except OSError:
                selector.unregister(client.connection)
                client.connection.close()


if __name__ == "__main__":
    main()
