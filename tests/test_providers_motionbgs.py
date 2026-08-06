"""MotionBGS: URL admission, bounded scraping, and a download that fails shut."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_providers_fakes import FakeClient, FrozenClock, Reply, mp4_bytes, names_in
from wall_in_one.library.model import Kind
from wall_in_one.providers import http, motionbgs
from wall_in_one.providers.base import ProviderError, SearchQuery, WallpaperCandidate
from wall_in_one.providers.download import MOTIONBGS_LOCATION

ORIGIN = motionbgs.ORIGIN

LISTING = """<!doctype html>
<html><head><title>1,234+ Live Wallpapers</title>
<link rel="prev" href="/">
<link rel="next" href="/3/">
</head><body>
<a href="/aurora-night/" title="Aurora Night Live Wallpaper 4K">
  <img data-src="https://motionbgs.com/i/c/320x180/media/42/aurora.jpg">
  <span class="ttl">Aurora Night</span><span class="frm">4K</span>
</a>
<a href="/rainy-street/" title="Rainy Street Live Wallpaper">
  <img src="https://motionbgs.com/media/43/rain.webp">
  <span class="ttl">Rainy Street</span><span class="frm">HD</span>
</a>
<a href="https://evil.example/steal/" title="Elsewhere Live Wallpaper">
  <span class="ttl">Elsewhere</span>
</a>
<a href="/aurora-night/" title="Aurora Night Live Wallpaper 4K">
  <span class="ttl">Aurora Night again</span>
</a>
</body></html>
"""

DETAIL = """<!doctype html>
<html><head><title>Aurora Night Live Wallpaper</title>
<meta property="og:title" content="Aurora Night Live Wallpaper">
<meta property="og:image" content="https://motionbgs.com/media/42/aurora.jpg">
<meta property="og:video" content="https://motionbgs.com/media/42/aurora.mp4">
</head><body>
<a href="/dl/hd/42/">HD 1920x1080 (12.5 MB)</a>
<a href="/dl/4k/42/">4K 3840x2160 (48.0 MB)</a>
<script type="application/ld+json">{"duration":"PT10S"}</script>
</body></html>
"""

MEDIA_URL = "https://motionbgs.com/media/42/aurora.mp4"


def html(body: str = LISTING) -> Reply:
    return Reply(body=body.encode("utf-8"), content_type="text/html")


def provider(routes: dict[str, Reply | list[Reply]]) -> tuple[motionbgs.MotionBgs, FakeClient]:
    client = FakeClient(routes=routes)
    clock = FrozenClock()
    return (
        motionbgs.MotionBgs(
            client, rate_limiter=http.RateLimiter(0.0, clock=clock, sleep=clock.sleep)
        ),
        client,
    )


# -- URL admission -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x",
        "http://motionbgs.com/x",
        "https://motionbgs.com.evil.example/x",
        "https://motionbgs.com:8080/x",
        "https://user@motionbgs.com/x",
        "https://motionbgs.com/a//b",
        "https://motionbgs.com/../etc/passwd",
        "https://motionbgs.com/a/../../b",
        "https://motionbgs.com/x#frag",
        "https://motionbgs.com/a\\b",
        "",
    ],
)
def test_urls_outside_the_origin_are_refused(url: str) -> None:
    with pytest.raises(ProviderError) as caught:
        motionbgs.normalise_url(url)
    assert caught.value.kind == "invalid-url"


def test_percent_encoded_traversal_is_decoded_before_it_is_checked() -> None:
    """`urlsplit` will not tell you `%2e%2e%2f` is a traversal. This does."""
    for hidden in ("/%2e%2e/x", "/a/%2E%2E%2Fb", "/a%5cb"):
        with pytest.raises(ProviderError) as caught:
            motionbgs.normalise_url("https://motionbgs.com" + hidden)
        assert caught.value.kind == "invalid-url"


def test_the_default_port_is_canonicalised_away() -> None:
    assert motionbgs.normalise_url("https://motionbgs.com:443/x/") == "https://motionbgs.com/x/"


def test_a_relative_url_resolves_against_the_page_it_came_from() -> None:
    assert (
        motionbgs.normalise_url("../other/", base="https://motionbgs.com/tag:x/2/")
        == "https://motionbgs.com/tag:x/other/"
    )


def test_media_urls_must_match_their_route() -> None:
    assert motionbgs.still_url("https://motionbgs.com/media/42/a.jpg")
    # Right host, wrong route: an HTML page dressed as an image.
    assert motionbgs.still_url("https://motionbgs.com/aurora-night/") == ""
    # A query string is never part of a media URL here.
    assert motionbgs.still_url("https://motionbgs.com/media/42/a.jpg?x=1") == ""
    # Absurd crop dimensions are a red flag, not a resize request.
    assert motionbgs.still_url("https://motionbgs.com/i/c/9999x9999/media/42/a.jpg") == ""
    assert motionbgs.video_url("https://motionbgs.com/media/42/a.mp4")
    assert motionbgs.video_url("https://motionbgs.com/media/42/a.exe") == ""
    assert motionbgs.video_url("https://evil.example/media/42/a.mp4") == ""


# -- bounded parsing -----------------------------------------------------


def test_a_page_with_too_many_tags_is_abandoned() -> None:
    markup = "<!doctype html>" + "<i>" * (motionbgs.MAX_HTML_TAGS + 1)
    with pytest.raises(ProviderError) as caught:
        motionbgs.parse_listing(
            markup, mode="latest", query="", genre="", page=1, source_url=ORIGIN + "/", limit=48
        )
    assert caught.value.kind == "site-markup"


def test_a_tag_with_too_many_attributes_is_abandoned() -> None:
    attributes = " ".join(f'a{index}="1"' for index in range(motionbgs.MAX_ATTRIBUTES_PER_TAG + 1))
    with pytest.raises(ProviderError) as caught:
        motionbgs.parse_listing(
            f"<!doctype html><div {attributes}>",
            mode="latest",
            query="",
            genre="",
            page=1,
            source_url=ORIGIN + "/",
            limit=48,
        )
    assert caught.value.kind == "site-markup"


def test_an_anti_bot_challenge_is_named_rather_than_worked_around() -> None:
    with pytest.raises(ProviderError) as caught:
        motionbgs.parse_listing(
            "<html><title>Just a moment...</title>",
            mode="latest",
            query="",
            genre="",
            page=1,
            source_url=ORIGIN + "/",
            limit=48,
        )
    assert caught.value.kind == "challenge"


def test_markup_with_no_cards_and_no_empty_notice_is_a_markup_change() -> None:
    with pytest.raises(ProviderError) as caught:
        motionbgs.parse_listing(
            "<!doctype html><html><body></body></html>",
            mode="latest",
            query="",
            genre="",
            page=1,
            source_url=ORIGIN + "/",
            limit=48,
        )
    assert caught.value.kind == "site-markup"


def test_an_empty_result_page_is_not_an_error() -> None:
    page = motionbgs.parse_listing(
        "<!doctype html><html><body>No wallpapers found</body></html>",
        mode="latest",
        query="",
        genre="",
        page=1,
        source_url=ORIGIN + "/",
        limit=48,
    )
    assert page.items == ()


def test_html_must_be_html_in_the_header_and_the_body() -> None:
    with pytest.raises(ProviderError) as caught:
        motionbgs.validate_html("text/plain", b"<!doctype html>")
    assert caught.value.kind == "content-type"

    with pytest.raises(ProviderError) as caught:
        motionbgs.validate_html("text/html", b"GIF89a")
    assert caught.value.kind == "content-type"

    with pytest.raises(ProviderError) as caught:
        motionbgs.validate_html("text/html", b"<!doctype html>\x00")
    assert caught.value.kind == "content-type"


# -- searching -----------------------------------------------------------


def test_a_listing_yields_only_same_origin_cards_and_no_duplicates() -> None:
    engine, client = provider({ORIGIN + "/2/": html()})
    result = engine.search(SearchQuery(page=2, options={"mode": "latest"}))

    assert [item.identifier for item in result.items] == ["aurora-night", "rainy-street"]
    assert result.items[0].title == "Aurora Night"
    assert result.items[0].kind is Kind.VIDEO
    assert result.items[0].thumbnail_url == "https://motionbgs.com/i/c/320x180/media/42/aurora.jpg"
    assert result.items[0].variant == "4k"
    assert result.has_previous and result.has_next
    assert result.total_hint == 1234
    assert len(client.requests) == 1


def test_a_repeat_search_is_answered_from_the_cache() -> None:
    engine, client = provider({ORIGIN + "/": html()})
    first = engine.search(SearchQuery(options={"mode": "latest"}))
    second = engine.search(SearchQuery(options={"mode": "latest"}))
    assert not first.cached and second.cached
    assert len(client.requests) == 1

    engine.clear_cache()
    third = engine.search(SearchQuery(options={"mode": "latest"}))
    assert not third.cached
    assert len(client.requests) == 2


def test_a_redirect_into_another_catalog_route_is_refused() -> None:
    engine, _ = provider(
        {
            ORIGIN + "/4k/": Reply(status=302, location="/tag:cars/"),
            ORIGIN + "/tag:cars/": html(),
        }
    )
    with pytest.raises(ProviderError) as caught:
        engine.search(SearchQuery(options={"mode": "4k"}))
    assert caught.value.kind == "redirects"


def test_a_search_redirected_to_its_own_tag_page_is_allowed() -> None:
    engine, _ = provider(
        {
            ORIGIN + "/search?q=aurora": Reply(status=302, location="/tag:aurora/"),
            ORIGIN + "/tag:aurora/": html(),
        }
    )
    result = engine.search(SearchQuery(text="aurora"))
    assert len(result) == 2


def test_a_cross_origin_redirect_fails_closed() -> None:
    engine, _ = provider({ORIGIN + "/": Reply(status=302, location="https://evil.example/x")})
    with pytest.raises(ProviderError) as caught:
        engine.search(SearchQuery(options={"mode": "latest"}))
    assert caught.value.kind == "invalid-url"


def test_an_endless_redirect_chain_terminates() -> None:
    engine, _ = provider({ORIGIN + "/": [Reply(status=302, location="/")] * 8})
    with pytest.raises(ProviderError) as caught:
        engine.search(SearchQuery(options={"mode": "latest"}))
    assert caught.value.kind == "redirects"


def test_a_challenge_status_is_not_retried() -> None:
    for status in (403, 429):
        engine, _ = provider({ORIGIN + "/": Reply(status=status, content_type="text/html")})
        with pytest.raises(ProviderError) as caught:
            engine.search(SearchQuery(options={"mode": "latest"}))
        assert caught.value.kind == "challenge"


@pytest.mark.parametrize(
    "query",
    [
        SearchQuery(options={"mode": "sideways"}),
        SearchQuery(text="a", page=2),
        SearchQuery(page=0, options={"mode": "latest"}),
        SearchQuery(text="a", options={"mode": "latest"}),
        SearchQuery(options={"mode": "genre"}),
        SearchQuery(options={"mode": "genre", "genre": "NOT A SLUG"}),
        SearchQuery(options={"mode": "latest", "genre": "cars"}),
        SearchQuery(text="  ", options={"mode": "search"}),
        SearchQuery(text="x" * 200),
        SearchQuery(options={"mode": "hd"}, page=2),
    ],
)
def test_bad_searches_are_refused_before_any_request(query: SearchQuery) -> None:
    engine, client = provider({})
    with pytest.raises(ProviderError):
        engine.search(query)
    assert client.requests == []


def test_an_unknown_option_is_refused_rather_than_ignored() -> None:
    engine, client = provider({})
    with pytest.raises(ProviderError) as caught:
        engine.search(SearchQuery(options={"mode": "latest", "nsfw": "yes"}))
    assert caught.value.kind == "invalid-request"
    assert client.requests == []


def test_requests_are_spaced_out() -> None:
    client = FakeClient(routes={ORIGIN + "/": html(), ORIGIN + "/2/": html()})
    clock = FrozenClock()
    engine = motionbgs.MotionBgs(
        client, rate_limiter=http.RateLimiter(1.0, clock=clock, sleep=clock.sleep)
    )
    engine.search(SearchQuery(options={"mode": "latest"}))
    engine.search(SearchQuery(page=2, options={"mode": "latest"}))
    assert clock.slept == [1.0]


# -- details -------------------------------------------------------------


def test_a_detail_page_yields_both_qualities() -> None:
    engine, _ = provider({ORIGIN + "/aurora-night": html(DETAIL)})
    detail = engine.detail("aurora-night")
    assert detail.title == "Aurora Night"
    assert detail.media_id == "42"
    assert detail.preview_url == MEDIA_URL
    assert detail.poster_url == "https://motionbgs.com/media/42/aurora.jpg"
    assert detail.duration == "PT10S"
    assert [option.quality for option in detail.downloads] == ["4k", "hd"]
    assert detail.option("hd").resolution == "1920x1080"
    assert detail.option("hd").advertised_size_mb == 12.5
    # No quality named means the best one.
    assert detail.option("").quality == "4k"


def test_a_detail_page_without_download_links_is_a_markup_change() -> None:
    engine, _ = provider({ORIGIN + "/aurora-night": html("<!doctype html><html></html>")})
    with pytest.raises(ProviderError) as caught:
        engine.detail("aurora-night")
    assert caught.value.kind == "site-markup"


def test_a_detail_page_redirected_elsewhere_is_refused() -> None:
    engine, _ = provider(
        {
            ORIGIN + "/aurora-night": Reply(status=302, location="/rainy-street"),
            ORIGIN + "/rainy-street": html(DETAIL),
        }
    )
    with pytest.raises(ProviderError) as caught:
        engine.detail("aurora-night")
    assert caught.value.kind == "redirects"


def test_a_hostile_slug_never_becomes_a_request() -> None:
    engine, client = provider({})
    for slug in ("../etc/passwd", "Aurora", "a" * 200, ""):
        with pytest.raises(ProviderError):
            engine.detail(slug)
    assert client.requests == []


# -- downloading ---------------------------------------------------------


def candidate(variant: str = "hd") -> WallpaperCandidate:
    return WallpaperCandidate(
        provider="motionbgs",
        identifier="aurora-night",
        title="Aurora Night",
        kind=Kind.VIDEO,
        page_url=ORIGIN + "/aurora-night",
        variant=variant,
    )


def download_routes(**overrides: Reply | list[Reply]) -> dict[str, Reply | list[Reply]]:
    routes: dict[str, Reply | list[Reply]] = {
        ORIGIN + "/aurora-night": html(DETAIL),
        ORIGIN + "/dl/hd/42/": Reply(status=302, location=MEDIA_URL),
        MEDIA_URL: Reply(body=mp4_bytes(), content_type="video/mp4"),
    }
    routes.update(overrides)
    return routes


def managed(root: Path) -> Path:
    return root / "Wall-in-One" / "MotionBGS"


def test_a_download_installs_media_marker_and_sidecar(tmp_path: Path) -> None:
    engine, _ = provider(download_routes())
    result = engine.download(candidate(), tmp_path)

    assert result.path == managed(tmp_path) / "aurora-night.hd.mp4"
    assert result.sidecar == managed(tmp_path) / "aurora-night.hd.mp4.motionbgs.json"
    assert result.marker.name == MOTIONBGS_LOCATION.marker_name
    assert result.download_url == MEDIA_URL
    assert result.kind is Kind.VIDEO
    assert result.path.read_bytes() == mp4_bytes()

    sidecar = json.loads(result.sidecar.read_text())
    assert sidecar["provider"] == "MotionBGS"
    assert sidecar["quality"] == "hd"
    assert sidecar["sha256"] == result.sha256
    assert sidecar["bytes"] == result.size


def test_a_download_redirected_to_another_media_id_installs_nothing(tmp_path: Path) -> None:
    engine, _ = provider(
        download_routes(
            **{
                ORIGIN + "/dl/hd/42/": Reply(status=302, location="/media/99/other.mp4"),
                "https://motionbgs.com/media/99/other.mp4": Reply(
                    body=mp4_bytes(), content_type="video/mp4"
                ),
            }
        )
    )
    with pytest.raises(ProviderError) as caught:
        engine.download(candidate(), tmp_path)
    assert caught.value.kind == "redirects"
    assert names_in(managed(tmp_path)) == {MOTIONBGS_LOCATION.marker_name}


def test_a_download_redirected_off_origin_installs_nothing(tmp_path: Path) -> None:
    engine, _ = provider(
        download_routes(
            **{ORIGIN + "/dl/hd/42/": Reply(status=302, location="https://evil.example/x.mp4")}
        )
    )
    with pytest.raises(ProviderError) as caught:
        engine.download(candidate(), tmp_path)
    assert caught.value.kind == "invalid-url"
    assert names_in(managed(tmp_path)) == {MOTIONBGS_LOCATION.marker_name}


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (Reply(body=b"not an mp4 at all, honestly", content_type="video/mp4"), "content-type"),
        (Reply(body=mp4_bytes(), content_type="text/html"), "content-type"),
        (Reply(status=500, content_type="text/html"), "http"),
    ],
)
def test_bad_download_bytes_never_reach_the_library(
    tmp_path: Path, reply: Reply, kind: str
) -> None:
    engine, _ = provider(download_routes(**{MEDIA_URL: reply}))
    with pytest.raises(ProviderError) as caught:
        engine.download(candidate(), tmp_path)
    assert caught.value.kind == kind
    assert names_in(managed(tmp_path)) == {MOTIONBGS_LOCATION.marker_name}


def test_an_existing_file_is_never_replaced(tmp_path: Path) -> None:
    """The user's own file keeps its name and its bytes; ours counts up."""
    directory = managed(tmp_path)
    directory.mkdir(parents=True)
    occupied = directory / "aurora-night.hd.mp4"
    occupied.write_bytes(b"user-owned sentinel")
    stray_sidecar = directory / "aurora-night.hd-1.mp4.motionbgs.json"
    stray_sidecar.write_bytes(b"an interrupted install")

    engine, _ = provider(download_routes())
    result = engine.download(candidate(), tmp_path)

    assert occupied.read_bytes() == b"user-owned sentinel"
    assert stray_sidecar.read_bytes() == b"an interrupted install"
    assert result.path.name == "aurora-night.hd-2.mp4"


def test_a_quality_the_page_does_not_offer_is_refused(tmp_path: Path) -> None:
    engine, _ = provider(
        {
            ORIGIN + "/aurora-night": html(
                DETAIL.replace('<a href="/dl/4k/42/">4K 3840x2160 (48.0 MB)</a>', "")
            )
        }
    )
    with pytest.raises(ProviderError) as caught:
        engine.download(candidate("4k"), tmp_path)
    assert caught.value.kind == "validation"


def test_an_invented_quality_never_becomes_a_request(tmp_path: Path) -> None:
    engine, client = provider({})
    with pytest.raises(ProviderError) as caught:
        engine.download(candidate("8k"), tmp_path)
    assert caught.value.kind == "validation"
    assert client.requests == []


def test_the_download_route_check_binds_quality_and_media_id() -> None:
    assert motionbgs.validate_download_route(ORIGIN + "/dl/hd/42/", "hd", "42")
    assert motionbgs.validate_download_route(MEDIA_URL, "hd", "42")
    for value, quality, media_id in (
        (ORIGIN + "/dl/4k/42/", "hd", "42"),
        (ORIGIN + "/dl/hd/99/", "hd", "42"),
        ("https://motionbgs.com/media/99/x.mp4", "hd", "42"),
        (ORIGIN + "/aurora-night/", "hd", "42"),
        (ORIGIN + "/dl/hd/42/?x=1", "hd", "42"),
    ):
        with pytest.raises(ProviderError) as caught:
            motionbgs.validate_download_route(value, quality, media_id)
        assert caught.value.kind == "redirects"


def test_the_mp4_check_reads_the_file_not_the_header(tmp_path: Path) -> None:
    good = tmp_path / "good.mp4"
    good.write_bytes(mp4_bytes())
    size, digest = motionbgs.validate_mp4(good, "video/mp4")
    assert size == good.stat().st_size
    assert len(digest) == 64

    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 4 + b"junk" + b"\x00" * 32)
    with pytest.raises(ProviderError) as caught:
        motionbgs.validate_mp4(bad, "video/mp4")
    assert caught.value.kind == "content-type"
