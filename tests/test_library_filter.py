from __future__ import annotations

import unicodedata
from pathlib import Path

from wall_in_one.library import filter as library_filter
from wall_in_one.library.filter import Kinds, Query, Sort
from wall_in_one.library.model import Kind, MediaItem


def _item(
    directory: Path,
    name: str,
    kind: Kind = Kind.STILL,
    *,
    size: int = 1,
    mtime: int = 0,
) -> MediaItem:
    """One library entry under ``directory``. Nothing is written to disk:
    matching and sorting read the model, never the filesystem."""
    suffix = ".mp4" if kind is Kind.VIDEO else ".png"
    return MediaItem(path=directory / f"{name}{suffix}", kind=kind, size=size, mtime=mtime)


def _names(items: tuple[MediaItem, ...]) -> list[str]:
    return [item.name for item in items]


# -- matching ------------------------------------------------------------


def test_an_empty_query_matches_every_name() -> None:
    assert library_filter.matches("anything at all", library_filter.terms(""))
    assert library_filter.matches("", library_filter.terms("   "))


def test_matching_ignores_case_on_both_sides() -> None:
    assert library_filter.matches("Snowy-Village", library_filter.terms("SNOWY"))
    assert library_filter.matches("SNOWY-VILLAGE", library_filter.terms("snowy"))


def test_every_word_of_the_query_must_appear_somewhere_in_the_name() -> None:
    """The rule the whole search box rests on: `snow vil` finds
    `snowy-village-still`, which plain substring matching cannot."""
    wanted = library_filter.terms("snow vil")
    assert library_filter.matches("snowy-village-still", wanted)
    assert not library_filter.matches("snowy-mountain", wanted)


def test_the_words_may_be_given_in_any_order() -> None:
    assert library_filter.matches("snowy-village", library_filter.terms("village snow"))


def test_a_word_may_start_inside_a_word_of_the_name() -> None:
    assert library_filter.matches("wallhaven-2k3l9x", library_filter.terms("haven"))


def test_repeated_spaces_do_not_become_a_term_that_matches_nothing() -> None:
    assert library_filter.terms("  snow   vil  ") == ("snow", "vil")


def test_an_accented_name_is_found_by_its_unaccented_spelling() -> None:
    assert library_filter.matches("Café-Terrasse", library_filter.terms("cafe"))
    assert library_filter.matches("cafe-terrasse", library_filter.terms("café"))


def test_a_decomposed_name_matches_a_precomposed_query() -> None:
    """The same filename off a Mac and off a Linux box are different strings."""
    decomposed = unicodedata.normalize("NFD", "caf\u00e9-terrasse")
    precomposed = unicodedata.normalize("NFC", "caf\u00e9")
    assert decomposed != unicodedata.normalize("NFC", decomposed)

    assert library_filter.matches(decomposed, library_filter.terms(precomposed))
    assert library_filter.matches(precomposed, library_filter.terms(decomposed[:5]))


def test_non_latin_names_match_themselves_and_not_each_other() -> None:
    assert library_filter.matches("東京-夜景", library_filter.terms("夜景"))
    assert not library_filter.matches("東京-夜景", library_filter.terms("札幌"))


def test_sharp_s_folds_the_way_casefold_says() -> None:
    assert library_filter.matches("Straße-bei-Nacht", library_filter.terms("STRASSE"))


# -- filtering -----------------------------------------------------------


def test_a_query_that_matches_nothing_returns_nothing(tmp_path: Path) -> None:
    items = (_item(tmp_path, "snowy-village"), _item(tmp_path, "cabin"))
    assert library_filter.apply(items, Query(text="marzipan")) == ()


def test_an_empty_query_keeps_everything(tmp_path: Path) -> None:
    items = (_item(tmp_path, "beta"), _item(tmp_path, "alpha"))
    assert _names(library_filter.apply(items, Query())) == ["alpha", "beta"]


def test_stills_only_and_videos_only_split_the_library(tmp_path: Path) -> None:
    still = _item(tmp_path, "cabin")
    video = _item(tmp_path, "aurora", Kind.VIDEO)
    items = (still, video)

    assert library_filter.apply(items, Query(kinds=Kinds.STILLS)) == (still,)
    assert library_filter.apply(items, Query(kinds=Kinds.VIDEOS)) == (video,)
    assert set(library_filter.apply(items, Query(kinds=Kinds.EVERYTHING))) == set(items)


def test_the_kind_filter_and_the_search_narrow_together(tmp_path: Path) -> None:
    items = (
        _item(tmp_path, "snowy-village", Kind.VIDEO),
        _item(tmp_path, "snowy-village-alt"),
        _item(tmp_path, "sunny-beach", Kind.VIDEO),
    )
    found = library_filter.apply(items, Query(text="snow", kinds=Kinds.VIDEOS))
    assert _names(found) == ["snowy-village"]


def test_searching_reads_the_stem_and_not_the_extension_or_the_directory(tmp_path: Path) -> None:
    """Otherwise every term naming a folder would match everything inside it."""
    nested = tmp_path / "winter"
    item = _item(nested, "cabin")

    assert library_filter.apply((item,), Query(text="cabin")) == (item,)
    assert library_filter.apply((item,), Query(text="winter")) == ()
    assert library_filter.apply((item,), Query(text="png")) == ()


def test_only_a_search_or_a_kind_counts_as_narrowing() -> None:
    assert not Query().narrows
    assert not Query(sort=Sort.LARGEST).narrows
    assert not Query(text="   ").narrows
    assert Query(text="snow").narrows
    assert Query(kinds=Kinds.VIDEOS).narrows


# -- sorting -------------------------------------------------------------


def test_name_order_ignores_case_and_accents(tmp_path: Path) -> None:
    items = (
        _item(tmp_path, "zebra"),
        _item(tmp_path, "Apple"),
        _item(tmp_path, "Ångström"),
        _item(tmp_path, "banana"),
    )
    assert _names(library_filter.apply(items, Query(sort=Sort.NAME))) == [
        "Ångström",
        "Apple",
        "banana",
        "zebra",
    ]


def test_names_that_fold_together_are_ordered_by_path(tmp_path: Path) -> None:
    """A total order, so that a rescan cannot reshuffle the grid."""
    second = _item(tmp_path / "b", "cabin")
    first = _item(tmp_path / "a", "Cabin")
    assert library_filter.apply((second, first), Query(sort=Sort.NAME)) == (first, second)


def test_newest_first_reads_the_modification_time(tmp_path: Path) -> None:
    old = _item(tmp_path, "old", mtime=100)
    recent = _item(tmp_path, "recent", mtime=900)
    middle = _item(tmp_path, "middle", mtime=500)
    found = library_filter.apply((old, recent, middle), Query(sort=Sort.NEWEST))
    assert _names(found) == ["recent", "middle", "old"]


def test_wallpapers_added_in_the_same_second_fall_back_to_name_order(tmp_path: Path) -> None:
    items = (
        _item(tmp_path, "beta", mtime=500),
        _item(tmp_path, "alpha", mtime=500),
        _item(tmp_path, "newer", mtime=900),
    )
    assert _names(library_filter.apply(items, Query(sort=Sort.NEWEST))) == [
        "newer",
        "alpha",
        "beta",
    ]


def test_largest_first_reads_the_size(tmp_path: Path) -> None:
    items = (
        _item(tmp_path, "small", size=10),
        _item(tmp_path, "huge", size=9_000_000),
        _item(tmp_path, "medium", size=4_000),
    )
    assert _names(library_filter.apply(items, Query(sort=Sort.LARGEST))) == [
        "huge",
        "medium",
        "small",
    ]


def test_wallpapers_of_the_same_size_fall_back_to_name_order(tmp_path: Path) -> None:
    items = (
        _item(tmp_path, "beta", size=2048),
        _item(tmp_path, "alpha", size=2048),
        _item(tmp_path, "bigger", size=4096),
    )
    assert _names(library_filter.apply(items, Query(sort=Sort.LARGEST))) == [
        "bigger",
        "alpha",
        "beta",
    ]


def test_sorting_survives_a_search(tmp_path: Path) -> None:
    items = (
        _item(tmp_path, "snowy-cabin", size=10),
        _item(tmp_path, "snowy-village", size=900),
        _item(tmp_path, "sunny-beach", size=5000),
    )
    found = library_filter.apply(items, Query(text="snow", sort=Sort.LARGEST))
    assert _names(found) == ["snowy-village", "snowy-cabin"]


def test_an_empty_library_sorts_to_nothing() -> None:
    for sort in library_filter.SORT_CHOICES:
        assert library_filter.apply((), Query(sort=sort)) == ()


# -- what the window says ------------------------------------------------


def test_the_description_names_what_is_being_shown() -> None:
    assert library_filter.describe(Query()) == "wallpapers"
    assert library_filter.describe(Query(kinds=Kinds.VIDEOS)) == "videos"
    assert library_filter.describe(Query(text="snow")) == 'wallpapers matching "snow"'
    assert (
        library_filter.describe(Query(text=" snow ", kinds=Kinds.STILLS))
        == 'stills matching "snow"'
    )


def test_the_description_reads_as_a_sentence_after_no() -> None:
    """The empty state puts `No ` in front of it, and must not read as a lie."""
    assert f"No {library_filter.describe(Query(text='foo'))}" == 'No wallpapers matching "foo"'


def test_every_choice_is_offered_exactly_once() -> None:
    """A member missing from these tuples is a control that cannot reach it."""
    assert tuple(Kinds) == library_filter.KIND_CHOICES
    assert tuple(Sort) == library_filter.SORT_CHOICES


def test_every_choice_has_a_label_of_its_own() -> None:
    labels = [choice.label for choice in library_filter.KIND_CHOICES]
    labels += [choice.label for choice in library_filter.SORT_CHOICES]
    assert all(labels)
    assert len(set(labels)) == len(labels)
