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

import os
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message as HTTPMessage
from pathlib import Path
from types import TracebackType
from typing import IO, BinaryIO, Final, Protocol, cast
from urllib.parse import urlsplit

from wall_in_one.providers.base import ProviderError

#: Sent on every request. Identifying the client honestly is the price of using
#: someone else's public API.
USER_AGENT: Final = "wall-in-one/0.1.0 (+https://github.com/goober/wall-in-one)"

#: Read granularity. Large enough that a 64 MiB image is not a million calls,
#: small enough that the ceiling is enforced promptly.
CHUNK_BYTES: Final = 256 * 1024

#: Nothing this package pulls *into memory* is legitimately larger.
DEFAULT_MAX_BYTES: Final = 1024 * 1024

#: Handed back to the caller rather than followed.
REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

#: Staged downloads are dot-files so a library scan already ignores them if one
#: is ever left behind by a hard kill.
STAGING_PREFIX: Final = ".wall-in-one-staged-"


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
    caller: install it or :meth:`discard` it. It is ``None`` when the remote
    answered with a redirect or an error, where there is no body worth staging.
    """

    url: str
    status: int
    content_type: str
    size: int
    path: Path | None = None
    location: str = ""

    @property
    def is_redirect(self) -> bool:
        return self.status in REDIRECT_STATUSES

    def discard(self) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> Transfer:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.discard()


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

    # -- transport -------------------------------------------------------

    def _open(self, request: Request) -> _Opened:
        url = require_https(request.url)
        headers = {
            "Accept": request.accept,
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": request.user_agent,
            **dict(request.headers),
        }
        outgoing = urllib.request.Request(url, method="GET", headers=headers)
        try:
            raw = cast("BinaryIO", self._opener.open(outgoing, timeout=request.timeout))
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
        return _describe(raw, url)

    def fetch(self, request: Request) -> Response:
        opened = self._open(request)
        try:
            _refuse_declared_overflow(opened, request.max_bytes)
            body = read_bounded(opened.stream, request.max_bytes)
        finally:
            opened.stream.close()
        return Response(
            url=opened.url,
            status=opened.status,
            content_type=opened.content_type,
            body=body,
            location=opened.location,
        )

    def download(self, request: Request, directory: Path) -> Transfer:
        opened = self._open(request)
        if opened.status in REDIRECT_STATUSES or not 200 <= opened.status < 300:
            opened.stream.close()
            return Transfer(
                url=opened.url,
                status=opened.status,
                content_type=opened.content_type,
                size=0,
                location=opened.location,
            )
        descriptor, name = tempfile.mkstemp(prefix=STAGING_PREFIX, dir=directory)
        staged = Path(name)
        total = 0
        try:
            _refuse_declared_overflow(opened, request.max_bytes)
            with os.fdopen(descriptor, "wb") as sink:
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
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        finally:
            opened.stream.close()
        return Transfer(
            url=opened.url,
            status=opened.status,
            content_type=opened.content_type,
            size=total,
            path=staged,
            location=opened.location,
        )


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

    Wallhaven publishes a 45-request-per-minute limit; the predecessor stored
    the last-request timestamp in a lock-protected file because several helper
    *processes* could race for it. One process needs none of that, so this is a
    plain attribute -- and a monotonic clock cannot run backwards, which
    removes the clock-skew clamp the file version needed.
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

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            delay = self._interval - (now - self._last)
            if delay > 0:
                self._sleep(delay)
                now = self._clock()
        self._last = now
