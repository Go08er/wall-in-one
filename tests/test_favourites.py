"""Favourites: a list of paths that must survive everything.

Two decisions carry most of these tests. A favourite whose file is not in the
library right now is *kept*, because the ordinary reasons for that -- an
unmounted drive, a root temporarily removed, a scan that hit its ceiling -- are
exactly the moments when the app is least able to tell that anything is wrong,
and pruning would silently discard a list the user built by hand. And a file
that cannot be read never stops the app: it degrades to no favourites, but the
unreadable bytes are moved aside rather than overwritten.

Every path here is under `tmp_path`; `XDG_STATE_HOME` is redirected so nothing
can reach the real list.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from wall_in_one import file_io
from wall_in_one.library import favourites, state_file
from wall_in_one.library.favourites import Favourites, FavouritesError, Store

ONE = Path("/w/one.png")
TWO = Path("/w/two.png")
THREE = Path("/w/three.mp4")


def _add_favourite_from_process(target: str, path: str) -> None:
    """Spawn target used to prove Store mutations rebase across processes."""
    Store.open(Path(target)).add(Path(path))


@pytest.fixture(autouse=True)
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    return home


# -- the value ------------------------------------------------------------


def test_favourites_start_empty() -> None:
    assert len(Favourites()) == 0
    assert list(Favourites()) == []


def test_adding_keeps_the_order_they_were_marked_in() -> None:
    """Insertion order is the only order the user has any memory of."""
    marked = Favourites().with_added(TWO).with_added(ONE).with_added(THREE)
    assert list(marked) == [TWO, ONE, THREE]


def test_adding_the_same_one_twice_changes_nothing() -> None:
    once = Favourites().with_added(ONE)
    assert once.with_added(ONE) is once


def test_removing_one_that_was_never_there_changes_nothing() -> None:
    marked = Favourites().with_added(ONE)
    assert marked.without(TWO) is marked


def test_removing_leaves_the_rest_in_order() -> None:
    marked = Favourites().with_added(ONE).with_added(TWO).with_added(THREE)
    assert list(marked.without(TWO)) == [ONE, THREE]


def test_missing_reports_favourites_the_library_cannot_account_for() -> None:
    """The honest form of keeping them: say so rather than look like a shorter list."""
    marked = Favourites().with_added(ONE).with_added(TWO)
    assert marked.missing([ONE]) == (TWO,)
    assert marked.missing([ONE, TWO]) == ()


# -- the file -------------------------------------------------------------


def test_a_saved_list_reads_back_the_same(tmp_path: Path) -> None:
    marked = Favourites().with_added(ONE).with_added(TWO)
    target = tmp_path / "favourites.json"
    favourites.save(marked, target)
    assert list(favourites.load(target)) == [ONE, TWO]


def test_the_default_location_is_under_the_state_home(state_home: Path) -> None:
    favourites.save(Favourites().with_added(ONE))
    assert favourites.state_path().is_relative_to(state_home)
    assert list(favourites.load()) == [ONE]


def test_nothing_saved_yet_is_no_favourites_rather_than_an_error() -> None:
    assert len(favourites.load()) == 0


def test_the_write_is_a_single_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-written list reads back short, which looks like the app dropping
    favourites rather than like a file that needs attention."""
    target = tmp_path / "favourites.json"
    favourites.save(Favourites().with_added(ONE), target)
    observed: list[str] = []
    real_replace = os.replace

    def watch(source: object, destination: object) -> None:
        observed.append(target.read_text(encoding="utf-8"))
        assert Path(str(source)).parent == target.parent
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", watch)
    favourites.save(Favourites().with_added(TWO), target)
    # The old list was still intact at the moment the new one was swapped in.
    assert str(ONE) in observed[0]


def test_a_failed_write_leaves_only_inert_private_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "favourites.json"

    def explode(_source: object, _destination: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(FavouritesError) as caught:
        favourites.save(Favourites().with_added(ONE), target)
    assert caught.value.kind == "local-io"
    retained = tmp_path / file_io.RETAINED_ENTRY_DIRECTORY
    assert set(tmp_path.iterdir()) == {retained}
    residues = tuple(retained.iterdir())
    assert len(residues) == 2
    assert any(path.is_file() and path.stat().st_size == 0 for path in residues)
    assert any(path.is_dir() and tuple(path.iterdir()) == () for path in residues)


# -- what a broken file does ---------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["", "not json at all", "[]", '{"paths": "not a list"}', "null", '{"nope": 1}'],
    ids=["empty", "garbage", "array", "wrong-type", "null", "no-paths"],
)
def test_an_unreadable_file_is_no_favourites_rather_than_a_crash(
    tmp_path: Path, content: str
) -> None:
    """A wallpaper manager that will not start over its own bookmark list is
    worse than one that starts empty."""
    target = tmp_path / "favourites.json"
    target.write_text(content, encoding="utf-8")
    assert len(favourites.load(target)) == 0


def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"paths": [str(ONE)]}), encoding="utf-8")
    link = tmp_path / "favourites.json"
    link.symlink_to(real)
    assert len(favourites.load(link)) == 0


def test_entries_that_are_not_absolute_paths_are_dropped(tmp_path: Path) -> None:
    """A relative path here has no directory to be relative to: the process
    that reads it may be running from anywhere."""
    target = tmp_path / "favourites.json"
    target.write_text(
        json.dumps({"paths": [str(ONE), "relative/one.png", "", 7, str(TWO)]}), encoding="utf-8"
    )
    assert list(favourites.load(target)) == [ONE, TWO]
    assert Store.open(target).fault is not None


def test_a_duplicate_in_the_file_is_read_once(tmp_path: Path) -> None:
    target = tmp_path / "favourites.json"
    target.write_text(json.dumps({"paths": [str(ONE), str(ONE)]}), encoding="utf-8")
    assert list(favourites.load(target)) == [ONE]


def test_an_absurdly_long_list_is_capped(tmp_path: Path) -> None:
    target = tmp_path / "favourites.json"
    many = [f"/w/{index}.png" for index in range(favourites.MAX_FAVOURITES + 50)]
    target.write_text(json.dumps({"paths": many}), encoding="utf-8")
    assert len(favourites.load(target)) == favourites.MAX_FAVOURITES


def test_an_unrecognised_version_is_recovered_but_faulted(tmp_path: Path) -> None:
    target = tmp_path / "favourites.json"
    target.write_text(
        json.dumps({"version": 99, "paths": [str(ONE)]}),
        encoding="utf-8",
    )
    opened = Store.open(target)
    assert list(opened.favourites) == [ONE]
    assert opened.fault is not None
    assert "unsupported version" in opened.fault


# -- the store ------------------------------------------------------------


def test_the_store_writes_through_on_every_change(tmp_path: Path) -> None:
    """Saving on exit loses the lot when the session ends any other way."""
    target = tmp_path / "favourites.json"
    store = Store(path=target)
    store.add(ONE)
    assert list(favourites.load(target)) == [ONE]
    store.discard(ONE)
    assert list(favourites.load(target)) == []


def test_toggling_answers_with_what_it_is_now(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "favourites.json")
    assert store.toggle(ONE) is True
    assert store.is_favourite(ONE)
    assert store.toggle(ONE) is False
    assert not store.is_favourite(ONE)


def test_adding_twice_reports_the_second_as_a_no_op(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "favourites.json")
    assert store.add(ONE) is True
    assert store.add(ONE) is False


def test_delete_cleanup_preserves_an_unrelated_cross_process_favourite(
    tmp_path: Path,
) -> None:
    target = tmp_path / "favourites.json"
    seed = Store(path=target)
    seed.add(ONE)
    stale_cleanup = Store.open(target)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_add_favourite_from_process,
        args=(str(target), str(TWO)),
    )
    process.start()
    try:
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()

    assert stale_cleanup.discard(ONE)
    assert Store.open(target).paths == frozenset({TWO})


def test_a_store_write_failure_does_not_change_the_in_memory_favourites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "favourites.json"
    store = Store(path=target)
    store.add(ONE)

    def fail(_favourites: object, _path: object) -> None:
        raise FavouritesError("local-io", "injected write failure")

    monkeypatch.setattr(favourites, "save", fail)
    with pytest.raises(FavouritesError):
        store.add(TWO)

    assert store.paths == frozenset({ONE})
    assert Store.open(target).paths == frozenset({ONE})


def test_the_store_opens_from_a_file(tmp_path: Path) -> None:
    target = tmp_path / "favourites.json"
    favourites.save(Favourites().with_added(ONE), target)
    assert Store.open(target).is_favourite(ONE)


def test_opening_a_broken_file_reports_the_fault_but_still_works(tmp_path: Path) -> None:
    target = tmp_path / "favourites.json"
    target.write_text("not json", encoding="utf-8")
    store = Store.open(target)
    assert store.fault is not None
    assert len(store) == 0
    assert store.add(ONE) is True


def test_a_broken_file_is_moved_aside_rather_than_overwritten(tmp_path: Path) -> None:
    """Whatever was in there is still the user's list, in some form."""
    target = tmp_path / "favourites.json"
    target.write_text("not json but precious", encoding="utf-8")
    store = Store.open(target)
    store.add(ONE)
    kept = target.with_name(target.name + favourites.BROKEN_SUFFIX)
    assert kept.read_text(encoding="utf-8") == "not json but precious"
    assert list(favourites.load(target)) == [ONE]


def test_a_valid_manual_repair_after_the_fault_read_remains_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "favourites.json"
    target.write_text("not json", encoding="utf-8")
    store = Store.open(target)
    manual = Favourites().with_added(TWO)
    preserve = state_file.preserve_faulted

    def repair_then_preserve(
        path: Path,
        *,
        observed: state_file.StateFileObservation,
    ) -> Path:
        favourites.save(manual, path)
        return preserve(path, observed=observed)

    monkeypatch.setattr(state_file, "preserve_faulted", repair_then_preserve)

    with pytest.raises(FavouritesError) as caught:
        store.add(ONE)

    assert caught.value.kind == "local-io"
    assert Store.open(target).favourites == manual
    assert not target.with_name(target.name + favourites.BROKEN_SUFFIX).exists()


def test_a_valid_manual_repair_before_recovery_publication_remains_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "favourites.json"
    original = "not json"
    target.write_text(original, encoding="utf-8")
    store = Store.open(target)
    manual = Favourites().with_added(TWO)
    save = favourites.save

    def repair_then_save(
        updated: Favourites,
        path: Path | None = None,
        *,
        replace_existing: bool = True,
    ) -> Path:
        assert path == target
        assert not replace_existing
        save(manual, target)
        return save(updated, target, replace_existing=replace_existing)

    monkeypatch.setattr(favourites, "save", repair_then_save)

    with pytest.raises(FavouritesError) as caught:
        store.add(ONE)

    assert caught.value.kind == "local-io"
    assert store.fault is not None
    assert Store.open(target).favourites == manual
    assert target.with_name(target.name + favourites.BROKEN_SUFFIX).read_text() == original


def test_a_failed_broken_file_relocation_keeps_the_fault_and_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "favourites.json"
    original = "not json but precious"
    target.write_text(original, encoding="utf-8")
    store = Store.open(target)

    def fail(_path: Path, *, observed: state_file.StateFileObservation) -> Path:
        assert observed.present
        raise OSError("injected relocation failure")

    monkeypatch.setattr(state_file, "preserve_faulted", fail)
    with pytest.raises(FavouritesError):
        store.add(ONE)

    assert store.fault is not None
    assert target.read_text(encoding="utf-8") == original
    assert len(store) == 0


def test_the_file_is_only_moved_aside_once(tmp_path: Path) -> None:
    """A second change must not overwrite the rescued copy with good data."""
    target = tmp_path / "favourites.json"
    target.write_text("precious", encoding="utf-8")
    store = Store.open(target)
    store.add(ONE)
    store.add(TWO)
    kept = target.with_name(target.name + favourites.BROKEN_SUFFIX)
    assert kept.read_text(encoding="utf-8") == "precious"


def test_the_store_answers_membership_without_touching_the_disk(tmp_path: Path) -> None:
    """The grid asks once per tile, so this cannot be a file read."""
    target = tmp_path / "favourites.json"
    store = Store(path=target)
    store.add(ONE)
    target.unlink()
    assert store.is_favourite(ONE)


def test_a_favourite_no_longer_in_the_library_is_kept(tmp_path: Path) -> None:
    """The decision worth defending: an unmounted drive must not silently
    empty a list the user built by hand."""
    store = Store(path=tmp_path / "favourites.json")
    store.add(ONE)
    store.add(TWO)
    assert store.missing([ONE]) == (TWO,)
    assert len(store) == 2
