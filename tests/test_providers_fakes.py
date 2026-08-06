"""Stand-ins for the network seam, shared by the provider suites.

Named `test_*` so it sits beside the suites that import it. The one test here
asserts the fakes really satisfy `http.Client`, so a change to the protocol
cannot leave them silently behind while every other suite keeps passing.

A route that was never registered raises rather than returning a default. That
is the mechanism that keeps the suite offline: a provider reaching for a URL
nobody stubbed fails loudly instead of quietly opening a socket.
"""

from __future__ import annotations

import struct
import tempfile
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from wall_in_one.providers import http
from wall_in_one.providers.base import ProviderError


@dataclass(frozen=True, slots=True)
class Reply:
    """What the fake transport should say to one request."""

    body: bytes = b""
    status: int = 200
    content_type: str = "text/html"
    location: str = ""
    #: Effective URL, when it should differ from the one requested.
    url: str = ""
    #: Raised instead of answering, to rehearse a transport failure.
    error: ProviderError | None = None


@dataclass
class FakeClient:
    """An `http.Client` backed by a routing table.

    A route's value may be a list, in which case successive requests to the
    same URL consume it in order -- which is how a redirect chain, or a retry,
    is expressed.
    """

    routes: Mapping[str, Reply | list[Reply]] = field(default_factory=dict)
    requests: list[http.Request] = field(default_factory=list)

    def _reply(self, request: http.Request) -> Reply:
        self.requests.append(request)
        entry = self.routes.get(request.url)
        if entry is None:
            raise AssertionError(f"no fake route for {request.url}")
        if isinstance(entry, list):
            if not entry:
                raise AssertionError(f"fake route for {request.url} is exhausted")
            return entry.pop(0)
        return entry

    def fetch(self, request: http.Request) -> http.Response:
        reply = self._reply(request)
        if reply.error is not None:
            raise reply.error
        if len(reply.body) > request.max_bytes:
            raise ProviderError(
                "size-limit", f"response exceeded its {request.max_bytes} byte ceiling"
            )
        return http.Response(
            url=reply.url or request.url,
            status=reply.status,
            content_type=reply.content_type,
            body=reply.body,
            location=reply.location,
        )

    def download(self, request: http.Request, directory: Path) -> http.Transfer:
        reply = self._reply(request)
        if reply.error is not None:
            raise reply.error
        if reply.status in http.REDIRECT_STATUSES or not 200 <= reply.status < 300:
            return http.Transfer(
                url=reply.url or request.url,
                status=reply.status,
                content_type=reply.content_type,
                size=0,
                location=reply.location,
            )
        if len(reply.body) > request.max_bytes:
            raise ProviderError(
                "size-limit", f"download exceeded its {request.max_bytes} byte ceiling"
            )
        descriptor, name = tempfile.mkstemp(prefix=http.STAGING_PREFIX, dir=directory)
        staged = Path(name)
        with open(descriptor, "wb") as sink:
            sink.write(reply.body)
        return http.Transfer(
            url=reply.url or request.url,
            status=reply.status,
            content_type=reply.content_type,
            size=len(reply.body),
            path=staged,
            location=reply.location,
        )


class FrozenClock:
    """A clock that only moves when a `sleep` asks it to."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


# -- byte fixtures -------------------------------------------------------


def mp4_bytes(payload: bytes = b"\x00" * 64) -> bytes:
    """A file that passes the ISO-BMFF `ftyp` check and nothing more."""
    header = struct.pack(">I", 24) + b"ftypisom" + b"\x00" * 12
    return header + payload


def png_bytes(width: int = 4, height: int = 3) -> bytes:
    """A structurally complete truecolour PNG, CRCs and all."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x7f\x30\x10" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def jpeg_bytes(width: int = 1920, height: int = 1080, *, entropy: bytes = b"\x11" * 8) -> bytes:
    """A baseline JPEG with one frame header and one scan."""
    frame = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    scan = b"\xff\xda" + struct.pack(">HB", 8, 1) + b"\x01\x00" + b"\x00\x3f\x00"
    return b"\xff\xd8" + frame + scan + entropy + b"\xff\xd9"


def names_in(directory: Path) -> set[str]:
    return {entry.name for entry in directory.iterdir()}


def flatten(replies: Iterable[Reply]) -> list[Reply]:
    return list(replies)


def test_the_fakes_satisfy_the_client_protocol() -> None:
    client: http.Client = FakeClient()
    assert callable(client.fetch)
    assert callable(client.download)
