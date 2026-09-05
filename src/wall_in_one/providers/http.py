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
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message as HTTPMessage
from pathlib import Path
from types import TracebackType
from typing import IO, BinaryIO, Final, Protocol, cast
from urllib.parse import urlsplit

from wall_in_one import file_io
from wall_in_one.providers import download as download_files
from wall_in_one.providers.base import ProviderError

#: Sent on every request. Identifying the client honestly is the price of using
#: someone else's public API.
USER_AGENT: Final = "wall-in-one/0.1.3 (+https://github.com/Go08er/wall-in-one)"

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
    """The seam. Tests implement this; in production only `UrllibClient` does."""

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

    def _open(self, request: Request) -> _Opened:
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
        opened = self._open(request)
        try:
            _refuse_declared_overflow(opened, request.max_bytes)
            body = read_bounded(opened.stream, request.max_bytes)
        finally:
            try:
                self._unregister(opened.stream)
            finally:
                _close_stream_preserving_error(opened.stream)
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
            try:
                self._unregister(opened.stream)
            finally:
                _close_stream_preserving_error(opened.stream)


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
    declared = -1
    if header_source is not None:
        content_type = str(header_source.get("Content-Type", "") or "")
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
    except OSError as error:
        raise ProviderError("transport", f"the response failed mid-body: {error}") from error


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
