"""The network seam itself: bounds, scheme, redirects, and staying offline."""

from __future__ import annotations

import io
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from typing import IO

import pytest

from tests.test_providers_fakes import FrozenClock
from wall_in_one.providers import base, cache, download, http, motionbgs, registry, wallhaven
from wall_in_one.providers.base import ProviderError
from wall_in_one.providers.cache import TtlCache

#: Everything in the package except the transport itself.
PROVIDER_MODULES = (base, cache, download, motionbgs, registry, wallhaven)


# -- the seam is single --------------------------------------------------


def test_only_the_transport_module_can_reach_the_network() -> None:
    """Every other provider module must be unable to open a socket at all.

    This is the property the whole suite rests on: if a provider grew a direct
    `urllib` call, tests would keep passing while quietly hitting the internet.
    """
    for module in PROVIDER_MODULES:
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        for forbidden in ("import urllib.request", "import socket", "import subprocess"):
            assert forbidden not in source, f"{module.__name__} reaches past the seam"


def test_a_search_runs_with_sockets_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: prove it offline rather than infer it from imports."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a test opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    filters = wallhaven.WallhavenFilters(query="mountains")
    assert filters.search_url().startswith("https://wallhaven.cc/api/v1/search?")


# -- URL admission -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://wallhaven.cc/",
        "ftp://wallhaven.cc/",
        "file:///etc/passwd",
        "https://user:pass@wallhaven.cc/",
        "https://wallhaven.cc:8443/",
        "https:///nohost",
        "",
        "https://wallhaven.cc/\nX-Injected: 1",
        "https://wallhaven.cc/" + "a" * 4096,
    ],
)
def test_hostile_urls_never_reach_a_socket(url: str) -> None:
    with pytest.raises(ProviderError) as caught:
        http.require_https(url)
    assert caught.value.kind == "invalid-url"


def test_a_plain_https_url_is_admitted() -> None:
    assert http.require_https("https://wallhaven.cc/api/v1/search?q=a") == (
        "https://wallhaven.cc/api/v1/search?q=a"
    )


# -- bounded reads -------------------------------------------------------


def test_reading_stops_at_the_ceiling_rather_than_truncating() -> None:
    stream = io.BytesIO(b"x" * 5000)
    with pytest.raises(ProviderError) as caught:
        http.read_bounded(stream, 1024)
    assert caught.value.kind == "size-limit"


def test_a_body_exactly_at_the_ceiling_is_accepted() -> None:
    stream = io.BytesIO(b"x" * 1024)
    assert http.read_bounded(stream, 1024) == b"x" * 1024


def test_a_stalled_read_becomes_a_timeout() -> None:
    class Stalls(io.BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            raise TimeoutError("stalled")

    with pytest.raises(ProviderError) as caught:
        http.read_bounded(Stalls(), 1024)
    assert caught.value.kind == "timeout"


# -- the real client, without a network ----------------------------------


class _StubOpener:
    """An `OpenerDirector` stand-in, so `UrllibClient` runs without a socket."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.opened: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> object:
        self.opened.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Fixed(io.BytesIO):
    """A response object shaped like what urllib hands back."""

    def __init__(self, body: bytes, status: int, headers: dict[str, str], url: str) -> None:
        super().__init__(body)
        self.status = status
        self.url = url
        message = Message()
        for key, value in headers.items():
            message[key] = value
        self.headers = message


def _client(result: object) -> tuple[http.UrllibClient, _StubOpener]:
    opener = _StubOpener(result)
    # `UrllibClient` only ever calls `.open`, so a structural stand-in is
    # enough and no real opener chain is built.
    return http.UrllibClient(opener=opener), opener  # type: ignore[arg-type]


def test_the_client_sends_its_headers_and_reads_the_body() -> None:
    client, opener = _client(
        _Fixed(b"{}", 200, {"Content-Type": "application/json; charset=utf-8"}, "https://a.test/")
    )
    response = client.fetch(
        http.Request(
            url="https://a.test/",
            accept="application/json",
            timeout=5.0,
            max_bytes=1024,
            headers=(("X-API-Key", "secret"),),
        )
    )
    assert response.status == 200
    assert response.content_type == "application/json"
    assert response.body == b"{}"
    sent = opener.opened[0]
    assert sent.get_header("X-api-key") == "secret"
    assert sent.get_header("User-agent") == http.USER_AGENT
    assert opener.timeouts == [5.0]


def test_a_media_request_keeps_its_body_deadline_but_bounds_connect() -> None:
    client, opener = _client(_Fixed(b"ok", 200, {}, "https://a.test/"))

    assert (
        client.fetch(
            http.Request(url="https://a.test/", accept="*/*", timeout=300.0, max_bytes=64)
        ).body
        == b"ok"
    )

    assert opener.timeouts == [http.CONNECT_TIMEOUT_SECONDS]


def test_close_wakes_an_active_response_reader() -> None:
    entered = threading.Event()
    released = threading.Event()

    class Blocking(_Fixed):
        def read(self, size: int | None = -1, /) -> bytes:
            entered.set()
            released.wait(timeout=2)
            if self.closed:
                raise OSError("response closed")
            return super().read(size)

        def close(self) -> None:
            released.set()
            super().close()

    client, _opener = _client(Blocking(b"body", 200, {}, "https://a.test/"))
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        client.fetch,
        http.Request(url="https://a.test/", accept="*/*", timeout=300.0, max_bytes=64),
    )
    assert entered.wait(1)

    started = time.monotonic()
    client.close()
    with pytest.raises(ProviderError):
        future.result(timeout=1)
    elapsed = time.monotonic() - started
    pool.shutdown(wait=True, cancel_futures=True)

    assert elapsed < 1.0
    assert client.cancelled()


def test_close_refuses_a_race_late_request_before_opening() -> None:
    client, opener = _client(_Fixed(b"body", 200, {}, "https://a.test/"))
    client.close()

    with pytest.raises(ProviderError, match="cancelled") as caught:
        client.fetch(http.Request(url="https://a.test/", accept="*/*", timeout=300.0, max_bytes=64))

    assert caught.value.kind == "cancelled"
    assert opener.opened == []


def test_a_finished_transfer_retains_the_real_client_shutdown_probe(tmp_path: Path) -> None:
    """Post-transfer validators can still observe close after the socket is gone."""
    client, _ = _client(_Fixed(b"payload", 200, {}, "https://a.test/f"))
    transfer = client.download(
        http.Request(url="https://a.test/f", accept="*/*", timeout=5.0, max_bytes=64),
        tmp_path,
    )
    probe = base.optional_cancellation_probe(client)

    assert probe is not None and not probe()
    client.close()
    assert probe()
    transfer.discard()


def test_a_redirect_is_reported_rather_than_followed() -> None:
    error = urllib.error.HTTPError("https://a.test/", 302, "Found", Message(), io.BytesIO(b""))
    error.headers["Location"] = "https://elsewhere.test/"
    client, _ = _client(error)
    response = client.fetch(
        http.Request(url="https://a.test/", accept="*/*", timeout=5.0, max_bytes=64)
    )
    assert response.is_redirect
    assert response.location == "https://elsewhere.test/"


def test_a_lying_content_length_is_refused_before_reading() -> None:
    client, _ = _client(_Fixed(b"x" * 10, 200, {"Content-Length": "999999999"}, "https://a.test/"))
    with pytest.raises(ProviderError) as caught:
        client.fetch(http.Request(url="https://a.test/", accept="*/*", timeout=5.0, max_bytes=1024))
    assert caught.value.kind == "size-limit"


def test_a_transport_failure_becomes_a_provider_error() -> None:
    client, _ = _client(urllib.error.URLError("no route to host"))
    with pytest.raises(ProviderError) as caught:
        client.fetch(http.Request(url="https://a.test/", accept="*/*", timeout=5.0, max_bytes=64))
    assert caught.value.kind == "transport"


def test_a_connect_timeout_becomes_a_timeout(tmp_path: Path) -> None:
    client, _ = _client(TimeoutError("timed out"))
    with pytest.raises(ProviderError) as caught:
        client.download(
            http.Request(url="https://a.test/", accept="*/*", timeout=1.0, max_bytes=64),
            tmp_path,
        )
    assert caught.value.kind == "timeout"


def test_an_oversized_download_leaves_no_file_behind(tmp_path: Path) -> None:
    client, _ = _client(_Fixed(b"y" * 4096, 200, {}, "https://a.test/f"))
    with pytest.raises(ProviderError) as caught:
        client.download(
            http.Request(url="https://a.test/f", accept="*/*", timeout=5.0, max_bytes=100),
            tmp_path,
        )
    assert caught.value.kind == "size-limit"
    assert list(tmp_path.iterdir()) == []


def test_a_staging_creation_failure_releases_the_registered_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _Fixed(b"payload", 200, {}, "https://a.test/f")
    client, _ = _client(stream)
    monkeypatch.setattr(
        "wall_in_one.providers.http.tempfile.mkstemp",
        lambda **_keywords: (_ for _ in ()).throw(OSError("disk refused staging")),
    )

    with pytest.raises(OSError, match="disk refused staging"):
        client.download(
            http.Request(url="https://a.test/f", accept="*/*", timeout=5.0, max_bytes=1024),
            tmp_path,
        )

    assert stream.closed
    assert client._active == {}


def test_a_download_stages_into_the_directory_it_was_given(tmp_path: Path) -> None:
    client, _ = _client(_Fixed(b"payload", 200, {"Content-Type": "video/mp4"}, "https://a.test/f"))
    transfer = client.download(
        http.Request(url="https://a.test/f", accept="*/*", timeout=5.0, max_bytes=1024),
        tmp_path,
    )
    assert transfer.path is not None
    assert transfer.path.parent == tmp_path
    assert transfer.path.read_bytes() == b"payload"
    assert transfer.size == 7
    transfer.discard()
    assert list(tmp_path.iterdir()) == []


def test_a_redirected_download_stages_nothing(tmp_path: Path) -> None:
    error = urllib.error.HTTPError("https://a.test/f", 302, "Found", Message(), io.BytesIO(b""))
    error.headers["Location"] = "https://a.test/g"
    client, _ = _client(error)
    transfer = client.download(
        http.Request(url="https://a.test/f", accept="*/*", timeout=5.0, max_bytes=1024),
        tmp_path,
    )
    assert transfer.is_redirect
    assert transfer.path is None
    assert list(tmp_path.iterdir()) == []


def test_a_transfer_used_as_a_context_manager_cleans_up(tmp_path: Path) -> None:
    client, _ = _client(_Fixed(b"payload", 200, {}, "https://a.test/f"))
    transfer = client.download(
        http.Request(url="https://a.test/f", accept="*/*", timeout=5.0, max_bytes=1024),
        tmp_path,
    )
    with transfer:
        assert transfer.path is not None and transfer.path.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_redirect_handler_declines_every_redirect() -> None:
    handler = http._NoRedirects()
    stream: IO[bytes] = io.BytesIO(b"")
    assert (
        handler.redirect_request(
            urllib.request.Request("https://a.test/"),
            stream,
            302,
            "Found",
            Message(),
            "https://b.test/",
        )
        is None
    )


# -- rate limiting -------------------------------------------------------


def test_the_first_call_never_waits_and_the_next_one_does() -> None:
    clock = FrozenClock()
    limiter = http.RateLimiter(2.0, clock=clock, sleep=clock.sleep)
    limiter.wait()
    assert clock.slept == []
    limiter.wait()
    assert clock.slept == [2.0]


def test_time_already_spent_counts_against_the_interval() -> None:
    clock = FrozenClock()
    limiter = http.RateLimiter(2.0, clock=clock, sleep=clock.sleep)
    limiter.wait()
    clock.now += 5.0
    limiter.wait()
    assert clock.slept == []


def test_concurrent_rate_limit_waiters_reserve_separate_intervals() -> None:
    """Detail, search and download workers must not wake into one request burst."""
    clock = FrozenClock()
    limiter = http.RateLimiter(2.0, clock=clock, sleep=clock.sleep)
    barrier = threading.Barrier(4)

    def wait() -> None:
        barrier.wait()
        limiter.wait()

    workers = [threading.Thread(target=wait) for _ in range(3)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert not any(worker.is_alive() for worker in workers)
    assert clock.slept == [2.0, 2.0]


# -- the cache -----------------------------------------------------------


def test_the_cache_expires_and_evicts() -> None:
    clock = FrozenClock()
    cache: TtlCache[str] = TtlCache(max_entries=2, ttl=10.0, clock=clock)
    cache.put("a", "one")
    cache.put("b", "two")
    assert cache.get("a") == "one"

    cache.put("c", "three")
    # "b" is least recently used -- "a" was just read.
    assert cache.get("b") is None
    assert cache.get("a") == "one"

    clock.now += 11.0
    assert cache.get("a") is None
    assert len(cache) == 1


def test_cache_clear_cannot_interleave_with_an_expiring_read() -> None:
    """A detail-cache clear must not make another worker's get raise KeyError."""
    entered = threading.Event()
    release = threading.Event()
    cleared = threading.Event()
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(2)
            return 20.0
        return 0.0

    cache: TtlCache[str] = TtlCache(ttl=10.0, clock=clock)
    cache.put("a", "one")
    errors: list[Exception] = []

    def read() -> None:
        try:
            cache.get("a")
        except Exception as error:  # pragma: no cover - the assertion below reports it
            errors.append(error)

    reader = threading.Thread(target=read)
    reader.start()
    assert entered.wait(2)

    def clear() -> None:
        cache.clear()
        cleared.set()

    clearer = threading.Thread(target=clear)
    clearer.start()
    assert not cleared.wait(0.05)
    release.set()
    reader.join(timeout=2)
    clearer.join(timeout=2)

    assert errors == []
    assert cleared.is_set()
