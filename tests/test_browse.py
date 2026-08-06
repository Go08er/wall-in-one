"""The layer between the window and the providers: selection, roots, downloads."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_providers_fakes import FakeClient, Reply, png_bytes
from wall_in_one.browse import Browser, Downloaded
from wall_in_one.library.model import Kind
from wall_in_one.providers.base import (
    DownloadResult,
    ProviderError,
    SearchQuery,
    SearchResult,
    WallpaperCandidate,
)


def candidate(
    provider: str = "wallhaven", *, thumbnail: str = "", identifier: str = "ab1234"
) -> WallpaperCandidate:
    return WallpaperCandidate(
        provider=provider,
        identifier=identifier,
        title="a wallpaper",
        kind=Kind.STILL,
        page_url=f"https://wallhaven.cc/w/{identifier}",
        thumbnail_url=thumbnail,
    )


def browser(routes: dict[str, Reply | list[Reply]] | None = None, **kwargs: object) -> Browser:
    return Browser(client=FakeClient(routes=routes or {}), **kwargs)  # type: ignore[arg-type]


# -- providers -----------------------------------------------------------


def test_every_provider_is_described_without_a_network(tmp_path: Path) -> None:
    infos = browser(root=tmp_path).available
    assert {info.name for info in infos} == {"motionbgs", "wallhaven"}
    assert all(info.usable for info in infos)


def test_a_provider_is_built_once_and_kept() -> None:
    """The provider owns the search cache, so rebuilding it would drop the cache."""
    engine = browser()
    assert engine.provider("wallhaven") is engine.provider("wallhaven")


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ProviderError) as caught:
        browser().provider("nowhere")
    assert caught.value.kind == "unknown-provider"


# -- the download root ---------------------------------------------------


def test_an_explicit_root_wins(tmp_path: Path) -> None:
    assert browser(root=tmp_path).download_root() == tmp_path


def test_the_root_comes_from_noctalia_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Downloads land in the directory Noctalia already calls the library."""
    monkeypatch.setattr("wall_in_one.library.scan.default_roots", lambda: (tmp_path,))
    assert browser().download_root() == tmp_path


def test_no_root_anywhere_is_an_error_not_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    # Writing to an invented directory would scatter files somewhere the user
    # never asked for, which is worse than refusing.
    monkeypatch.setattr("wall_in_one.library.scan.default_roots", tuple)
    with pytest.raises(ProviderError) as caught:
        browser().download_root()
    assert caught.value.kind == "no-root"


def test_a_download_reports_where_to_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = DownloadResult(
        provider="wallhaven",
        identifier="ab1234",
        path=tmp_path / "Wall-in-One" / "Wallhaven" / "wallhaven-ab1234.png",
        sidecar=tmp_path / "s.json",
        marker=tmp_path / "m.json",
        kind=Kind.STILL,
        size=3 * 1024 * 1024,
        source_url="https://wallhaven.cc/w/ab1234",
        download_url="https://w.wallhaven.cc/full/ab/wallhaven-ab1234.png",
        sha256="0" * 64,
        downloaded_at="2026-01-01T00:00:00Z",
    )

    engine = browser(root=tmp_path)

    class Stub:
        def download(self, _candidate: object, root: Path, *, variant: str = "") -> DownloadResult:
            assert root == tmp_path
            assert variant == "4k"
            return installed

    monkeypatch.setattr(engine, "provider", lambda _name: Stub())

    done = engine.download(candidate(), variant="4k")

    assert isinstance(done, Downloaded)
    assert done.root == tmp_path
    assert done.result.path.name == "wallhaven-ab1234.png"
    assert done.describe() == "downloaded wallhaven-ab1234.png (3.0 MB)"


# -- thumbnails ----------------------------------------------------------


def test_a_candidate_without_a_thumbnail_is_not_a_failure() -> None:
    assert browser().thumbnail(candidate()) == b""


def test_a_thumbnail_is_fetched_bounded() -> None:
    url = "https://th.wallhaven.cc/lg/ab/ab1234.jpg"
    body = png_bytes(4, 3)
    client = FakeClient(routes={url: Reply(body=body, content_type="image/png")})
    engine = Browser(client=client)

    assert engine.thumbnail(candidate(thumbnail=url)) == body
    assert client.requests[0].max_bytes <= 4 * 1024 * 1024
    assert client.requests[0].timeout > 0


def test_a_thumbnail_that_is_not_an_image_is_discarded() -> None:
    """An error page rendered into a Gtk.Picture is worse than no picture."""
    url = "https://th.wallhaven.cc/lg/ab/ab1234.jpg"
    client = FakeClient(routes={url: Reply(body=b"<html>nope", content_type="text/html")})

    assert Browser(client=client).thumbnail(candidate(thumbnail=url)) == b""


def test_a_thumbnail_redirect_is_discarded() -> None:
    url = "https://th.wallhaven.cc/lg/ab/ab1234.jpg"
    client = FakeClient(routes={url: Reply(status=302, location="https://evil.example/x.png")})

    assert Browser(client=client).thumbnail(candidate(thumbnail=url)) == b""


def test_a_plaintext_thumbnail_url_never_opens_a_socket() -> None:
    client = FakeClient(routes={})
    with pytest.raises(ProviderError):
        Browser(client=client).thumbnail(candidate(thumbnail="http://th.wallhaven.cc/x.jpg"))
    assert client.requests == []


# -- search --------------------------------------------------------------


def test_search_is_delegated_to_the_named_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = browser()
    seen: list[SearchQuery] = []
    answer = SearchResult(
        provider="wallhaven", query_url="https://wallhaven.cc/api/v1/search", items=()
    )

    class Stub:
        def search(self, query: SearchQuery) -> SearchResult:
            seen.append(query)
            return answer

    monkeypatch.setattr(engine, "provider", lambda _name: Stub())
    assert engine.search("wallhaven", SearchQuery(text="aurora")) is answer
    assert seen[0].text == "aurora"
