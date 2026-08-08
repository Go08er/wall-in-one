"""The layer between the window and the providers: selection, roots, downloads."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_providers_fakes import FakeClient, Reply, png_bytes
from wall_in_one import browse
from wall_in_one.browse import Browser, Downloaded
from wall_in_one.library.model import Kind
from wall_in_one.providers import wallhaven
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


# -- filters -------------------------------------------------------------
#
# The dialog reads its widgets into a `Filters` and asks for a query back.
# What is worth pinning is the conditional parts: Wallhaven refuses a seed
# outside random sorting rather than ignoring it, so a control that always
# sent one would turn changing the sort order into an error message.


def test_default_filters_ask_for_nothing_unusual() -> None:
    query = browse.Filters(text="mountain").to_query(browse.WALLHAVEN)
    assert query.text == "mountain"
    assert query.options == {
        "sorting": "date_added",
        "order": "desc",
        "categories": "111",
        "purity": "100",
    }


def test_the_optional_wallhaven_filters_appear_only_when_set() -> None:
    filters = browse.Filters(atleast="1920x1080", ratios="16x9", colour="0066cc")
    options = filters.to_query(browse.WALLHAVEN).options
    assert options["atleast"] == "1920x1080"
    assert options["ratios"] == "16x9"
    assert options["colors"] == "0066cc"


def test_the_top_range_is_sent_only_for_the_toplist() -> None:
    """Wallhaven ignores it elsewhere, so sending it would misdescribe the URL."""
    assert "top_range" not in browse.Filters(sorting="hot").to_query(browse.WALLHAVEN).options
    ranged = browse.Filters(sorting="toplist", top_range="1w").to_query(browse.WALLHAVEN)
    assert ranged.options["top_range"] == "1w"


def test_a_seed_is_generated_for_random_sorting() -> None:
    """Without one, page two of a random search overlaps page one."""
    filters = browse.Filters(sorting="random").seeded()
    assert len(filters.seed) == browse.SEED_LENGTH
    assert filters.seed.isalnum()
    assert filters.to_query(browse.WALLHAVEN).options["seed"] == filters.seed


def test_an_existing_seed_is_kept_so_paging_stays_put() -> None:
    filters = browse.Filters(sorting="random", seed="abc123")
    assert filters.seeded().seed == "abc123"


def test_a_stale_seed_is_dropped_when_the_sorting_changes() -> None:
    """Wallhaven refuses a seed outside random sorting, rather than ignoring it.

    So a user who searches randomly and then switches to "Top list" would get
    a validation error instead of results.
    """
    filters = browse.Filters(sorting="toplist", seed="abc123").seeded()
    assert filters.seed == ""
    assert "seed" not in filters.to_query(browse.WALLHAVEN).options


def test_seeds_differ_between_searches() -> None:
    seeds = {browse.new_seed() for _ in range(20)}
    assert len(seeds) > 1


def test_motionbgs_gets_its_own_shape() -> None:
    options = browse.Filters(mode="genre", genre="anime").to_query(browse.MOTIONBGS).options
    assert options == {"mode": "genre", "genre": "anime"}


def test_typing_a_query_overrides_the_motionbgs_browse_mode() -> None:
    """MotionBGS rejects a query and a browse mode together."""
    options = browse.Filters(text="kakashi", mode="latest").to_query(browse.MOTIONBGS).options
    assert options["mode"] == "search"


def test_wallhaven_accepts_every_option_the_filters_produce() -> None:
    """The two halves have to agree, and only one of them is in this module.

    `WallhavenFilters.from_query` refuses an unknown option outright, so a
    name misspelled here would fail at the website rather than in a test.
    """
    filters = browse.Filters(
        text="forest",
        sorting="random",
        order="asc",
        atleast="2560x1440",
        ratios="16x9,21x9",
        colour="336600",
    ).seeded()
    parsed = wallhaven.WallhavenFilters.from_query(
        filters.to_query(browse.WALLHAVEN), authenticated=False
    )
    assert parsed.seed == filters.seed
    assert parsed.colors == "336600"
    assert parsed.ratios == "16x9,21x9"
    assert parsed.order == "asc"
