"""The keep-alive transport: same guarantees as urllib, fewer handshakes.

Nothing unprivileged can listen on 443, `require_https` refuses any other
port, and the suite forbids every IP connection, so these tests replace only
the connection factory: requests still go through `require_https`, the real
`http.client` framing, the bounded readers and the pool, but land on a
plain-HTTP/1.1 server listening on a Unix socket.
"""

from __future__ import annotations

import gzip
import socket
import socketserver
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from tests.test_providers_fakes import FrozenClock
from tests.test_providers_http import _Fixed
from wall_in_one.browse import Browser
from wall_in_one.providers import http
from wall_in_one.providers.base import ProviderError

Reply = tuple[int, dict[str, str], bytes]


@dataclass
class Server:
    """A loopback HTTP/1.1 server that counts connections and records requests."""

    routes: dict[str, Callable[[BaseHTTPRequestHandler], Reply | None]] = field(
        default_factory=dict
    )
    connections: int = 0
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    path: str = ""


class _UnixConnection(HTTPConnection):
    """`http.client` framing over a Unix socket instead of TCP."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def _serve(state: Server, path: Path) -> socketserver.ThreadingUnixStreamServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            pass

        def setup(self) -> None:
            super().setup()
            with state.lock:
                state.connections += 1

        def do_GET(self) -> None:
            with state.lock:
                state.requests.append((self.path, dict(self.headers.items())))
            route = state.routes.get(self.path)
            reply = route(self) if route is not None else (404, {}, b"missing")
            if reply is None:
                # The route took over the socket (e.g. hung up without a reply).
                self.close_connection = True
                return
            status, headers, body = reply
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    server.daemon_threads = True
    state.path = str(path)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    return server


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    state = Server()
    running = _serve(state, tmp_path / "http.sock")
    yield state
    running.shutdown()
    running.server_close()


def _client(
    server: Server, *, dialled: list[str] | None = None, **kwargs: object
) -> http.KeepAliveClient:
    def connect(host: str, timeout: float) -> HTTPConnection:
        if dialled is not None:
            dialled.append(host)
        return _UnixConnection(server.path, timeout)

    return http.KeepAliveClient(connect=connect, **kwargs)  # type: ignore[arg-type]


def _get(client: http.KeepAliveClient, url: str, max_bytes: int = 4096) -> http.Response:
    return client.fetch(http.Request(url=url, accept="*/*", timeout=5.0, max_bytes=max_bytes))


def _ok(body: bytes = b"hello", **headers: str) -> Callable[[BaseHTTPRequestHandler], Reply]:
    return lambda _handler: (200, {"Content-Type": "text/plain", **headers}, body)


# -- reuse -----------------------------------------------------------------


def test_sequential_requests_share_one_connection(server: Server) -> None:
    server.routes["/a"] = _ok(b"one")
    client = _client(server)

    bodies = [_get(client, "https://a.test/a").body for _ in range(3)]

    assert bodies == [b"one"] * 3
    assert server.connections == 1
    assert client.connections_opened == 1
    # Nothing tells the server to hang up after each response any more.
    assert all(headers.get("Connection") != "close" for _path, headers in server.requests)


def test_hosts_never_share_a_connection(server: Server) -> None:
    server.routes["/a"] = _ok()
    dialled: list[str] = []
    client = _client(server, dialled=dialled)

    _get(client, "https://a.test/a")
    _get(client, "https://b.test/a")
    _get(client, "https://a.test/a")

    assert dialled == ["a.test", "b.test"]


def test_a_body_that_was_not_read_to_the_end_is_never_reused(server: Server) -> None:
    server.routes["/big"] = _ok(b"x" * 2000)
    server.routes["/a"] = _ok()
    client = _client(server)

    with pytest.raises(ProviderError) as caught:
        _get(client, "https://a.test/big", max_bytes=100)
    assert caught.value.kind == "size-limit"
    assert _get(client, "https://a.test/a").body == b"hello"

    assert client.connections_opened == 2


def test_a_server_announcing_close_is_not_pooled(server: Server) -> None:
    server.routes["/a"] = _ok(Connection="close")
    client = _client(server)

    _get(client, "https://a.test/a")
    _get(client, "https://a.test/a")

    assert client.connections_opened == 2


def test_an_idle_connection_past_its_timeout_is_replaced(server: Server) -> None:
    server.routes["/a"] = _ok()
    clock = FrozenClock()
    client = _client(server, clock=clock)

    _get(client, "https://a.test/a")
    clock.sleep(http.IDLE_TIMEOUT_SECONDS + 1)
    _get(client, "https://a.test/a")

    assert client.connections_opened == 2


def test_a_stale_reused_connection_is_retried_once_on_a_fresh_one(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer_then_hang_up(handler: BaseHTTPRequestHandler) -> Reply:
        # Answer without announcing a close, then drop the socket: what a
        # server's keep-alive timeout looks like from the client's side.
        handler.close_connection = True
        return 200, {"Content-Type": "text/plain"}, b"hello"

    server.routes["/a"] = answer_then_hang_up
    client = _client(server)
    _get(client, "https://a.test/a")
    # Pretend the liveness probe raced the server's close and missed it.
    monkeypatch.setattr(http, "_idle_connection_usable", lambda _connection: True)

    assert _get(client, "https://a.test/a").body == b"hello"
    assert client.connections_opened == 2


def test_a_fresh_connection_failing_is_not_retried(server: Server) -> None:
    server.routes["/hangup"] = lambda _handler: None
    client = _client(server)

    with pytest.raises(ProviderError) as caught:
        _get(client, "https://a.test/hangup")

    assert caught.value.kind == "transport"
    assert client.connections_opened == 1
    assert len(server.requests) == 1


def test_a_redirect_is_returned_and_its_connection_survives(server: Server) -> None:
    server.routes["/old"] = lambda _handler: (302, {"Location": "https://a.test/new"}, b"moved")
    server.routes["/new"] = _ok(b"new")
    client = _client(server)

    first = _get(client, "https://a.test/old")
    assert first.is_redirect and first.location == "https://a.test/new"
    assert _get(client, "https://a.test/new").body == b"new"

    assert [path for path, _headers in server.requests] == ["/old", "/new"]
    assert client.connections_opened == 1


def test_a_redirected_download_drains_and_reuses(server: Server, tmp_path: Path) -> None:
    server.routes["/dl"] = lambda _handler: (302, {"Location": "https://a.test/file"}, b"moved")
    server.routes["/file"] = _ok(b"payload", **{"Content-Type": "application/octet-stream"})
    client = _client(server)
    request = http.Request(url="https://a.test/dl", accept="*/*", timeout=5.0, max_bytes=64)

    with client.download(request, tmp_path) as moved:
        assert moved.is_redirect and moved.path is None
    final = http.Request(url="https://a.test/file", accept="*/*", timeout=5.0, max_bytes=64)
    with client.download(final, tmp_path) as transfer:
        assert transfer.path is not None and transfer.path.read_bytes() == b"payload"

    assert client.connections_opened == 1


# -- what is (and is not) sent ---------------------------------------------


def test_hostile_urls_never_reach_the_connector(server: Server) -> None:
    dialled: list[str] = []
    client = _client(server, dialled=dialled)

    for url in ("http://a.test/", "https://a.test:8443/", "https://u:p@a.test/"):
        with pytest.raises(ProviderError) as caught:
            _get(client, url)
        assert caught.value.kind == "invalid-url"

    assert dialled == []


def test_headers_match_the_urllib_transport(server: Server) -> None:
    server.routes["/a?q=1"] = _ok()
    client = _client(server)

    client.fetch(
        http.Request(
            url="https://a.test/a?q=1#frag",
            accept="application/json",
            timeout=5.0,
            max_bytes=64,
            headers=(("X-API-Key", "secret"),),
        )
    )

    path, headers = server.requests[0]
    assert path == "/a?q=1"
    assert headers["Host"] == "a.test"
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == http.USER_AGENT
    assert headers["X-API-Key"] == "secret"


def test_a_configured_proxy_keeps_the_urllib_path(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    dialled: list[str] = []

    class Opener:
        def __init__(self) -> None:
            self.opened: list[object] = []

        def open(self, request: object, timeout: float) -> _Fixed:
            self.opened.append(request)
            return _Fixed(b"via proxy", 200, {}, "https://a.test/")

    def dial(host: str, timeout: float) -> HTTPConnection:
        dialled.append(host)
        raise AssertionError("a proxied request must not dial the host directly")

    opener = Opener()
    client = http.KeepAliveClient(connect=dial, opener=opener)  # type: ignore[arg-type]

    assert _get(client, "https://a.test/").body == b"via proxy"
    assert dialled == []
    assert len(opener.opened) == 1


# -- compression -----------------------------------------------------------


def _gzip_route(body: bytes) -> Callable[[BaseHTTPRequestHandler], Reply]:
    def route(handler: BaseHTTPRequestHandler) -> Reply:
        if "gzip" not in handler.headers.get("Accept-Encoding", ""):
            return 200, {"Content-Type": "text/html"}, body
        return 200, {"Content-Type": "text/html", "Content-Encoding": "gzip"}, gzip.compress(body)

    return route


def test_fetch_asks_for_gzip_and_decodes_it(server: Server) -> None:
    page = b"<html>" + b"card " * 2000 + b"</html>"
    server.routes["/page"] = _gzip_route(page)
    client = _client(server)

    assert _get(client, "https://a.test/page", max_bytes=len(page)).body == page
    assert server.requests[0][1]["Accept-Encoding"] == "gzip"


def test_compression_can_be_switched_off(server: Server) -> None:
    server.routes["/page"] = _gzip_route(b"<html></html>")
    client = _client(server, compress=False)

    assert _get(client, "https://a.test/page").body == b"<html></html>"
    assert server.requests[0][1]["Accept-Encoding"] == "identity"


def test_downloads_never_ask_for_compression(server: Server, tmp_path: Path) -> None:
    server.routes["/file"] = _gzip_route(b"bytes on disk must match the metadata")
    client = _client(server)
    request = http.Request(url="https://a.test/file", accept="*/*", timeout=5.0, max_bytes=64)

    with client.download(request, tmp_path) as transfer:
        assert transfer.path is not None
        assert transfer.path.read_bytes() == b"bytes on disk must match the metadata"

    assert server.requests[0][1]["Accept-Encoding"] == "identity"


def test_a_gzip_bomb_stops_at_the_ceiling(server: Server) -> None:
    bomb = gzip.compress(b"\0" * (4 * 1024 * 1024))
    server.routes["/bomb"] = lambda _handler: (200, {"Content-Encoding": "gzip"}, bomb)
    client = _client(server)

    with pytest.raises(ProviderError) as caught:
        _get(client, "https://a.test/bomb", max_bytes=64 * 1024)

    assert caught.value.kind == "size-limit"
    assert len(bomb) < 64 * 1024  # the wire size alone would have passed


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        ("gzip", b"not gzip at all"),
        ("gzip", gzip.compress(b"truncated")[:-4]),
        ("gzip", gzip.compress(b"one") + b"trailing"),
        ("br", b"\x0b\x02\x80hello\x03"),
    ],
)
def test_an_undecodable_body_is_a_response_error(
    server: Server, encoding: str, body: bytes
) -> None:
    server.routes["/x"] = lambda _handler: (200, {"Content-Encoding": encoding}, body)
    client = _client(server)

    with pytest.raises(ProviderError) as caught:
        _get(client, "https://a.test/x")

    assert caught.value.kind == "response"


# -- lifecycle -------------------------------------------------------------


def test_close_drops_idle_connections_and_refuses_new_requests(server: Server) -> None:
    server.routes["/a"] = _ok()
    client = _client(server)
    _get(client, "https://a.test/a")

    client.close()

    assert client.cancelled()
    assert client._idle == {}
    with pytest.raises(ProviderError) as caught:
        _get(client, "https://a.test/a")
    assert caught.value.kind == "cancelled"


def test_production_code_builds_the_keep_alive_transport() -> None:
    assert isinstance(http.default_client(), http.KeepAliveClient)
    assert isinstance(Browser()._client, http.KeepAliveClient)
