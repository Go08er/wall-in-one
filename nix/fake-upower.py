"""Minimal UPower provider for the isolated runtime resource gate.

Only connects to the explicitly supplied disposable bus. It owns no desktop
resources and exposes the one authoritative property used by the runtime.
"""

from __future__ import annotations

import ctypes as c
import signal
import sys


class MessageIter(c.Structure):
    # Stable public DBusMessageIter ABI (dbus-message.h), as in power.rs.
    _fields_ = [
        ("pointers", c.c_void_p * 2),
        ("serial", c.c_uint),
        ("integers", c.c_int * 9),
        ("padding", c.c_void_p * 2),
    ]


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[3] not in ("ac", "battery"):
        raise SystemExit("usage: fake-upower.py LIBDBUS PRIVATE_BUS_ADDRESS ac|battery")
    library, address, source = sys.argv[1:]
    if not address.startswith("unix:"):
        raise SystemExit("the resource fixture requires an explicit private Unix bus")
    dbus = c.CDLL(library)

    def bind(name: str, result: object, *arguments: object) -> object:
        function = getattr(dbus, name)
        function.restype = result
        function.argtypes = arguments
        return function

    open_private = bind("dbus_connection_open_private", c.c_void_p, c.c_char_p, c.c_void_p)
    register = bind("dbus_bus_register", c.c_uint, c.c_void_p, c.c_void_p)
    request_name = bind(
        "dbus_bus_request_name", c.c_int, c.c_void_p, c.c_char_p, c.c_uint, c.c_void_p
    )
    exit_on_disconnect = bind("dbus_connection_set_exit_on_disconnect", None, c.c_void_p, c.c_uint)
    read_write = bind("dbus_connection_read_write", c.c_uint, c.c_void_p, c.c_int)
    pop_message = bind("dbus_connection_pop_message", c.c_void_p, c.c_void_p)
    is_method_call = bind(
        "dbus_message_is_method_call", c.c_uint, c.c_void_p, c.c_char_p, c.c_char_p
    )
    get_path = bind("dbus_message_get_path", c.c_char_p, c.c_void_p)
    iter_init = bind("dbus_message_iter_init", c.c_uint, c.c_void_p, c.POINTER(MessageIter))
    iter_type = bind("dbus_message_iter_get_arg_type", c.c_int, c.POINTER(MessageIter))
    iter_basic = bind("dbus_message_iter_get_basic", None, c.POINTER(MessageIter), c.c_void_p)
    iter_next = bind("dbus_message_iter_next", c.c_uint, c.POINTER(MessageIter))
    method_return = bind("dbus_message_new_method_return", c.c_void_p, c.c_void_p)
    method_error = bind("dbus_message_new_error", c.c_void_p, c.c_void_p, c.c_char_p, c.c_char_p)
    init_append = bind("dbus_message_iter_init_append", None, c.c_void_p, c.POINTER(MessageIter))
    open_container = bind(
        "dbus_message_iter_open_container",
        c.c_uint,
        c.POINTER(MessageIter),
        c.c_int,
        c.c_char_p,
        c.POINTER(MessageIter),
    )
    append_basic = bind(
        "dbus_message_iter_append_basic", c.c_uint, c.POINTER(MessageIter), c.c_int, c.c_void_p
    )
    close_container = bind(
        "dbus_message_iter_close_container",
        c.c_uint,
        c.POINTER(MessageIter),
        c.POINTER(MessageIter),
    )
    send = bind("dbus_connection_send", c.c_uint, c.c_void_p, c.c_void_p, c.c_void_p)
    flush = bind("dbus_connection_flush", None, c.c_void_p)
    unref_message = bind("dbus_message_unref", None, c.c_void_p)
    close = bind("dbus_connection_close", None, c.c_void_p)
    unref_connection = bind("dbus_connection_unref", None, c.c_void_p)

    def requested_property(message: int) -> bool:
        iterator = MessageIter()
        if not iter_init(message, c.byref(iterator)):
            return False
        for index, expected in enumerate((b"org.freedesktop.UPower", b"OnBattery")):
            if iter_type(c.byref(iterator)) != ord("s"):
                return False
            value = c.c_char_p()
            iter_basic(c.byref(iterator), c.byref(value))
            if value.value != expected:
                return False
            more = bool(iter_next(c.byref(iterator)))
            if more != (index == 0):
                return False
        return True

    connection = open_private(address.encode(), None)
    if not connection:
        raise RuntimeError("could not connect to the disposable bus")
    exit_on_disconnect(connection, 0)
    running = True

    def stop(_signal: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if not register(connection, None):
            raise RuntimeError("could not register on the disposable bus")
        # DO_NOT_QUEUE: a duplicate fixture must fail, not pretend readiness.
        if request_name(connection, b"org.freedesktop.UPower", 4, None) != 1:
            raise RuntimeError("could not own the disposable UPower name")
        print("READY", flush=True)
        while running:
            if not read_write(connection, 100):
                raise RuntimeError("the disposable bus disconnected unexpectedly")
            message = pop_message(connection)
            if not message:
                continue
            try:
                if not is_method_call(message, b"org.freedesktop.DBus.Properties", b"Get"):
                    continue
                valid = get_path(message) == b"/org/freedesktop/UPower" and requested_property(
                    message
                )
                reply = (
                    method_return(message)
                    if valid
                    else method_error(
                        message,
                        b"org.freedesktop.DBus.Error.InvalidArgs",
                        b"expected UPower.OnBattery",
                    )
                )
                if not reply:
                    raise RuntimeError("could not allocate a fixture reply")
                try:
                    if valid:
                        outer, inner = MessageIter(), MessageIter()
                        value = c.c_uint(source == "battery")
                        init_append(reply, c.byref(outer))
                        if not (
                            open_container(c.byref(outer), ord("v"), b"b", c.byref(inner))
                            and append_basic(c.byref(inner), ord("b"), c.byref(value))
                            and close_container(c.byref(outer), c.byref(inner))
                        ):
                            raise RuntimeError("could not encode a fixture reply")
                    if not send(connection, reply, None):
                        raise RuntimeError("could not send a fixture reply")
                    flush(connection)
                finally:
                    unref_message(reply)
            finally:
                unref_message(message)
    finally:
        close(connection)
        unref_connection(connection)


if __name__ == "__main__":
    main()
