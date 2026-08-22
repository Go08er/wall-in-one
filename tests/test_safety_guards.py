"""Prove the suite-wide guards fail before touching the host."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from wall_in_one.theme import noctalia


def test_live_network_connections_are_refused_before_the_socket_connects() -> None:
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="live network"):
            connection.connect(("127.0.0.1", 9))
    finally:
        connection.close()


def test_unix_control_sockets_remain_available_to_offline_tests(tmp_path: Path) -> None:
    path = tmp_path / "local.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(1)
        client.connect(str(path))
        accepted, _address = listener.accept()
        accepted.close()
    finally:
        client.close()
        listener.close()


def test_direct_noctalia_messages_cannot_mutate_the_desktop() -> None:
    with pytest.raises(AssertionError, match="changes the live desktop"):
        noctalia.message("color-scheme-set", "builtin", "Nord")
