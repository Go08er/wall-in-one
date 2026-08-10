"""Named playlists: a rotation somebody chose rather than a folder listing.

Two things carry most of these. An entry has an identity of its own, so
reordering leaves it the same entry and one wallpaper can appear twice. And a
playlist may name wallpapers that are not here, because a drive that is not
mounted this morning is not somebody deleting their list.

`XDG_STATE_HOME` is redirected; every path is under `tmp_path`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wall_in_one.library import playlists
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.library.playlists import Playlist, PlaylistError, Store


@pytest.fixture(autouse=True)
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(path=tmp_path / "playlists.json")


def item(name: str, kind: Kind = Kind.STILL) -> MediaItem:
    suffix = ".mp4" if kind is Kind.VIDEO else ".png"
    return MediaItem(path=Path(f"/w/{name}{suffix}"), kind=kind, size=1, mtime=0)


def names(items: tuple[MediaItem, ...]) -> list[str]:
    return [one.name for one in items]


# -- making them ----------------------------------------------------------


def test_a_new_playlist_is_empty_and_named(store: Store) -> None:
    made = store.create("  Evening   colours ")
    assert made.name == "Evening colours"
    assert len(made) == 0


@pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
def test_a_playlist_needs_a_name(store: Store, raw: str) -> None:
    with pytest.raises(PlaylistError) as caught:
        store.create(raw)
    assert caught.value.kind == "invalid-name"


def test_an_absurdly_long_name_is_refused(store: Store) -> None:
    with pytest.raises(PlaylistError):
        store.create("x" * (playlists.MAX_NAME_LENGTH + 1))


def test_a_playlist_can_be_found_by_name_however_it_is_typed(store: Store) -> None:
    made = store.create("Evening Colours")
    assert store.find("evening colours").id == made.id
    assert store.find(made.id).id == made.id


def test_asking_for_a_playlist_that_is_not_there_says_which(store: Store) -> None:
    with pytest.raises(PlaylistError) as caught:
        store.find("nope")
    assert caught.value.kind == "no-such-playlist"


def test_renaming_keeps_the_identity_and_the_entries(store: Store) -> None:
    made = store.create("Evening")
    store.add(made.id, Path("/w/a.png"))
    renamed = store.rename(made.id, "Night")
    assert (renamed.id, renamed.name, len(renamed)) == (made.id, "Night", 1)


def test_deleting_reports_whether_there_was_one(store: Store) -> None:
    made = store.create("Evening")
    assert store.delete(made.id) is True
    assert store.delete(made.id) is False


# -- entries --------------------------------------------------------------


def test_entries_keep_the_order_they_were_added_in(store: Store) -> None:
    made = store.create("Evening")
    for name in ("b", "a", "c"):
        store.add(made.id, Path(f"/w/{name}.png"))
    assert [entry.path.stem for entry in store.find(made.id).entries] == ["b", "a", "c"]


def test_one_wallpaper_may_appear_twice(store: Store) -> None:
    """Which is impossible if the path is the key, and is a legitimate thing
    to want: a list can open and close on the same picture."""
    made = store.create("Evening")
    store.add(made.id, Path("/w/a.png"))
    store.add(made.id, Path("/w/a.png"))
    entries = store.find(made.id).entries
    assert len(entries) == 2
    assert entries[0].id != entries[1].id


def test_moving_an_entry_keeps_its_identity(store: Store) -> None:
    """ "Third in the list" stops meaning anything if editing renumbers."""
    made = store.create("Evening")
    for name in ("a", "b", "c"):
        store.add(made.id, Path(f"/w/{name}.png"))
    moving = store.find(made.id).entries[2]

    moved = store.move_entry(made.id, moving.id, 0)

    assert [entry.path.stem for entry in moved.entries] == ["c", "a", "b"]
    assert moved.entries[0].id == moving.id


def test_moving_past_the_end_lands_at_the_end(store: Store) -> None:
    """A drag past the bottom of a list has an obvious reading; refusing it
    would be pedantry about a gesture."""
    made = store.create("Evening")
    for name in ("a", "b"):
        store.add(made.id, Path(f"/w/{name}.png"))
    first = store.find(made.id).entries[0]
    moved = store.move_entry(made.id, first.id, 99)
    assert [entry.path.stem for entry in moved.entries] == ["b", "a"]


def test_removing_an_entry_leaves_the_others_in_order(store: Store) -> None:
    made = store.create("Evening")
    for name in ("a", "b", "c"):
        store.add(made.id, Path(f"/w/{name}.png"))
    middle = store.find(made.id).entries[1]
    left = store.remove_entry(made.id, middle.id)
    assert [entry.path.stem for entry in left.entries] == ["a", "c"]


def test_removing_an_entry_that_is_not_there_says_so(store: Store) -> None:
    made = store.create("Evening")
    with pytest.raises(PlaylistError) as caught:
        store.remove_entry(made.id, "nope")
    assert caught.value.kind == "no-such-entry"


def test_a_deleted_wallpaper_leaves_every_list(store: Store) -> None:
    """Entries outlive a missing file on purpose. Not one we unlinked."""
    first, second = store.create("A"), store.create("B")
    store.add(first.id, Path("/w/gone.png"))
    store.add(second.id, Path("/w/gone.png"))
    store.add(second.id, Path("/w/kept.png"))

    assert store.forget_path(Path("/w/gone.png")) is True

    assert len(store.find(first.id)) == 0
    assert [entry.path.stem for entry in store.find(second.id).entries] == ["kept"]
    assert store.forget_path(Path("/w/gone.png")) is False


# -- resolving against a library ------------------------------------------


def test_a_playlist_resolves_in_its_own_order(store: Store) -> None:
    library = [item("a"), item("b"), item("c")]
    made = store.create("Evening")
    for name in ("c", "a"):
        store.add(made.id, Path(f"/w/{name}.png"))
    assert names(store.find(made.id).resolve(library)) == ["c", "a"]


def test_an_entry_the_library_cannot_account_for_is_skipped_not_dropped(store: Store) -> None:
    library = [item("a")]
    made = store.create("Evening")
    store.add(made.id, Path("/w/a.png"))
    store.add(made.id, Path("/w/unmounted.png"))

    playlist = store.find(made.id)

    assert names(playlist.resolve(library)) == ["a"]
    assert len(playlist) == 2, "the entry is still in the list"
    assert playlist.missing(library) == ("/w/unmounted.png",)


def test_a_wallpaper_listed_twice_is_shown_twice(store: Store) -> None:
    library = [item("a")]
    made = store.create("Evening")
    store.add(made.id, Path("/w/a.png"))
    store.add(made.id, Path("/w/a.png"))
    assert names(store.find(made.id).resolve(library)) == ["a", "a"]


def test_a_singleton_playlist_is_visible_and_replaced_in_place(store: Store) -> None:
    first = store.set_singleton("quick-choice", "Quick choice", Path("/w/a.png"), entry_id="a")
    second = store.set_singleton("quick-choice", "Quick choice", Path("/w/b.png"), entry_id="b")

    assert first.id == second.id == "quick-choice"
    assert [entry.source for entry in second.entries] == ["/w/b.png"]
    assert [playlist.id for playlist in store.all()] == ["quick-choice"]


# -- what the rotation does with it ---------------------------------------


def test_no_active_playlist_means_the_whole_library(store: Store) -> None:
    assert playlists.rotation(store, "", [item("a")]) is None


def test_an_active_playlist_narrows_the_rotation(store: Store) -> None:
    library = [item("a"), item("b")]
    made = store.create("Evening")
    store.add(made.id, Path("/w/b.png"))
    chosen = playlists.rotation(store, made.id, library)
    assert chosen is not None and names(chosen) == ["b"]


def test_a_playlist_can_be_made_active_by_name(store: Store) -> None:
    """Which is what somebody types, and what a settings file holds."""
    library = [item("a")]
    store.add(store.create("Evening").id, Path("/w/a.png"))
    assert playlists.rotation(store, "Evening", library) is not None


def test_an_active_playlist_that_was_deleted_falls_back(store: Store) -> None:
    assert playlists.rotation(store, "deleted-id", [item("a")]) is None


def test_a_playlist_whose_wallpapers_are_all_absent_falls_back(store: Store) -> None:
    """A manager that stops changing the wallpaper is a worse answer than one
    that keeps working -- the same rule the favourites follow."""
    made = store.create("Evening")
    store.add(made.id, Path("/w/unmounted.png"))
    assert playlists.rotation(store, made.id, [item("a")]) is None


def test_an_empty_playlist_falls_back(store: Store) -> None:
    made = store.create("Evening")
    assert playlists.rotation(store, made.id, [item("a")]) is None


# -- the file -------------------------------------------------------------


def test_a_playlist_outlives_the_process(tmp_path: Path) -> None:
    target = tmp_path / "playlists.json"
    made = Store(path=target).create("Evening")
    Store.open(target).add(made.id, Path("/w/a.png"))

    reopened = Store.open(target)

    assert reopened.find("Evening").entries[0].source == "/w/a.png"


def test_entry_identities_survive_the_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "playlists.json"
    store = Store(path=target)
    made = store.create("Evening")
    store.add(made.id, Path("/w/a.png"))
    original = store.find(made.id).entries[0].id

    assert Store.open(target).find(made.id).entries[0].id == original


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"playlists": "not a list"}', "null", '{"nope": 1}'],
    ids=["empty", "garbage", "array", "wrong-type", "null", "no-key"],
)
def test_an_unreadable_file_is_no_playlists_rather_than_a_crash(
    tmp_path: Path, content: str
) -> None:
    target = tmp_path / "playlists.json"
    target.write_text(content, encoding="utf-8")
    assert playlists.load(target) == {}


def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"playlists": [{"id": "x", "name": "E"}]}), encoding="utf-8")
    link = tmp_path / "playlists.json"
    link.symlink_to(real)
    assert playlists.load(link) == {}


def test_one_bad_playlist_costs_only_itself(tmp_path: Path) -> None:
    target = tmp_path / "playlists.json"
    target.write_text(
        json.dumps(
            {
                "playlists": [
                    {"id": "one", "name": "Good", "entries": [{"id": "e", "source": "/w/a.png"}]},
                    {"name": "no id"},
                    "not even an object",
                    {"id": "two", "name": "Also good", "entries": "not a list"},
                ]
            }
        ),
        encoding="utf-8",
    )
    found = playlists.load(target)
    assert set(found) == {"one", "two"}
    assert len(found["two"]) == 0


def test_a_relative_entry_is_dropped_and_the_list_survives(tmp_path: Path) -> None:
    target = tmp_path / "playlists.json"
    target.write_text(
        json.dumps(
            {
                "playlists": [
                    {
                        "id": "one",
                        "name": "Evening",
                        "entries": [
                            {"id": "a", "source": "relative/a.png"},
                            {"id": "b", "source": "/w/b.png"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert [entry.id for entry in playlists.load(target)["one"].entries] == ["b"]


def test_the_write_is_a_single_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "playlists.json"
    store = Store(path=target)
    made = store.create("First")
    observed: list[str] = []
    real_replace = os.replace

    def watch(source: object, destination: object) -> None:
        observed.append(target.read_text(encoding="utf-8"))
        assert Path(str(source)).parent == target.parent
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", watch)
    store.rename(made.id, "Second")
    assert "First" in observed[0]


def test_a_failed_write_leaves_no_debris(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(_source: object, _destination: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(PlaylistError) as caught:
        playlists.save({"one": Playlist(id="one", name="Evening")}, tmp_path / "playlists.json")
    assert caught.value.kind == "local-io"
    assert list(tmp_path.iterdir()) == []


def test_a_broken_file_is_moved_aside_rather_than_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "playlists.json"
    target.write_text("not json but somebody's lists", encoding="utf-8")
    store = Store.open(target)
    assert store.fault is not None
    store.create("Evening")
    kept = target.with_name(target.name + playlists.BROKEN_SUFFIX)
    assert kept.read_text(encoding="utf-8") == "not json but somebody's lists"


def test_playlists_are_listed_by_name(store: Store) -> None:
    """So a listing is stable between runs rather than following an opaque id."""
    for name in ("zebra", "apple", "Mango"):
        store.create(name)
    assert [one.name for one in store.all()] == ["apple", "Mango", "zebra"]


@pytest.mark.parametrize(
    ("moving", "anchor", "after", "position", "expected"),
    [
        ("d", "b", False, 1, ("a", "d", "b", "c")),
        ("a", "d", False, 2, ("b", "c", "a", "d")),
        ("a", "b", True, 1, ("b", "a", "c", "d")),
        ("b", "c", True, 2, ("a", "c", "b", "d")),
        ("c", "d", True, 3, ("a", "b", "d", "c")),
        ("c", "c", False, 2, ("a", "b", "c", "d")),
        ("c", "c", True, 2, ("a", "b", "c", "d")),
        ("b", None, False, 3, ("a", "c", "d", "b")),
        ("b", "missing", True, 3, ("a", "c", "d", "b")),
    ],
)
def test_drop_position_knows_both_sides_and_preserves_self_drops(
    moving: str,
    anchor: str | None,
    after: bool,
    position: int,
    expected: tuple[str, ...],
) -> None:
    ids = ("a", "b", "c", "d")
    remaining = [entry_id for entry_id in ids if entry_id != moving]

    actual = playlists.drop_position(ids, moving, anchor, after=after)
    remaining.insert(actual, moving)

    assert actual == position
    assert tuple(remaining) == expected
