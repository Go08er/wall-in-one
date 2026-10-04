"""The one place this package touches the network.

Every provider request goes through a :class:`Client`, so a test substitutes
one object and the whole package is offline. Nothing else under `providers/`
imports `urllib`, `socket` or `subprocess`, and `test_providers_http` asserts
that rather than trusting it.

The implementation this was lifted from shelled out to `curl`, and got
`--max-filesize`, `--proto =https`, `--max-redirs 0` and `--speed-limit` for
free. Those are re-earned here rather than kept:

* the size ceiling is enforced while reading, which is stricter than
  `--max-filesize` because it also catches a lying `Content-Length`;
* the scheme check is :func:`require_https`, applied before a socket opens;
* redirects are never followed -- a 3xx comes back as a response carrying a
  `location` and the provider decides, which is how a cross-origin redirect
  fails closed;
* `--speed-limit`/`--speed-time` becomes the socket timeout, which urllib
  applies per read, so a stalled transfer still dies on schedule.

What is deliberately *not* re-earned is the reason the Wallhaven key was passed
as `--header @file`: curl put every other header in `argv`, where any local
process could read it. There is no `argv` now, so the key is a plain string.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import select
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message as HTTPMessage
from pathlib import Path
from types import TracebackType
from typing import IO, BinaryIO, Final, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from wall_in_one import file_io
from wall_in_one.providers import download as download_files
from wall_in_one.providers.base import ProviderError

#: Sent on every request. Identifying the client honestly is the price of using
#: someone else's public API.
USER_AGENT: Final = "wall-in-one/0.2.2 (+https://github.com/Go08er/wall-in-one)"

#: Read granularity. Large enough that a 64 MiB image is not a million calls,
#: small enough that the ceiling is enforced promptly.
CHUNK_BYTES: Final = 256 * 1024

#: Nothing this package pulls *into memory* is legitimately larger.
DEFAULT_MAX_BYTES: Final = 1024 * 1024

#: Handed back to the caller rather than followed.
REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

#: Staged downloads are dot-files so a library scan already ignores them if one
#: is ever left behind by a hard kill.
STAGING_PREFIX: Final = download_files.MEDIA_STAGING_PREFIX

# Media transfers retain their published 120/300-second *stall* ceilings, but
# establishing a socket is a separate operation.  A UI shutdown cannot close a
# response stream which urllib has not returned yet, so the connect/TLS phase
# needs its own honest bound rather than inheriting a five-minute download
# timeout.
CONNECT_TIMEOUT_SECONDS: Final = 5.0

#: Idle keep-alive connections retained per host. Equal to the preview worker
#: count, so a page of thumbnails reuses connections rather than paying a TCP
#: and TLS handshake for every card.
MAX_IDLE_PER_HOST: Final = 4

#: An idle connection older than this is closed instead of reused. Inside
#: nginx's 75-second default, and shorter than a browser keeps one (Firefox:
#: 115 s). A connection the server closed sooner is caught by the liveness
#: probe on checkout, or at worst by the single retry in `KeepAliveClient`.
IDLE_TIMEOUT_SECONDS: Final = 60.0

#: A redirect or error body no larger than this is read to its end so the
#: connection can carry the next request. Anything bigger closes it instead.
DRAIN_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class Request:
    """One bounded GET. There is no other verb; providers only read."""

    url: str
    accept: str
    timeout: float
    max_bytes: int
    #: Extra request headers, as pairs so the request stays hashable.
    headers: tuple[tuple[str, str], ...] = ()
    user_agent: str = USER_AGENT


@dataclass(frozen=True, slots=True)
class Response:
    """A body small enough to hold in memory, and what framed it."""

    url: str
    status: int
    content_type: str
    body: bytes
    #: Only meaningful when :attr:`is_redirect`.
    location: str = ""

    @property
    def is_redirect(self) -> bool:
        return self.status in REDIRECT_STATUSES


@dataclass(frozen=True, slots=True)
class Transfer:
    """A body streamed to a temporary file, because it may be hundreds of MiB.

    ``path`` is a file in the directory the caller nominated and belongs to the
    caller: install it or :meth:`discard` it. A hidden shared capability keeps
    that exact inode pinned across provider wrappers until commit or cleanup.
    It is ``None`` when the remote answered with a redirect or an error, where
    there is no body worth staging.
    """

    url: str
    status: int
    content_type: str
    size: int
    path: Path | None = None
    location: str = ""
    _staged_pin: file_io.PinnedPath | None = field(default=None, repr=False, compare=False)
    _staged_fingerprint: file_io.FileFingerprint | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _staged_file: download_files._StagedFile | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _released: bool = field(default=False, init=False, repr=False, compare=False)
    _release_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.path is None:
            if self._staged_pin is not None or self._staged_fingerprint is not None:
                if self._staged_pin is not None:
                    self._staged_pin.close()
                raise ValueError("a bodyless transfer cannot retain a staged file")
            return
        staged_file = download_files._retain_staged_file(
            self.path,
            pinned_source=self._staged_pin,
            expected_fingerprint=self._staged_fingerprint,
        )
        try:
            validation_path = download_files._validation_path(staged_file)
        except BaseException:
            download_files._release_staged_file(staged_file)
            raise
        object.__setattr__(self, "_staged_file", staged_file)
        object.__setattr__(self, "path", validation_path)

    @property
    def is_redirect(self) -> bool:
        return self.status in REDIRECT_STATUSES

    def discard(self) -> None:
        self._release(discard=True)

    def _release(self, *, discard: bool) -> None:
        with self._release_lock:
            if self._released:
                return
            object.__setattr__(self, "_released", True)
            staged_file = self._staged_file
            if staged_file is None:
                return
            try:
                if discard:
                    download_files._consume_staged_file(staged_file, discard=True)
            finally:
                download_files._release_staged_file(staged_file)

    def __enter__(self) -> Transfer:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.discard()

    def __del__(self) -> None:
        # Destructors cannot report a cleanup failure. Explicit discard or a
        # context manager remains the observable cleanup path.
        with contextlib.suppress(Exception):
            self._release(discard=False)


class Client(Protocol):
    """The seam. Tests implement this; in production `KeepAliveClient` does."""

    def fetch(self, request: Request) -> Response: ...

    def download(self, request: Request, directory: Path) -> Transfer: ...


def require_https(url: str) -> str:
    """Reject anything that is not a plain HTTPS URL, before a socket opens.

    A provider has already validated the URL against its own origin by the time
    it gets here. This is the backstop for the case where one has not, and for
    a `Location` header a provider forgot to re-check.
    """
    if not url or len(url) > 2048:
        raise ProviderError("invalid-url", "request URL is empty or too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ProviderError("invalid-url", "request URL contains control characters")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ProviderError("invalid-url", "request URL has an invalid port") from error
    if parsed.scheme != "https":
        raise ProviderError("invalid-url", "only HTTPS requests are made")
    if not parsed.hostname:
        raise ProviderError("invalid-url", "request URL has no host")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderError("invalid-url", "request URL carries credentials")
    if port not in (None, 443):
        raise ProviderError("invalid-url", "request URL uses a non-standard port")
    return url


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into a response the caller has to think about."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        # Widened to the base signature's return type: returning None is what
        # declines the redirect and hands the 3xx back to the caller.
        return None


@dataclass(slots=True)
class _Opened:
    status: int
    url: str
    content_type: str
    location: str
    declared_length: int
    stream: BinaryIO
    content_encoding: str = ""
    #: The pooled connection carrying ``stream``; ``None`` on the urllib path.
    connection: http.client.HTTPConnection | None = None
    #: The pool key ``connection`` returns to.
    origin: str = ""


class UrllibClient:
    """The real transport. Stdlib only, as the rest of this app is."""

    def __init__(self, *, opener: urllib.request.OpenerDirector | None = None) -> None:
        self._opener = (
            opener
            if opener is not None
            else urllib.request.build_opener(_NoRedirects, urllib.request.HTTPSHandler)
        )
        self._lock = threading.Lock()
        self._closed = False
        self._active: dict[int, BinaryIO] = {}

    def cancelled(self) -> bool:
        """Whether this transport has been permanently closed by its owner."""
        with self._lock:
            return self._closed

    def close(self) -> None:
        """Refuse new requests and wake active response-body readers."""
        with self._lock:
            self._closed = True
            active = tuple(self._active.values())
            self._active.clear()
        for stream in active:
            _interrupt_stream(stream)

    def _register(self, stream: BinaryIO) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._active[id(stream)] = stream
            return True

    def _unregister(self, stream: BinaryIO) -> None:
        with self._lock:
            self._active.pop(id(stream), None)

    # -- transport -------------------------------------------------------

    def _open(self, request: Request, *, compressed: bool = False) -> _Opened:
        del compressed  # urllib always asks for an identity body
        url = require_https(request.url)
        if self.cancelled():
            raise ProviderError("cancelled", "request cancelled during shutdown")
        headers = {
            "Accept": request.accept,
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": request.user_agent,
            **dict(request.headers),
        }
        outgoing = urllib.request.Request(url, method="GET", headers=headers)
        try:
            raw = cast(
                "BinaryIO",
                self._opener.open(
                    outgoing,
                    timeout=min(request.timeout, CONNECT_TIMEOUT_SECONDS),
                ),
            )
        except urllib.error.HTTPError as error:
            # An HTTPError *is* the response, and a 3xx arrives here precisely
            # because redirects are not followed.
            raw = cast("BinaryIO", error)
        except TimeoutError as error:
            raise ProviderError("timeout", f"request to {url} timed out") from error
        except urllib.error.URLError as error:
            raise ProviderError("transport", f"could not reach {url}: {error.reason}") from error
        except OSError as error:
            raise ProviderError("transport", f"could not reach {url}: {error}") from error
        try:
            opened = _describe(raw, url)
            # urllib applies the open timeout to the resulting socket too. Restore
            # the request's published body-stall deadline after the bounded
            # connect/TLS phase, so cancellation does not weaken slow-download
            # behaviour.
            _set_stream_timeout(opened.stream, request.timeout)
            if not self._register(opened.stream):
                raise ProviderError("cancelled", "request cancelled during shutdown")
            return opened
        except BaseException:
            # Ownership begins when urllib returns, not after response metadata
            # happens to parse successfully.
            _interrupt_stream(raw)
            raise

    def fetch(self, request: Request) -> Response:
        opened = self._open(request, compressed=True)
        try:
            _refuse_declared_overflow(opened, request.max_bytes)
            body = _read_body(opened, request.max_bytes)
        finally:
            self._finish(opened)
        return Response(
            url=opened.url,
            status=opened.status,
            content_type=opened.content_type,
            body=body,
            location=opened.location,
        )

    def download(self, request: Request, directory: Path) -> Transfer:
        opened = self._open(request)
        try:
            if opened.status in REDIRECT_STATUSES or not 200 <= opened.status < 300:
                return Transfer(
                    url=opened.url,
                    status=opened.status,
                    content_type=opened.content_type,
                    size=0,
                    location=opened.location,
                )
            descriptor, staged = download_files._mkstemp(
                prefix=STAGING_PREFIX,
                directory=directory,
            )
            staged_pin = download_files._pin_created_temporary(descriptor, staged)
            staged_descriptor: int | None = descriptor
            staged_fingerprint: file_io.FileFingerprint | None = None
            pin_owned = True
            total = 0
            try:
                staged_fingerprint = staged_pin.fingerprint
                owned_descriptor = staged_descriptor
                staged_descriptor = None
                if owned_descriptor is None:
                    raise RuntimeError("download creation descriptor was already consumed")
                with download_files._fdopen_owned(owned_descriptor, "wb") as sink:
                    _refuse_declared_overflow(opened, request.max_bytes)
                    remaining = request.max_bytes
                    while True:
                        chunk = _read_chunk(opened.stream, remaining)
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ProviderError(
                                "size-limit",
                                f"download exceeded its {request.max_bytes} byte ceiling",
                            )
                        sink.write(chunk)
                        total += len(chunk)
                    sink.flush()
                    os.fsync(sink.fileno())
                staged_fingerprint = staged_pin.fingerprint
                pin_owned = False
                transfer = Transfer(
                    url=opened.url,
                    status=opened.status,
                    content_type=opened.content_type,
                    size=total,
                    path=staged,
                    location=opened.location,
                    _staged_pin=staged_pin,
                    _staged_fingerprint=staged_fingerprint,
                )
                return transfer
            finally:
                try:
                    if staged_descriptor is not None:
                        download_files._close_descriptor_preserving_error(
                            staged_descriptor,
                            f"the download staging file {staged}",
                        )
                finally:
                    if pin_owned:
                        download_files._discard_pinned_temporary(
                            staged,
                            staged_pin,
                            fallback=staged_fingerprint,
                        )
                    elif sys.exception() is not None:
                        # Transfer consumes the supplied pin even when its
                        # constructor rejects the handoff. Re-pin by full
                        # generation for exact cleanup rather than touching a
                        # potentially reused public name.
                        active_error = sys.exception()
                        try:
                            if staged_fingerprint is not None:
                                download_files._discard_owned(
                                    staged,
                                    expected_fingerprint=staged_fingerprint,
                                )
                        except BaseException as cleanup_error:
                            if active_error is not None:
                                active_error.add_note(
                                    f"also could not retire provider temporary {staged}: "
                                    f"{cleanup_error}"
                                )
        finally:
            self._finish(opened)

    def _finish(self, opened: _Opened) -> None:
        """Hand a finished response back. urllib's connections never outlive one."""
        try:
            self._unregister(opened.stream)
        finally:
            _close_stream_preserving_error(opened.stream)


class KeepAliveClient(UrllibClient):
    """`UrllibClient`'s guarantees over persistent HTTPS connections.

    urllib sends ``Connection: close`` and builds a new TCP and TLS session for
    every request, so a page of 24 thumbnails cost 24 handshakes -- two or
    three round trips each before the first byte -- and the site 24 times the
    connection set-up work. This keeps a few idle connections per host, the
    way a browser does, and is otherwise the same transport:

    * :func:`require_https` still runs before any socket is touched, and only
      port 443 is ever dialled;
    * redirects are still returned, never followed -- http.client has no
      redirect logic at all;
    * bodies are read through the same bounded readers, and a connection is
      only reused when its previous response was read to the end;
    * certificate and hostname verification use the default context, exactly
      as urllib's ``HTTPSHandler`` does;
    * a configured HTTPS proxy routes the request through the urllib path, so
      proxy behaviour is unchanged.

    `fetch` additionally advertises gzip. Downloads never do: their byte
    counts are checked against the provider's metadata.
    """

    def __init__(
        self,
        *,
        compress: bool = True,
        clock: Callable[[], float] = time.monotonic,
        connect: Connector | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        super().__init__(opener=opener)
        self._compress = compress
        self._clock = clock
        self._connect = connect if connect is not None else _https_connection
        self._idle: dict[str, list[tuple[float, http.client.HTTPConnection]]] = {}
        self._busy: set[http.client.HTTPConnection] = set()
        #: Connections opened, for benchmarks and tests; never a decision input.
        self.connections_opened = 0

    def close(self) -> None:
        super().close()
        with self._lock:
            idle = [connection for pool in self._idle.values() for _, connection in pool]
            self._idle.clear()
            busy = tuple(self._busy)
        for connection in idle:
            _close_connection(connection)
        # A request still waiting for its status line has no registered body
        # stream yet; shutting its socket down is what wakes it.
        for connection in busy:
            _interrupt_connection(connection)

    # -- the pool --------------------------------------------------------

    def _checkout(self, host: str, timeout: float) -> tuple[http.client.HTTPConnection, bool]:
        now = self._clock()
        stale: list[http.client.HTTPConnection] = []
        chosen: http.client.HTTPConnection | None = None
        with self._lock:
            if self._closed:
                raise ProviderError("cancelled", "request cancelled during shutdown")
            pool = self._idle.get(host, [])
            while pool and chosen is None:
                parked_at, candidate = pool.pop()
                if now - parked_at <= IDLE_TIMEOUT_SECONDS and _idle_connection_usable(candidate):
                    chosen = candidate
                else:
                    stale.append(candidate)
            if chosen is not None:
                self._busy.add(chosen)
        for connection in stale:
            _close_connection(connection)
        if chosen is not None:
            if chosen.sock is not None:
                with contextlib.suppress(OSError):
                    chosen.sock.settimeout(timeout)
            return chosen, True
        connection = self._connect(host, timeout)
        with self._lock:
            if self._closed:
                raise ProviderError("cancelled", "request cancelled during shutdown")
            self._busy.add(connection)
            self.connections_opened += 1
        try:
            connection.connect()
        except BaseException:
            self._drop(connection)
            raise
        return connection, False

    def _checkin(self, host: str, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            self._busy.discard(connection)
            if not self._closed and connection.sock is not None:
                pool = self._idle.setdefault(host, [])
                if len(pool) < MAX_IDLE_PER_HOST:
                    pool.append((self._clock(), connection))
                    return
        _close_connection(connection)

    def _drop(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            self._busy.discard(connection)
        _close_connection(connection)

    # -- transport -------------------------------------------------------

    def _open(self, request: Request, *, compressed: bool = False) -> _Opened:
        url = require_https(request.url)
        if self.cancelled():
            raise ProviderError("cancelled", "request cancelled during shutdown")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if _proxied(host):
            return super()._open(request)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        headers = {
            # Explicit, as urllib does, rather than derived from whatever the
            # connector dialled.
            "Host": parsed.netloc,
            "Accept": request.accept,
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": request.user_agent,
            **dict(request.headers),
        }
        if compressed and self._compress:
            headers["Accept-Encoding"] = "gzip"
        connect_timeout = min(request.timeout, CONNECT_TIMEOUT_SECONDS)
        for attempt in range(2):
            try:
                connection, reused = self._checkout(host, connect_timeout)
            except TimeoutError as error:
                raise ProviderError("timeout", f"request to {url} timed out") from error
            except OSError as error:
                raise ProviderError("transport", f"could not reach {url}: {error}") from error
            try:
                connection.request("GET", target, headers=headers)
                response = connection.getresponse()
            except (ConnectionError, http.client.BadStatusLine) as error:
                # A reused connection the server had already closed fails here,
                # before any response arrived. GET is idempotent, so one retry
                # on a fresh connection is safe; a fresh one failing is real.
                self._drop(connection)
                if reused and attempt == 0 and not self.cancelled():
                    continue
                if self.cancelled():
                    raise ProviderError("cancelled", "request cancelled during shutdown") from error
                raise ProviderError("transport", f"could not reach {url}: {error}") from error
            except TimeoutError as error:
                self._drop(connection)
                raise ProviderError("timeout", f"request to {url} timed out") from error
            except (OSError, http.client.HTTPException) as error:
                self._drop(connection)
                if self.cancelled():
                    raise ProviderError("cancelled", "request cancelled during shutdown") from error
                raise ProviderError("transport", f"could not reach {url}: {error}") from error
            break
        try:
            opened = _describe(cast("BinaryIO", response), url)
            opened.connection = connection
            opened.origin = host
            _set_stream_timeout(opened.stream, request.timeout)
            if connection.sock is not None:
                with contextlib.suppress(OSError):
                    connection.sock.settimeout(request.timeout)
            if not self._register(opened.stream):
                raise ProviderError("cancelled", "request cancelled during shutdown")
            return opened
        except BaseException:
            _interrupt_stream(cast("BinaryIO", response))
            self._drop(connection)
            raise

    def _finish(self, opened: _Opened) -> None:
        connection = opened.connection
        if connection is None:
            super()._finish(opened)
            return
        response = cast("http.client.HTTPResponse", opened.stream)
        reusable = False
        try:
            self._unregister(opened.stream)
            if sys.exception() is None and not response.isclosed():
                _drain(response, connection)
            reusable = (
                sys.exception() is None
                and response.isclosed()
                and not response.will_close
                and connection.sock is not None
            )
        except Exception:
            reusable = False
        finally:
            if reusable:
                self._checkin(opened.origin, connection)
            else:
                try:
                    _close_stream_preserving_error(opened.stream)
                finally:
                    self._drop(connection)


#: Opens (but does not yet connect) a connection to ``host``:443. The seam the
#: keep-alive tests replace, since nothing unprivileged can listen on 443.
type Connector = Callable[[str, float], http.client.HTTPConnection]

_TLS_CONTEXT: ssl.SSLContext | None = None
_TLS_CONTEXT_LOCK = threading.Lock()


def _https_connection(host: str, timeout: float) -> http.client.HTTPConnection:
    """What urllib's ``HTTPSHandler`` builds: default verification, port 443."""
    global _TLS_CONTEXT
    with _TLS_CONTEXT_LOCK:
        if _TLS_CONTEXT is None:
            _TLS_CONTEXT = ssl.create_default_context()
            _TLS_CONTEXT.set_alpn_protocols(["http/1.1"])
        context = _TLS_CONTEXT
    return http.client.HTTPSConnection(host, 443, timeout=timeout, context=context)


def default_client() -> UrllibClient:
    """The transport production code builds when none is injected."""
    return KeepAliveClient()


def _proxied(host: str) -> bool:
    """Whether urllib would send a request for ``host`` through a proxy."""
    proxies = urllib.request.getproxies()
    return "https" in proxies and not urllib.request.proxy_bypass(host)


def _idle_connection_usable(connection: http.client.HTTPConnection) -> bool:
    """An idle connection must be silent: readable means EOF, an alert, or junk."""
    sock = connection.sock
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except OSError, ValueError:
        return False
    return not readable


def _drain(response: http.client.HTTPResponse, connection: http.client.HTTPConnection) -> None:
    """Read a small unread body (a redirect, an error page) so the socket survives."""
    if response.length is None or response.length > DRAIN_BYTES:
        return
    if connection.sock is not None:
        connection.sock.settimeout(CONNECT_TIMEOUT_SECONDS)
    response.read(DRAIN_BYTES + 1)


def _close_connection(connection: http.client.HTTPConnection) -> None:
    with contextlib.suppress(Exception):
        connection.close()


def _interrupt_connection(connection: http.client.HTTPConnection) -> None:
    sock = connection.sock
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
    _close_connection(connection)


def _stream_socket(stream: BinaryIO) -> socket.socket | None:
    """Find urllib/http.client's socket without depending on one exact layer."""
    current: object | None = stream
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.socket):
            return current
        owned = getattr(current, "_sock", None)
        if isinstance(owned, socket.socket):
            return owned
        next_layer = getattr(current, "fp", None)
        if next_layer is None:
            next_layer = getattr(current, "raw", None)
        current = next_layer
    return None


def _set_stream_timeout(stream: BinaryIO, timeout: float) -> None:
    connection = _stream_socket(stream)
    if connection is not None:
        with contextlib.suppress(OSError):
            connection.settimeout(timeout)


def _interrupt_stream(stream: BinaryIO) -> None:
    """Wake a blocked body read, then release urllib's response object."""
    connection: socket.socket | None = None
    with contextlib.suppress(Exception):
        connection = _stream_socket(stream)
    if connection is not None:
        with contextlib.suppress(OSError):
            connection.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            connection.close()
    with contextlib.suppress(Exception):
        stream.close()


def _close_stream_preserving_error(stream: BinaryIO) -> None:
    """Close a response without replacing a body/validation failure."""
    active_error = sys.exception()
    try:
        stream.close()
    except Exception as close_error:
        if active_error is not None:
            active_error.add_note(f"also could not close the HTTP response: {close_error}")
            return
        raise


def _describe(raw: BinaryIO, requested: str) -> _Opened:
    """Pull the four things we care about out of whatever urllib returned."""
    status = int(getattr(raw, "status", 0) or 0)
    header_source = getattr(raw, "headers", None)
    content_type = ""
    location = ""
    encoding = ""
    declared = -1
    if header_source is not None:
        content_type = str(header_source.get("Content-Type", "") or "")
        encoding = str(header_source.get("Content-Encoding", "") or "").strip().lower()
        location = str(header_source.get("Location", "") or "")
        raw_length = str(header_source.get("Content-Length", "") or "").strip()
        if raw_length.isdigit():
            declared = int(raw_length)
    return _Opened(
        status=status,
        # Read the effective URL back rather than assume it: with redirects
        # disabled it should equal what was asked for, and a provider checks.
        url=str(getattr(raw, "url", requested) or requested),
        content_type=content_type.split(";", 1)[0].strip().lower(),
        location=location.strip(),
        declared_length=declared,
        stream=raw,
        content_encoding=encoding,
    )


def _refuse_declared_overflow(opened: _Opened, maximum: int) -> None:
    if opened.declared_length > maximum:
        raise ProviderError(
            "size-limit",
            f"response declares {opened.declared_length} bytes, over the {maximum} ceiling",
        )


def _read_chunk(stream: BinaryIO, remaining: int) -> bytes:
    # Ask for one byte more than is allowed, so an overrun is detected instead
    # of silently truncating the body into something that still parses.
    try:
        return stream.read(min(CHUNK_BYTES, max(remaining, 0) + 1))
    except TimeoutError as error:
        raise ProviderError("timeout", "the response stalled mid-body") from error
    except (OSError, http.client.HTTPException) as error:
        raise ProviderError("transport", f"the response failed mid-body: {error}") from error


def _read_body(opened: _Opened, maximum: int) -> bytes:
    """The response body, decoded when it arrived gzip-compressed."""
    if opened.content_encoding in {"", "identity"}:
        return read_bounded(opened.stream, maximum)
    if opened.content_encoding in {"gzip", "x-gzip"}:
        return read_bounded_gzip(opened.stream, maximum)
    raise ProviderError(
        "response", f"unsupported content encoding {opened.content_encoding[:32]!r}"
    )


def read_bounded_gzip(stream: BinaryIO, maximum: int) -> bytes:
    """Inflate a gzip body, refusing once either side passes ``maximum``.

    The compressed bytes are bounded like any other body, and the inflated
    output is bounded *while inflating*, so a small bomb cannot expand past
    the ceiling in memory before the check runs.
    """
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    chunks: list[bytes] = []
    received = 0
    produced = 0
    try:
        while True:
            chunk = _read_chunk(stream, maximum - received)
            if not chunk:
                break
            received += len(chunk)
            if received > maximum:
                raise ProviderError("size-limit", f"response exceeded its {maximum} byte ceiling")
            pending = chunk
            while pending:
                if inflater.eof:
                    raise ProviderError("response", "gzip body has trailing data")
                output = inflater.decompress(pending, maximum - produced + 1)
                produced += len(output)
                if produced > maximum:
                    raise ProviderError(
                        "size-limit", f"decoded response exceeded its {maximum} byte ceiling"
                    )
                chunks.append(output)
                # Input past the gzip trailer lands in `unused_data`; feeding it
                # round again is what turns it into the trailing-data refusal.
                pending = inflater.unconsumed_tail or inflater.unused_data
    except zlib.error as error:
        raise ProviderError("response", f"invalid gzip body: {error}") from error
    if not inflater.eof:
        raise ProviderError("response", "gzip body ended before its trailer")
    return b"".join(chunks)


def read_bounded(stream: BinaryIO, maximum: int) -> bytes:
    """Read up to ``maximum`` bytes, refusing rather than truncating past it."""
    chunks: list[bytes] = []
    remaining = maximum
    while True:
        chunk = _read_chunk(stream, remaining)
        if not chunk:
            return b"".join(chunks)
        remaining -= len(chunk)
        if remaining < 0:
            raise ProviderError("size-limit", f"response exceeded its {maximum} byte ceiling")
        chunks.append(chunk)


class RateLimiter:
    """Keep at least ``interval`` seconds between calls.

    Wallhaven publishes a 45-request-per-minute limit. One process removes the
    old cross-process lock, but not synchronization itself: search, detail and
    download workers share a provider and can call this concurrently.
    """

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        # Keep the reservation while sleeping. Releasing it first lets every
        # waiter sleep toward one deadline and then issue a burst together.
        with self._lock:
            now = self._clock()
            if self._last is not None:
                delay = self._interval - (now - self._last)
                if delay > 0:
                    self._sleep(delay)
                    now = self._clock()
            self._last = now
