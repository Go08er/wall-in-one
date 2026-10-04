"""The new Store's filters and pages, with no display: what is sent, and what gets a card.

`StoreQuery` is what the page's controls say; `real_store.filters_for` and
`search_request` turn it into the classic Store's `browse.Filters`, which owns
every provider rule. Each mapped option is checked against the provider's own
validator, so a filter the site would refuse cannot be offered. `ResultPages`
is the bounded-pages model (DECISIONS.md: 40 cards at most, Previous/Next).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from wall_in_one import browse
from wall_in_one.providers import wallhaven
from wall_in_one.providers.base import ProviderError, SearchQuery
from wall_in_one.providers.motionbgs import _plan_search
from wall_in_one.ui.next.real_store import filters_for, search_request
from wall_in_one.ui.next.store_paging import ResultPages
from wall_in_one.ui.next.store_state import (
    COLOURS,
    MOTIONBGS,
    PAGE_SIZE,
    RATIOS,
    SIZES,
    SORTS,
    TOP_RANGES,
    WALLHAVEN,
    StoreQuery,
    StoreResults,
    bits,
    nearest_colour,
    short_resolution,
)

# -- the option tables ---------------------------------------------------------------------


def test_the_palette_is_every_colour_wallhaven_accepts_in_its_order() -> None:
    """The 29-colour filter: a swatch the API would refuse is a search that fails."""
    assert COLOURS == wallhaven.COLOR_ORDER
    assert len(COLOURS) == 29


def test_every_sort_and_top_range_is_wallhavens() -> None:
    assert {key for key, _label in SORTS} == wallhaven.SORTING
    assert {row[0] for row in TOP_RANGES} == wallhaven.TOP_RANGE


def test_three_bit_masks_never_ask_for_nothing() -> None:
    names = ("General", "Anime", "People")
    assert bits(frozenset(names), names) == "111"
    assert bits(frozenset({"General", "People"}), names) == "101"
    assert bits(frozenset({"Anime"}), names) == "010"
    # Wallhaven reads 000 as an error; nothing chosen means the first.
    assert bits(frozenset(), names) == "100"


# -- StoreQuery -> browse.Filters ------------------------------------------------------------


def test_the_opening_view_is_popular_this_week_safe_for_work() -> None:
    filters = filters_for(StoreQuery())
    options = filters.wallhaven_options()
    assert options == {
        "sorting": "toplist",
        "order": "desc",
        "categories": "111",
        "purity": "100",
        "top_range": "1w",
    }


def test_a_minimum_size_is_wallhavens_atleast_and_a_ratio_its_ratios() -> None:
    query = StoreQuery(size="2160", ratio="21x9", colour="0066cc", sort="views")
    options = filters_for(query).wallhaven_options()
    assert options["atleast"] == "3840x2160"
    assert options["ratios"] == "21x9"
    assert options["colors"] == "0066cc"
    # "Top list covers" means nothing to another sorting, so it is not sent.
    assert "top_range" not in options


def test_categories_and_purity_are_three_bit_masks() -> None:
    query = StoreQuery(
        categories=frozenset({"General", "People"}), purity=frozenset({"SFW", "Sketchy"})
    )
    options = filters_for(query).wallhaven_options()
    assert options["categories"] == "101"
    assert options["purity"] == "110"


def test_reverse_order_is_ascending_only_where_an_order_means_something() -> None:
    assert filters_for(StoreQuery(sort="views", reverse=True)).order == "asc"
    assert filters_for(StoreQuery(sort="random", reverse=True)).order == "desc"
    assert filters_for(StoreQuery(sort="relevance", reverse=True)).order == "desc"


def test_a_colour_outside_the_palette_is_not_sent() -> None:
    assert filters_for(StoreQuery(colour="123456")).colour == ""


@pytest.mark.parametrize("size", [row[0] for row in SIZES])
@pytest.mark.parametrize("ratio", [row[0] for row in RATIOS])
def test_every_size_and_ratio_passes_wallhavens_own_validator(size: str, ratio: str) -> None:
    request, _seed = search_request(StoreQuery(size=size, ratio=ratio, colour="ffffff"), 1)
    parsed = wallhaven.WallhavenFilters.from_query(request, authenticated=False)
    assert parsed.atleast == next(row[2] for row in SIZES if row[0] == size)
    assert parsed.ratios == next(row[2] for row in RATIOS if row[0] == ratio)


@pytest.mark.parametrize("sort", [row[0] for row in SORTS])
@pytest.mark.parametrize("top_range", [row[0] for row in TOP_RANGES])
def test_every_sort_and_range_passes_wallhavens_own_validator(sort: str, top_range: str) -> None:
    request, _seed = search_request(StoreQuery(text="sky", sort=sort, top_range=top_range), 1)
    wallhaven.WallhavenFilters.from_query(request, authenticated=False)


def test_nsfw_is_refused_by_wallhaven_without_a_key() -> None:
    request, _seed = search_request(StoreQuery(purity=frozenset({"NSFW"})), 1)
    with pytest.raises(ProviderError) as caught:
        wallhaven.WallhavenFilters.from_query(request, authenticated=False)
    assert caught.value.kind == "credential"


def test_a_random_search_keeps_its_seed_across_pages_and_rerolls_when_new() -> None:
    first, seed = search_request(StoreQuery(sort="random"), 1)
    assert len(seed) == browse.SEED_LENGTH and first.options["seed"] == seed
    second, kept = search_request(StoreQuery(sort="random"), 2, seed)
    assert kept == seed and second.options["seed"] == seed and second.page == 2
    # Another sorting drops it: Wallhaven refuses a stale seed outright.
    other, dropped = search_request(StoreQuery(sort="views"), 1, seed)
    assert dropped == "" and "seed" not in other.options


# -- MotionBGS -------------------------------------------------------------------------------


def test_motionbgs_genre_is_its_genre_browse_mode() -> None:
    query = StoreQuery(provider=MOTIONBGS, mode="genre", genre=" anime ")
    request, seed = search_request(query, 1)
    assert seed == ""
    assert request == SearchQuery(text="", page=1, options={"mode": "genre", "genre": "anime"})
    assert _plan_search(request)[:3] == ("genre", "", "anime")


@pytest.mark.parametrize("mode", ["latest", "4k", "hd"])
def test_motionbgs_browse_modes_pass_its_own_planner(mode: str) -> None:
    request, _seed = search_request(StoreQuery(provider=MOTIONBGS, mode=mode), 1)
    assert request.options == {"mode": mode}
    assert _plan_search(request)[0] == mode


def test_typing_searches_motionbgs_instead_of_browsing() -> None:
    query = StoreQuery(provider=MOTIONBGS, text="forest", mode="genre", genre="anime")
    request, _seed = search_request(query, 1)
    assert request.options == {"mode": "search"}
    assert _plan_search(request)[:2] == ("search", "forest")


def test_an_empty_genre_is_refused_with_words_about_the_box() -> None:
    with pytest.raises(ProviderError) as caught:
        search_request(StoreQuery(provider=MOTIONBGS, mode="genre", genre="  "), 1)
    assert caught.value.kind == "validation"
    assert "type a genre" in str(caught.value)


def test_motionbgs_quality_is_a_download_variant_not_a_search_option() -> None:
    plain, _ = search_request(StoreQuery(provider=MOTIONBGS), 1)
    chosen, _ = search_request(StoreQuery(provider=MOTIONBGS, quality="4k"), 1)
    assert plain == chosen


# -- badges and colours ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resolution", "badge"),
    [
        ("3840x2160", "4K"),
        ("5120x2880", "5K"),
        ("2560x1440", "1440p"),
        ("2560x1600", "1600p · 16:10"),
        ("3440x1440", "1440p · 21:9"),
        ("1440x2560", "1440p · Portrait"),
        ("3840 \u00d7 2160", "4K"),
        ("4K", "4K"),
        ("", ""),
    ],
)
def test_a_card_badge_is_short(resolution: str, badge: str) -> None:
    assert short_resolution(resolution) == badge


def test_a_detail_colour_finds_the_nearest_search_colour() -> None:
    assert nearest_colour("0066cc") == "0066cc"
    assert nearest_colour("#0167cb") == "0066cc"
    assert nearest_colour("nonsense") == ""


# -- bounded pages ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    id: str
    provider: str = WALLHAVEN
    title: str = ""
    is_moving: bool = False
    resolution: str = ""
    detail: str = ""


def _answer(*names: str, has_next: bool = False) -> StoreResults:
    return StoreResults(items=tuple(Item(name) for name in names), has_next=has_next)


def _names(count: int, start: int = 0) -> list[str]:
    return [f"r{index}" for index in range(start, start + count)]


def test_a_large_provider_page_becomes_pages_of_at_most_forty() -> None:
    pages = ResultPages()
    pages.add(_answer(*_names(100)), 1)
    assert PAGE_SIZE == 40
    assert pages.pages == 3
    assert [len(pages.current)] == [40]
    assert pages.show(2) and len(pages.current) == 20
    assert not pages.show(3)


def test_each_provider_page_keeps_its_own_pages_so_next_never_skips() -> None:
    """Wallhaven answers 24 at a time: page two is the next 24, never a regrown first page."""
    pages = ResultPages()
    pages.add(_answer(*_names(24), has_next=True), 1)
    assert pages.label() == "Page 1 of 1 · more online"
    assert pages.can_next and not pages.next_is_kept
    pages.add(_answer(*_names(24, 24), has_next=True), 2)
    assert pages.pages == 2 and pages.provider_page == 2
    assert [item.id for item in pages.current] == _names(24)
    assert pages.show(1)
    assert [item.id for item in pages.current] == _names(24, 24)
    assert pages.can_previous


def test_an_overlapping_page_adds_only_what_is_new() -> None:
    pages = ResultPages()
    pages.add(_answer("a", "b", "c", has_next=True), 1)
    added = pages.add(_answer("c", "d", has_next=True), 2)
    assert added == 1
    assert [item.id for item in pages.items] == ["a", "b", "c", "d"]


def test_a_page_of_repeats_stops_asking_for_more() -> None:
    pages = ResultPages()
    pages.add(_answer("a", "b", has_next=True), 1)
    assert pages.add(_answer("a", "b", has_next=True), 2) == 0
    assert not pages.has_more and not pages.can_next
    assert pages.pages == 1


def test_results_have_an_honest_ceiling() -> None:
    pages = ResultPages(page_size=40, ceiling=50)
    pages.add(_answer(*_names(40), has_next=True), 1)
    pages.add(_answer(*_names(40, 40), has_next=True), 2)
    assert pages.count == 50 and pages.capped and not pages.has_more
    assert pages.summary().startswith("the first 50 results")


def test_a_failed_next_page_keeps_every_page_reached() -> None:
    pages = ResultPages()
    pages.add(_answer(*_names(30), has_next=True), 1)
    pages.stop()
    assert pages.pages == 1 and not pages.can_next


def test_the_summary_counts_every_result_kept() -> None:
    pages = ResultPages()
    pages.add(StoreResults(items=(Item("a"), Item("b")), total_hint=1200, dropped=1), 1)
    assert pages.summary() == "2 results of about 1,200 · 1 unreadable"
    pages.reset()
    assert pages.summary() == "0 results" and pages.label() == ""
