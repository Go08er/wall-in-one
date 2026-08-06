"""Wallhaven: filters, disbelieving the JSON, and pixels that match metadata."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.test_providers_fakes import (
    FakeClient,
    FrozenClock,
    Reply,
    jpeg_bytes,
    names_in,
    png_bytes,
)
from wall_in_one.library.model import Kind
from wall_in_one.providers import http, wallhaven
from wall_in_one.providers.base import ProviderError, SearchQuery, WallpaperCandidate
from wall_in_one.providers.download import WALLHAVEN_LOCATION

SEARCH_URL = "https://wallhaven.cc/api/v1/search?categories=111&purity=100&sorting=date_added&order=desc&page=1"


def record(
    identifier: str = "ab1234", extension: str = "jpg", **overrides: object
) -> dict[str, object]:
    base: dict[str, object] = {
        "id": identifier,
        "url": f"https://wallhaven.cc/w/{identifier}",
        "short_url": f"https://whvn.cc/{identifier}",
        "path": (
            f"https://w.wallhaven.cc/full/{identifier[:2]}/wallhaven-{identifier}.{extension}"
        ),
        "file_type": "image/jpeg" if extension == "jpg" else "image/png",
        "file_size": 1234,
        "dimension_x": 1920,
        "dimension_y": 1080,
        "resolution": "1920x1080",
        "ratio": "1.78",
        "purity": "sfw",
        "category": "general",
        "views": 10,
        "favorites": 2,
        "source": "",
        "created_at": "2024-01-01 00:00:00",
        "colors": ["#000000", "not a colour"],
        "thumbs": {
            "large": f"https://th.wallhaven.cc/lg/{identifier[:2]}/{identifier}.jpg",
            "small": f"https://th.wallhaven.cc/small/{identifier[:2]}/{identifier}.jpg",
            "original": "https://evil.example/thumb.jpg",
        },
        "tags": [{"id": 7, "name": "aurora", "alias": "", "category": "nature", "purity": "sfw"}],
        "uploader": {"username": "someone", "group": "User"},
    }
    base.update(overrides)
    return base


def json_reply(document: object, **overrides: object) -> Reply:
    fields: dict[str, object] = {
        "body": json.dumps(document).encode("utf-8"),
        "content_type": "application/json",
    }
    fields.update(overrides)
    return Reply(**fields)  # type: ignore[arg-type]


def provider(
    routes: dict[str, Reply | list[Reply]], *, api_key: str = ""
) -> tuple[wallhaven.Wallhaven, FakeClient]:
    client = FakeClient(routes=routes)
    clock = FrozenClock()
    return (
        wallhaven.Wallhaven(
            client,
            api_key=api_key,
            rate_limiter=http.RateLimiter(0.0, clock=clock, sleep=clock.sleep),
        ),
        client,
    )


# -- filters -------------------------------------------------------------


def test_the_default_search_url_is_stable() -> None:
    assert wallhaven.WallhavenFilters().search_url() == SEARCH_URL


def test_filters_reach_the_url_in_wallhaven_s_own_spelling() -> None:
    filters = wallhaven.WallhavenFilters.from_query(
        SearchQuery(
            text="aurora borealis",
            page=3,
            options={
                "categories": "100",
                "purity": "110",
                "sorting": "toplist",
                "order": "asc",
                "atleast": "1920x1080",
                "ratios": "16x9,16x10",
                "colors": "#0066cc",
                "top_range": "1w",
            },
        ),
        authenticated=True,
    )
    url = filters.search_url()
    assert "q=aurora%20borealis" in url
    assert "atleast=1920x1080" in url
    assert "ratios=16x9%2C16x10" in url
    assert "colors=0066cc" in url
    assert "topRange=1w" in url
    assert url.endswith("&page=3")


def test_nsfw_needs_a_key_and_says_so() -> None:
    with pytest.raises(ProviderError) as caught:
        wallhaven.WallhavenFilters.from_query(
            SearchQuery(options={"purity": "001"}), authenticated=False
        )
    assert caught.value.kind == "credential"

    allowed = wallhaven.WallhavenFilters.from_query(
        SearchQuery(options={"purity": "001"}), authenticated=True
    )
    assert allowed.purity == "001"


@pytest.mark.parametrize(
    "options",
    [
        {"categories": "000"},
        {"categories": "11"},
        {"categories": "abc"},
        {"purity": "2"},
        {"sorting": "alphabetical"},
        {"order": "sideways"},
        {"atleast": "1920"},
        {"atleast": "0x0"},
        {"atleast": "1920 x 1080"},
        {"resolutions": "1920x1080,"},
        {"resolutions": "01920x1080"},
        {"resolutions": "1920x1080, 1280x720"},
        {"ratios": "16:9"},
        {"ratios": "0x9"},
        {"ratios": "9999x1"},
        {"colors": "123456"},
        {"top_range": "2y"},
        {"seed": "abc123"},
        {"atleast": "1920x1080", "resolutions": "1280x720"},
        {"unknown": "1"},
    ],
)
def test_bad_filters_are_refused_before_any_request(options: dict[str, str]) -> None:
    engine, client = provider({})
    with pytest.raises(ProviderError):
        engine.search(SearchQuery(options=options))
    assert client.requests == []


def test_an_out_of_range_page_is_refused() -> None:
    engine, client = provider({})
    for page in (0, -1, 10_001):
        with pytest.raises(ProviderError):
            engine.search(SearchQuery(page=page))
    assert client.requests == []


def test_a_control_character_query_is_refused() -> None:
    engine, client = provider({})
    with pytest.raises(ProviderError) as caught:
        engine.search(SearchQuery(text="a\nb"))
    assert caught.value.kind == "validation"
    assert client.requests == []


def test_a_seed_only_makes_sense_with_random_sorting() -> None:
    filters = wallhaven.WallhavenFilters.from_query(
        SearchQuery(options={"sorting": "random", "seed": "aB3xY9"}), authenticated=False
    )
    assert "seed=aB3xY9" in filters.search_url()
    # A seed is dropped from the URL entirely when the sorting cannot use it.
    assert "seed" not in wallhaven.WallhavenFilters(sorting="toplist", seed="aB3xY9").search_url()


# -- disbelieving the JSON -----------------------------------------------


def test_a_media_url_that_names_another_wallpaper_is_refused() -> None:
    for path in (
        "https://w.wallhaven.cc/full/ab/wallhaven-zz9999.jpg",
        "https://w.wallhaven.cc/full/zz/wallhaven-ab1234.jpg",
        "https://evil.example/full/ab/wallhaven-ab1234.jpg",
        "https://w.wallhaven.cc/full/ab/wallhaven-ab1234.exe",
        "https://w.wallhaven.cc/full/ab/wallhaven-ab1234.jpg?x=1",
    ):
        with pytest.raises(ProviderError) as caught:
            wallhaven.cdn_url(path, "ab1234")
        assert caught.value.kind == "response"


def test_a_file_type_that_disagrees_with_the_url_is_refused() -> None:
    with pytest.raises(ProviderError) as caught:
        wallhaven.normalise_wallpaper(record(extension="jpg", file_type="image/png"), detail=False)
    assert caught.value.kind == "response"


def test_a_thumbnail_from_another_host_is_dropped_not_fatal() -> None:
    wallpaper = wallhaven.normalise_wallpaper(record(), detail=False)
    assert wallpaper.thumbnail_large.startswith(wallhaven.THUMB_ORIGIN)
    assert wallpaper.thumbnail_original == ""


def test_junk_in_a_record_is_clamped_rather_than_trusted() -> None:
    wallpaper = wallhaven.normalise_wallpaper(
        record(
            views="lots",
            favorites=True,
            file_size=float("inf"),
            dimension_x=-5,
            dimension_y=10**30,
            source="a\x00b" + "c" * 2000,
            colors="not a list",
        ),
        detail=False,
    )
    assert wallpaper.views == 0
    assert wallpaper.favorites == 0
    assert wallpaper.file_size == 0
    assert wallpaper.width == 0
    assert wallpaper.height == 1_000_000
    assert "\x00" not in wallpaper.source
    assert len(wallpaper.source.encode("utf-8")) <= wallhaven.MAX_TEXT_BYTES
    assert wallpaper.colors == ()


def test_tags_are_normalised_and_capped() -> None:
    many = [{"id": index + 1, "name": f"t{index}"} for index in range(200)]
    wallpaper = wallhaven.normalise_wallpaper(record(tags=many), detail=True)
    assert len(wallpaper.tags) == wallhaven.MAX_TAGS
    assert wallpaper.uploader == "someone"
    # A tag with no usable id or name is dropped rather than half-built.
    sparse = wallhaven.normalise_wallpaper(record(tags=[{"id": 0, "name": "x"}]), detail=True)
    assert sparse.tags == ()


@pytest.mark.parametrize(
    ("body", "content_type", "kind"),
    [
        (b"{not json", "application/json", "response"),
        (b"\xff\xfe\x00", "application/json", "response"),
        (b"<html></html>", "text/html", "content-type"),
        (b'["a list, not an object"]', "application/json", "response"),
        (b'{"data": "not a list"}', "application/json", "response"),
    ],
)
def test_malformed_search_responses_are_refused(body: bytes, content_type: str, kind: str) -> None:
    engine, _ = provider({SEARCH_URL: Reply(body=body, content_type=content_type)})
    with pytest.raises(ProviderError) as caught:
        engine.search(SearchQuery())
    assert caught.value.kind == kind


def test_one_bad_result_does_not_lose_the_page() -> None:
    engine, _ = provider(
        {
            SEARCH_URL: json_reply(
                {
                    "data": [
                        record("ab1234"),
                        {"id": "nope"},
                        record("cd5678"),
                        record("ef9012", file_type="image/png"),
                    ],
                    "meta": {"current_page": 1, "last_page": 4, "total": 96},
                }
            )
        }
    )
    result = engine.search(SearchQuery())
    assert [item.identifier for item in result.items] == ["ab1234", "cd5678"]
    assert result.dropped == 2
    assert result.total_hint == 96
    assert result.has_next and not result.has_previous
    assert result.items[0].kind is Kind.STILL


def test_a_repeat_search_is_answered_from_the_cache() -> None:
    engine, client = provider({SEARCH_URL: json_reply({"data": [record()], "meta": {}})})
    assert not engine.search(SearchQuery()).cached
    assert engine.search(SearchQuery()).cached
    assert len(client.requests) == 1


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "credential"), (429, "rate-limit"), (503, "remote"), (418, "http")],
)
def test_api_failures_are_named(status: int, kind: str) -> None:
    engine, _ = provider({SEARCH_URL: Reply(status=status, content_type="application/json")})
    with pytest.raises(ProviderError) as caught:
        engine.search(SearchQuery())
    assert caught.value.kind == kind


def test_an_api_redirect_is_refused() -> None:
    engine, _ = provider({SEARCH_URL: Reply(status=302, location="https://evil.example/")})
    with pytest.raises(ProviderError) as caught:
        engine.search(SearchQuery())
    assert caught.value.kind == "redirects"


def test_an_oversized_body_is_refused_by_the_transport() -> None:
    engine, _ = provider(
        {
            SEARCH_URL: Reply(
                body=b"x" * (wallhaven.MAX_BODY_BYTES + 1), content_type="application/json"
            )
        }
    )
    with pytest.raises(ProviderError) as caught:
        engine.search(SearchQuery())
    assert caught.value.kind == "size-limit"


def test_the_api_key_travels_as_a_header_and_only_when_present() -> None:
    engine, client = provider(
        {SEARCH_URL: json_reply({"data": [], "meta": {}})}, api_key="secret-key"
    )
    engine.search(SearchQuery())
    assert client.requests[0].headers == (("X-API-Key", "secret-key"),)
    assert "secret" not in client.requests[0].url

    anonymous, plain_client = provider({SEARCH_URL: json_reply({"data": [], "meta": {}})})
    anonymous.search(SearchQuery())
    assert plain_client.requests[0].headers == ()


def test_a_key_that_could_not_be_a_key_is_refused() -> None:
    with pytest.raises(ProviderError) as caught:
        wallhaven.Wallhaven(FakeClient(), api_key="has spaces and\nnewlines")
    assert caught.value.kind == "credential"


def test_details_for_a_different_wallpaper_are_refused() -> None:
    engine, _ = provider(
        {"https://wallhaven.cc/api/v1/w/ab1234": json_reply({"data": record("cd5678")})}
    )
    with pytest.raises(ProviderError) as caught:
        engine.detail("ab1234")
    assert caught.value.kind == "response"


def test_a_hostile_id_never_becomes_a_request() -> None:
    engine, client = provider({})
    for identifier in ("../../etc", "ABCDEF!", "", "toolongforanid"):
        with pytest.raises(ProviderError):
            engine.detail(identifier)
    assert client.requests == []


# -- image structure -----------------------------------------------------


def test_png_dimensions_come_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(png_bytes(7, 5))
    assert wallhaven.png_dimensions(path) == (7, 5)


def test_a_corrupt_png_is_refused(tmp_path: Path) -> None:
    payload = bytearray(png_bytes(4, 3))
    payload[30] ^= 0xFF  # inside IDAT: the CRC no longer matches
    path = tmp_path / "bad.png"
    path.write_bytes(bytes(payload))
    with pytest.raises(ProviderError) as caught:
        wallhaven.png_dimensions(path)
    assert caught.value.kind == "content-type"


def test_a_truncated_png_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "short.png"
    path.write_bytes(png_bytes(4, 3)[:-6])
    with pytest.raises(ProviderError):
        wallhaven.png_dimensions(path)


def test_jpeg_dimensions_come_from_the_frame_header(tmp_path: Path) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(jpeg_bytes(1600, 900))
    assert wallhaven.jpeg_dimensions(path) == (1600, 900)


def test_a_jpeg_without_its_terminal_marker_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.jpg"
    path.write_bytes(jpeg_bytes()[:-2] + b"\x00\x00")
    with pytest.raises(ProviderError) as caught:
        wallhaven.jpeg_dimensions(path)
    assert caught.value.kind == "content-type"


def test_a_jpeg_with_endless_padding_segments_is_bounded(tmp_path: Path) -> None:
    """A file that would otherwise walk 100k segments stops at the ceiling."""
    padding = b"\xff\xfe\x00\x02" * 5000  # empty COM segments
    path = tmp_path / "many.jpg"
    path.write_bytes(b"\xff\xd8" + padding + jpeg_bytes()[2:])
    with pytest.raises(ProviderError) as caught:
        wallhaven.jpeg_dimensions(path)
    assert caught.value.kind == "content-type"
    assert "too many segments" in str(caught.value)


# -- downloading ---------------------------------------------------------


def png_record(identifier: str = "ab1234", width: int = 4, height: int = 3) -> dict[str, object]:
    body = png_bytes(width, height)
    return record(
        identifier,
        "png",
        file_size=len(body),
        dimension_x=width,
        dimension_y=height,
        resolution=f"{width}x{height}",
    )


def download_routes(
    overrides: Mapping[str, Reply | list[Reply]] | None = None,
    identifier: str = "ab1234",
) -> dict[str, Reply | list[Reply]]:
    # Overrides come in as a mapping rather than keyword arguments: the keys are
    # URLs, which are not spellable as Python identifiers.
    body = png_bytes(4, 3)
    media = f"https://w.wallhaven.cc/full/{identifier[:2]}/wallhaven-{identifier}.png"
    routes: dict[str, Reply | list[Reply]] = {
        f"https://wallhaven.cc/api/v1/w/{identifier}": json_reply({"data": png_record(identifier)}),
        media: Reply(body=body, content_type="image/png"),
    }
    routes.update(overrides or {})
    return routes


def candidate(identifier: str = "ab1234") -> WallpaperCandidate:
    return WallpaperCandidate(
        provider="wallhaven",
        identifier=identifier,
        title=identifier,
        kind=Kind.STILL,
        page_url=f"https://wallhaven.cc/w/{identifier}",
    )


def managed(root: Path) -> Path:
    return root / "Wall-in-One" / "Wallhaven"


def installed(root: Path) -> set[str]:
    """Everything in the managed directory that is not our own bookkeeping.

    Tolerates the directory being absent, because it should be: a download
    refused on the metadata never gets as far as touching the disk, and
    creating a directory for a request that never fetched would be litter.
    """
    directory = managed(root)
    if not directory.is_dir():
        return set()
    return {entry.name for entry in directory.iterdir()} - {WALLHAVEN_LOCATION.marker_name}


def test_a_download_installs_media_marker_and_sidecar(tmp_path: Path) -> None:
    engine, _ = provider(download_routes())
    result = engine.download(candidate(), tmp_path)

    assert result.path == managed(tmp_path) / "wallhaven-ab1234.png"
    assert result.sidecar == managed(tmp_path) / "wallhaven-ab1234.png.wallhaven.json"
    assert result.marker.name == WALLHAVEN_LOCATION.marker_name
    assert result.path.read_bytes() == png_bytes(4, 3)
    assert result.kind is Kind.STILL

    sidecar = json.loads(result.sidecar.read_text())
    assert sidecar["provider"] == "Wallhaven"
    assert sidecar["id"] == "ab1234"
    assert sidecar["sha256"] == result.sha256


def test_a_wallpaper_already_in_the_library_is_not_fetched_again(tmp_path: Path) -> None:
    engine, client = provider(download_routes())
    engine.download(candidate(), tmp_path)
    before = len(client.requests)

    engine.clear_cache()
    engine, client = provider(download_routes())
    with pytest.raises(ProviderError) as caught:
        engine.download(candidate(), tmp_path)
    assert caught.value.kind == "conflict"
    assert before > 0


@pytest.mark.parametrize(
    ("overrides", "kind"),
    [
        ({"file_size": 999}, "size-mismatch"),
        ({"dimension_x": 99}, "dimensions"),
        ({"file_size": 0}, "validation"),
        ({"dimension_x": 60_000, "dimension_y": 60_000}, "validation"),
    ],
)
def test_pixels_that_disagree_with_the_metadata_are_never_installed(
    tmp_path: Path, overrides: dict[str, object], kind: str
) -> None:
    detail = png_record()
    detail.update(overrides)
    engine, _ = provider(
        download_routes({"https://wallhaven.cc/api/v1/w/ab1234": json_reply({"data": detail})})
    )
    with pytest.raises(ProviderError) as caught:
        engine.download(candidate(), tmp_path)
    assert caught.value.kind == kind
    assert installed(tmp_path) == set()


def test_a_media_response_of_the_wrong_type_is_never_installed(tmp_path: Path) -> None:
    engine, _ = provider(
        download_routes(
            {
                "https://w.wallhaven.cc/full/ab/wallhaven-ab1234.png": Reply(
                    body=png_bytes(4, 3), content_type="image/jpeg"
                )
            }
        )
    )
    with pytest.raises(ProviderError) as caught:
        engine.download(candidate(), tmp_path)
    assert caught.value.kind == "content-type"
    assert names_in(managed(tmp_path)) == {WALLHAVEN_LOCATION.marker_name}


def test_a_media_redirect_is_never_installed(tmp_path: Path) -> None:
    engine, _ = provider(
        download_routes(
            {
                "https://w.wallhaven.cc/full/ab/wallhaven-ab1234.png": Reply(
                    status=302, location="https://evil.example/x.png"
                )
            }
        )
    )
    with pytest.raises(ProviderError) as caught:
        engine.download(candidate(), tmp_path)
    assert caught.value.kind == "redirects"
    assert names_in(managed(tmp_path)) == {WALLHAVEN_LOCATION.marker_name}


def test_the_download_ceiling_is_the_advertised_size(tmp_path: Path) -> None:
    """More bytes than advertised is refused by the transport, not the parser."""
    engine, client = provider(download_routes())
    engine.download(candidate(), tmp_path)
    media_request = client.requests[-1]
    assert media_request.max_bytes == len(png_bytes(4, 3))
