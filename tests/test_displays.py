"""Which playlist each screen shows.

Every test writes to `tmp_path`; nothing here can see the real state file.
"""

from __future__ import annotations

import json
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from wall_in_one.library import displays, state_file
from wall_in_one.library.displays import DisplayError, Store


def _unassign_display_from_stale_process(
    target: str,
    connector: str,
    ready: Connection,
    proceed: Connection,
    outcome: Connection,
) -> None:
    """Open before another writer, then remove from that stale Store."""
    store = Store.open(Path(target))
    ready.send(True)
    proceed.recv()
    outcome.send(store.unassign(connector))


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(path=tmp_path / "displays.json")


def test_a_screen_with_no_entry_follows_the_default(store: Store) -> None:
    """Empty is the ordinary answer, not a failure."""
    assert store.playlist_for("eDP-1") == ""


def test_an_assignment_survives_a_reopen(store: Store, tmp_path: Path) -> None:
    store.assign("eDP-1", "Quiet")

    assert Store.open(tmp_path / "displays.json").playlist_for("eDP-1") == "Quiet"


def test_reassigning_replaces_rather_than_adds(store: Store) -> None:
    store.assign("eDP-1", "Quiet")
    store.assign("eDP-1", "Cityscapes")

    assert store.playlist_for("eDP-1") == "Cityscapes"
    assert len(store) == 1


def test_two_screens_can_differ(store: Store) -> None:
    """The whole point: cityscapes on the big one, something quiet on the laptop."""
    store.assign("DP-2", "Cityscapes")
    store.assign("eDP-1", "Quiet")

    assert store.playlist_for("DP-2") == "Cityscapes"
    assert store.playlist_for("eDP-1") == "Quiet"


def test_listing_is_stable_between_runs(store: Store) -> None:
    store.assign("eDP-1", "Quiet")
    store.assign("DP-2", "Cityscapes")

    assert store.all() == (("DP-2", "Cityscapes"), ("eDP-1", "Quiet"))


def test_unassigning_returns_a_screen_to_the_default(store: Store) -> None:
    store.assign("eDP-1", "Quiet")

    assert store.unassign("eDP-1")
    assert store.playlist_for("eDP-1") == ""


def test_unassigning_something_unassigned_says_so(store: Store) -> None:
    assert not store.unassign("eDP-1")


def test_a_deleted_playlist_takes_its_assignments_with_it(store: Store) -> None:
    """A screen pointing at a name that resolves to nothing reads as broken."""
    store.assign("eDP-1", "Quiet")
    store.assign("DP-2", "Quiet")
    store.assign("DP-3", "Cityscapes")

    assert store.forget_playlist("Quiet") == 2
    assert store.all() == (("DP-3", "Cityscapes"),)


def test_stale_same_display_delete_preserves_an_unrelated_cross_process_write(
    tmp_path: Path,
) -> None:
    """The delete applies to latest disk and cannot replace the other screen."""
    target = tmp_path / "displays.json"
    seeded = Store(path=target)
    seeded.assign("DP-2", "Original")
    parent = Store.open(target)
    context = multiprocessing.get_context("spawn")
    ready_parent, ready_child = context.Pipe()
    proceed_parent, proceed_child = context.Pipe()
    outcome_parent, outcome_child = context.Pipe()
    process = context.Process(
        target=_unassign_display_from_stale_process,
        args=(str(target), "DP-2", ready_child, proceed_child, outcome_child),
    )
    process.start()
    try:
        assert ready_parent.poll(5), "child did not open its stale display Store"
        assert ready_parent.recv() is True
        parent.assign("DP-2", "Parent edit")
        parent.assign("eDP-1", "Quiet")
        proceed_parent.send(True)
        assert outcome_parent.poll(5), "child did not finish its display mutation"
        assert outcome_parent.recv() is True
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert Store.open(target).all() == (("eDP-1", "Quiet"),)


def test_an_unplugged_screen_keeps_its_assignment(store: Store) -> None:
    """Unplugging a dock at the end of the day must not forget the arrangement."""
    store.assign("DP-2", "Cityscapes")

    lines = store.describe(attached=("eDP-1",))

    assert len(lines) == 1
    assert "DP-2" in lines[0] and "not attached" in lines[0]


def test_an_attached_screen_is_not_marked(store: Store) -> None:
    store.assign("eDP-1", "Quiet")
    assert "not attached" not in store.describe(attached=("eDP-1",))[0]


def test_playlist_names_are_tidied_but_connectors_remain_tokens(store: Store) -> None:
    store.assign("eDP-1", "  Quiet  Nights ")

    assert store.all() == (("eDP-1", "Quiet Nights"),)
    assert store.playlist_for("eDP-1") == "Quiet Nights"


@pytest.mark.parametrize("bad", ["", "   ", "\n", " DP-1", "DP 1", "DP-1\t"])
def test_an_empty_connector_is_refused(store: Store, bad: str) -> None:
    with pytest.raises(DisplayError, match="connector"):
        store.assign(bad, "Quiet")


@pytest.mark.parametrize("bad", ["", "   "])
def test_an_empty_playlist_is_refused(store: Store, bad: str) -> None:
    with pytest.raises(DisplayError, match="playlist"):
        store.assign("eDP-1", bad)


def test_the_number_of_screens_is_bounded(store: Store) -> None:
    for n in range(64):
        store.assign(f"DP-{n}", "Quiet")
    with pytest.raises(DisplayError, match="no more than"):
        store.assign("DP-999", "Quiet")


def test_reassigning_an_existing_screen_at_the_ceiling_still_works(store: Store) -> None:
    for n in range(64):
        store.assign(f"DP-{n}", "Quiet")
    store.assign("DP-0", "Cityscapes")
    assert store.playlist_for("DP-0") == "Cityscapes"


# -- a file somebody has been editing --------------------------------------


def test_a_missing_file_is_an_empty_store(tmp_path: Path) -> None:
    opened = Store.open(tmp_path / "nothing.json")
    assert len(opened) == 0
    assert opened.fault is None


def test_a_direct_absent_file_seed_is_retained_on_its_first_mutation(tmp_path: Path) -> None:
    target = tmp_path / "displays.json"
    store = Store({"DP-2": "Seed"}, target)

    assert store.unassign("missing") is False
    store.assign("eDP-1", "Added")

    assert Store.open(target).all() == (("DP-2", "Seed"), ("eDP-1", "Added"))


def test_unreadable_json_is_reported_rather_than_thrown(tmp_path: Path) -> None:
    target = tmp_path / "displays.json"
    target.write_text("{not json", encoding="utf-8")

    opened = Store.open(target)

    assert len(opened) == 0
    assert opened.fault is not None


def test_a_broken_file_is_set_aside_on_the_next_write(tmp_path: Path) -> None:
    """Overwriting it silently would destroy whatever somebody was editing."""
    target = tmp_path / "displays.json"
    target.write_text("{not json", encoding="utf-8")
    opened = Store.open(target)

    opened.assign("eDP-1", "Quiet")

    assert (tmp_path / "displays.json.broken").read_text(encoding="utf-8") == "{not json"
    assert Store.open(target).playlist_for("eDP-1") == "Quiet"


def test_a_fault_which_appears_after_open_is_still_preserved(tmp_path: Path) -> None:
    target = tmp_path / "displays.json"
    store = Store.open(target)
    original = "a concurrent broken display file"
    target.write_text(original, encoding="utf-8")

    store.assign("eDP-1", "Quiet")

    assert (
        target.with_name(target.name + displays.BROKEN_SUFFIX).read_text(encoding="utf-8")
        == original
    )
    assert Store.open(target).playlist_for("eDP-1") == "Quiet"


def test_a_symlinked_mutation_lock_is_reported_without_touching_its_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "displays.json"
    sentinel = tmp_path / "outside"
    sentinel.write_text("do not touch", encoding="utf-8")
    target.with_name(f".{target.name}.mutation.lock").symlink_to(sentinel)
    store = Store(path=target)

    with pytest.raises(DisplayError) as caught:
        store.assign("eDP-1", "Quiet")

    assert caught.value.kind == "local-io"
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert not target.exists()
    assert len(store) == 0


def test_a_store_write_failure_does_not_change_the_in_memory_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "displays.json"
    store = Store(path=target)
    store.assign("eDP-1", "Quiet")

    def fail(_assignments: object, _path: object) -> None:
        raise DisplayError("local-io", "injected write failure")

    monkeypatch.setattr(displays, "save", fail)
    with pytest.raises(DisplayError):
        store.assign("eDP-1", "Loud")

    assert store.playlist_for("eDP-1") == "Quiet"
    assert Store.open(target).playlist_for("eDP-1") == "Quiet"


def test_a_failed_broken_file_relocation_keeps_the_fault_and_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "displays.json"
    original = "{not json"
    store = Store.open(target)
    target.write_text(original, encoding="utf-8")

    def fail(_path: Path) -> Path:
        raise OSError("injected relocation failure")

    monkeypatch.setattr(state_file, "preserve_faulted", fail)
    with pytest.raises(DisplayError):
        store.assign("eDP-1", "Quiet")

    assert store.fault is not None
    assert target.read_text(encoding="utf-8") == original
    assert len(store) == 0


def test_entries_that_are_not_strings_are_dropped(tmp_path: Path) -> None:
    target = tmp_path / "displays.json"
    target.write_text(
        json.dumps({"version": 1, "displays": {"eDP-1": 7, "DP-2": "Quiet", "": "x"}}),
        encoding="utf-8",
    )

    opened = Store.open(target)
    assert opened.all() == (("DP-2", "Quiet"),)
    assert opened.fault is not None


def test_surrogate_text_is_dropped_as_malformed_instead_of_crashing(tmp_path: Path) -> None:
    target = tmp_path / "displays.json"
    target.write_text(
        '{"version": 1, "displays": {"DP-2": "\\ud800", "\\ud801": "Quiet"}}',
        encoding="utf-8",
    )

    opened = Store.open(target)

    assert opened.all() == ()
    assert opened.fault is not None


def test_a_symlink_where_the_state_should_be_is_not_read(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"displays": {"eDP-1": "Secret"}}), encoding="utf-8")
    target = tmp_path / "displays.json"
    target.symlink_to(elsewhere)

    opened = Store.open(target)
    assert len(opened) == 0
    assert opened.fault is not None
