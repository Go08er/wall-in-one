"""Every authoring Store is safe against files written by a newer build.

Two guarantees, checked the same way for each of the six JSON stores:

* A file whose ``version`` is newer than this build understands is read-only:
  it is still readable for display, every mutation is refused with kind
  ``newer-version``, the file stays byte-identical and no ``.broken`` copy is
  made. Before the guard an older build moved it aside and rewrote it narrowed
  to its own version.
* A key this build does not model, at the top level or on a record, survives
  every save of a file this build *does* understand. Before, it vanished on
  the next unrelated edit with no fault and no backup.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from wall_in_one.library import (
    displays,
    favourites,
    pairings,
    playlists,
    removals,
    schedules,
    state_file,
)
from wall_in_one.library.model import Kind, MediaItem

A = Path("/w/a.png")
B = Path("/w/b.png")


def _item(path: Path) -> MediaItem:
    return MediaItem(path, Kind.STILL, 7, 1)


def _intent(path: Path) -> removals.Intent:
    return removals.Intent(path=path, kind=Kind.STILL, roots=(Path("/w"),), token="t" * 32)


Mutation = Callable[[Any], object]


@dataclass(frozen=True)
class Case:
    name: str
    filename: str
    open: Callable[[Path], Any]
    error: type[Exception]
    #: A newer-version document, in the shape this build knows.
    newer: dict[str, Any]
    #: True when the opened Store still shows the newer document's content.
    readable: Callable[[Any], bool]
    #: A current-version document with no unknown keys.
    current: dict[str, Any]
    mutations: tuple[tuple[str, Mutation], ...]


CASES = (
    Case(
        name="playlists",
        filename="playlists.json",
        open=playlists.Store.open,
        error=playlists.PlaylistError,
        newer={
            "version": 2,
            "playlists": [
                {
                    "id": "x",
                    "name": "X",
                    "interval": 30,
                    "entries": [{"id": "e", "source": str(A)}],
                }
            ],
        },
        readable=lambda store: store.find("X").entries[0].source == str(A),
        current={
            "version": 1,
            "playlists": [{"id": "x", "name": "X", "entries": [{"id": "e", "source": str(A)}]}],
        },
        mutations=(
            ("create", lambda store: store.create("Y")),
            ("rename", lambda store: store.rename("x", "Z")),
            ("rename-to-same", lambda store: store.rename("x", "X")),
            ("delete", lambda store: store.delete("x")),
            ("add", lambda store: store.add("x", B)),
            ("singleton", lambda store: store.set_singleton("quick-choice", "Quick choice", B)),
            ("display-singleton", lambda store: store.set_display_singleton("DP-1", B)),
            ("remove-entry", lambda store: store.remove_entry("x", "e")),
            ("move-entry", lambda store: store.move_entry("x", "e", 0)),
            ("move-entry-relative", lambda store: store.move_entry_relative("x", "e", 1)),
            ("forget-path", lambda store: store.forget_path(A)),
        ),
    ),
    Case(
        name="schedules",
        filename="schedules.json",
        open=schedules.Store.open,
        error=schedules.ScheduleError,
        newer={"version": 3, "rules": [{"id": "a", "playlist": "X", "name": "Evening"}]},
        readable=lambda store: [rule.id for rule in store.rules] == ["a"],
        current={"version": 2, "rules": [{"id": "a", "playlist": "X"}]},
        mutations=(
            ("add", lambda store: store.add("Y", rule_id="b")),
            ("remove", lambda store: store.remove("a")),
            ("set-enabled", lambda store: store.set_enabled("a", False)),
            ("update", lambda store: store.update("a", "Y")),
            ("move", lambda store: store.move("a", 0)),
            ("move-relative", lambda store: store.move_relative("a", 1)),
            ("forget-playlist", lambda store: store.forget_playlist("X")),
        ),
    ),
    Case(
        name="pairings",
        filename="pairings.json",
        open=pairings.Store.open,
        error=pairings.PairingError,
        newer={
            "version": 3,
            "pairings": [{"identity": f"still:{A}", "palette": "builtin:Ocean", "motion": "x"}],
        },
        readable=lambda store: store.get(pairings.Identity.of(_item(A))).palette.name == "Ocean",
        current={
            "version": 2,
            "pairings": [{"identity": f"still:{A}", "palette": "builtin:Ocean"}],
        },
        mutations=(
            ("choose-still", lambda store: store.choose_still(_item(A), B)),
            (
                "choose-palette",
                lambda store: store.choose_palette(_item(A), pairings.PalettePolicy("keep")),
            ),
            ("mark-borked", lambda store: store.mark_borked(_item(A), "bad", "runtime")),
            (
                "mark-borked-many",
                lambda store: store.mark_borked_many(((_item(B), "bad", "runtime"),)),
            ),
            (
                "mark-borked-many-if-unchanged",
                lambda store: store.mark_borked_many_if_unchanged(
                    ((_item(B), "bad", "runtime"),), store.records
                ),
            ),
            ("clear-borked", lambda store: store.clear_borked(_item(A))),
            ("reset", lambda store: store.reset(_item(A))),
            (
                "forget-identity",
                lambda store: store.forget_identity(pairings.Identity.of(_item(A))),
            ),
            ("forget-item", lambda store: store.forget_item(_item(A))),
            ("forget-path", lambda store: store.forget_path(A)),
        ),
    ),
    Case(
        name="displays",
        filename="displays.json",
        open=displays.Store.open,
        error=displays.DisplayError,
        newer={"version": 2, "displays": {"DP-1": "X"}, "precedence": "display"},
        readable=lambda store: store.playlist_for("DP-1") == "X",
        current={"version": 1, "displays": {"DP-1": "X"}},
        mutations=(
            ("assign", lambda store: store.assign("DP-2", "Y")),
            ("reassign-same", lambda store: store.assign("DP-1", "X")),
            ("unassign", lambda store: store.unassign("DP-1")),
            ("forget-playlist", lambda store: store.forget_playlist("X")),
        ),
    ),
    Case(
        name="favourites",
        filename="favourites.json",
        open=favourites.Store.open,
        error=favourites.FavouritesError,
        newer={"version": 2, "paths": [str(A)], "starred_at": {str(A): 1}},
        readable=lambda store: store.is_favourite(A),
        current={"version": 1, "paths": [str(A)]},
        mutations=(
            ("add", lambda store: store.add(B)),
            ("add-existing", lambda store: store.add(A)),
            ("discard", lambda store: store.discard(A)),
            ("toggle", lambda store: store.toggle(B)),
        ),
    ),
    Case(
        name="pending removals",
        filename="pending-removals.json",
        open=removals.Store.open,
        error=removals.RemovalJournalError,
        newer={"version": 2, "removals": []},
        # A journal drives deletion, so a newer one is never acted on at all.
        readable=lambda store: store.records == (),
        current={"version": 1, "removals": []},
        mutations=(
            ("record-external", lambda store: store.record_external(_item(A), (Path("/w"),))),
            ("mark-committed", lambda store: store.mark_committed(_intent(A))),
            ("discard", lambda store: store.discard(_intent(A))),
        ),
    ),
)

MUTATIONS = [
    pytest.param(case, label, mutate, id=f"{case.name}-{label}")
    for case in CASES
    for label, mutate in case.mutations
]


def _write(target: Path, document: dict[str, Any]) -> bytes:
    encoded = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)
    return encoded


def _siblings(target: Path) -> list[str]:
    return sorted(path.name for path in target.parent.iterdir() if path.name != target.name)


def _assert_refused(case: Case, target: Path, mutate: Mutation, store: Any) -> None:
    with pytest.raises(case.error) as caught:
        mutate(store)
    error: Any = caught.value
    assert error.kind == state_file.NEWER_VERSION == "newer-version"
    assert str(error) == (
        f"newer-version: {case.filename} was saved by a newer version of Wall-in-One; "
        "open that version to change it. Nothing was changed."
    )
    assert store.fault_kind == state_file.NEWER_VERSION
    assert store.fault is not None
    assert "newer version" in store.fault


# -- newer versions are read-only ---------------------------------------------


@pytest.mark.parametrize(("case", "label", "mutate"), MUTATIONS)
def test_a_newer_file_refuses_every_mutation_and_stays_byte_identical(
    tmp_path: Path, case: Case, label: str, mutate: Mutation
) -> None:
    target = tmp_path / "state" / case.filename
    original = _write(target, case.newer)
    before = _siblings(target)

    store = case.open(target)
    assert store.fault_kind == state_file.NEWER_VERSION
    assert case.readable(store)

    _assert_refused(case, target, mutate, store)

    assert target.read_bytes() == original
    # No ``.broken`` copy, and no temporary left behind; only lock files.
    assert [name for name in _siblings(target) if name not in before] == [
        name for name in _siblings(target) if name.endswith(".lock")
    ]
    assert not list(target.parent.glob(f"{case.filename}.broken*"))
    reopened = case.open(target)
    assert reopened.fault_kind == state_file.NEWER_VERSION
    assert case.readable(reopened)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_file_a_newer_build_wrote_after_opening_is_refused_from_the_fresh_read(
    tmp_path: Path, case: Case
) -> None:
    """The decision is made from the locked read, not from the opened snapshot."""
    target = tmp_path / "state" / case.filename
    _write(target, case.current)
    store = case.open(target)
    assert store.fault is None
    newer = _write(target, case.newer)

    _label, mutate = case.mutations[0]
    _assert_refused(case, target, mutate, store)

    assert target.read_bytes() == newer
    assert not list(target.parent.glob(f"{case.filename}.broken*"))


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_newer_file_replaced_by_a_current_one_is_editable_again(
    tmp_path: Path, case: Case
) -> None:
    target = tmp_path / "state" / case.filename
    _write(target, case.newer)
    store = case.open(target)
    assert store.fault_kind == state_file.NEWER_VERSION
    _write(target, case.current)

    _label, mutate = case.mutations[0]
    mutate(store)

    assert store.fault is None
    assert store.fault_kind is None
    assert not list(target.parent.glob(f"{case.filename}.broken*"))


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_restructured_newer_file_is_newer_rather_than_corrupt(tmp_path: Path, case: Case) -> None:
    """A newer schema may move the very container an older parser expects."""
    target = tmp_path / "state" / case.filename
    original = _write(target, {"version": 99, "renamed-container": {"anything": [1, 2]}})

    store = case.open(target)
    assert store.fault_kind == state_file.NEWER_VERSION
    _label, mutate = case.mutations[0]
    _assert_refused(case, target, mutate, store)

    assert target.read_bytes() == original
    assert not list(target.parent.glob(f"{case.filename}.broken*"))


@pytest.mark.parametrize("marker", ("2", True, 0, -1))
@pytest.mark.parametrize(
    "case",
    [case for case in CASES if case.error is not removals.RemovalJournalError],
    ids=lambda case: case.name,
)
def test_a_damaged_version_marker_keeps_the_broken_file_recovery(
    tmp_path: Path, case: Case, marker: object
) -> None:
    """Only a newer integer is read-only; damage keeps the historical recovery."""
    target = tmp_path / "state" / case.filename
    damaged = dict(case.current)
    damaged["version"] = marker
    original = _write(target, damaged)

    store = case.open(target)
    assert store.fault_kind == state_file.UNREADABLE
    _label, mutate = case.mutations[0]
    mutate(store)

    assert store.fault is None
    assert (target.parent / f"{case.filename}.broken").read_bytes() == original


def test_a_damaged_removal_journal_still_refuses_as_invalid_state(tmp_path: Path) -> None:
    target = tmp_path / "state" / "pending-removals.json"
    original = _write(target, {"version": "2", "removals": []})

    store = removals.Store.open(target)
    assert store.fault_kind == state_file.UNREADABLE
    with pytest.raises(removals.RemovalJournalError) as caught:
        store.record_external(_item(A), (Path("/w"),))

    assert caught.value.kind == "invalid-state"
    assert target.read_bytes() == original


def test_pairings_health_sync_refuses_a_newer_file_before_the_fault_gate(tmp_path: Path) -> None:
    """The 30-second health sync writes Pairings too; it must not narrow it."""
    target = tmp_path / "state" / "pairings.json"
    original = _write(
        target,
        {"version": 3, "pairings": [{"identity": f"still:{A}", "palette": "builtin:Ocean"}]},
    )
    store = pairings.Store.open(target)

    with pytest.raises(pairings.PairingError) as caught:
        store.mark_borked_many(((_item(A), "renderer crashed", "runtime"),))

    assert caught.value.kind == "newer-version"
    assert target.read_bytes() == original


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_refusal_reaches_ctl_with_its_kind_and_sentence(tmp_path: Path, case: Case) -> None:
    """ctl prints the message and can branch on ``kind`` without parsing it."""
    from wall_in_one.control import server
    from wall_in_one.control.protocol import Response

    target = tmp_path / "state" / case.filename
    _write(target, case.newer)
    _label, mutate = case.mutations[0]
    with pytest.raises(case.error) as caught:
        mutate(case.open(target))

    response = Response.decode(server.failed(caught.value).encode())

    assert not response.ok
    assert response.kind == "newer-version"
    assert response.message == (
        f"newer-version: {case.filename} was saved by a newer version of Wall-in-One; "
        "open that version to change it. Nothing was changed."
    )


# -- unknown keys survive every save ------------------------------------------


def _saved(target: Path) -> dict[str, Any]:
    document = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_playlists_carry_unknown_keys_at_every_level_through_every_edit(tmp_path: Path) -> None:
    target = tmp_path / "playlists.json"
    _write(
        target,
        {
            "version": 1,
            "written_by": "0.2.0",
            "playlists": [
                {
                    "id": "x",
                    "name": "X",
                    "description": "for evenings",
                    "interval": 30,
                    "entries": [{"id": "e", "source": str(A), "weight": 2}],
                },
                {"id": "y", "name": "Y", "description": "doomed", "entries": []},
            ],
        },
    )
    store = playlists.Store.open(target)
    assert store.fault is None

    store.create("Z", entry_id="z")
    store.rename("x", "Renamed")
    store.add("x", B, entry_id="f")
    store.move_entry("x", "e", 1)
    store.delete("y")

    saved = _saved(target)
    assert saved["written_by"] == "0.2.0"
    by_id = {record["id"]: record for record in saved["playlists"]}
    assert set(by_id) == {"x", "z"}
    assert by_id["x"]["name"] == "Renamed"
    assert by_id["x"]["description"] == "for evenings"
    assert by_id["x"]["interval"] == 30
    assert by_id["x"]["entries"] == [
        {"id": "f", "source": str(B)},
        {"id": "e", "source": str(A), "weight": 2},
    ]
    assert by_id["z"] == {"id": "z", "name": "Z", "entries": []}
    assert not list(tmp_path.glob("*.broken*"))


def test_schedules_carry_unknown_keys_without_resurrecting_cleared_fields(
    tmp_path: Path,
) -> None:
    target = tmp_path / "schedules.json"
    _write(
        target,
        {
            "version": 2,
            "note": "kept",
            "rules": [
                {"id": "a", "playlist": "X", "name": "Evening rule", "enabled": False},
                {"id": "b", "playlist": "Y", "start": "21:00", "end": "04:00"},
            ],
        },
    )
    store = schedules.Store.open(target)

    store.add("Z", rule_id="c")
    store.set_enabled("a", True)
    store.update("b", "Y")
    store.move("a", 2)

    saved = _saved(target)
    assert saved["note"] == "kept"
    assert saved["rules"] == [
        {"id": "b", "playlist": "Y"},
        {"id": "c", "playlist": "Z"},
        {"id": "a", "playlist": "X", "name": "Evening rule"},
    ]


def test_the_lazy_schedule_upgrade_carries_unknown_keys_into_version_two(
    tmp_path: Path,
) -> None:
    target = tmp_path / "schedules.json"
    _write(
        target,
        {"version": 1, "note": "kept", "rules": [{"id": "a", "playlist": "X", "name": "N"}]},
    )
    store = schedules.Store.open(target)
    assert store.fault is None

    store.add("Y", rule_id="b")

    saved = _saved(target)
    assert saved["version"] == 2
    assert saved["note"] == "kept"
    assert saved["rules"][0] == {"id": "a", "playlist": "X", "name": "N"}


def test_pairings_carry_unknown_keys_through_edits_and_the_health_sync(tmp_path: Path) -> None:
    target = tmp_path / "pairings.json"
    _write(
        target,
        {
            "version": 2,
            "origin": "0.2.0",
            "pairings": [
                {
                    "identity": f"still:{A}",
                    "palette": "builtin:Ocean",
                    "label": "sea",
                    "health": {
                        "state": "borked",
                        "reason": "old",
                        "source": "runtime",
                        "since": "2026-10-01",
                    },
                },
                {"identity": f"still:{B}", "palette": "keep", "label": "kept too"},
            ],
        },
    )
    store = pairings.Store.open(target)
    assert store.fault is None

    store.choose_palette(_item(A), pairings.PalettePolicy("builtin", "Forest"))
    store.mark_borked_many(((_item(B), "renderer crashed", "runtime"),))

    saved = _saved(target)
    assert saved["origin"] == "0.2.0"
    by_identity = {record["identity"]: record for record in saved["pairings"]}
    assert by_identity[f"still:{A}"]["palette"] == "builtin:Forest"
    assert by_identity[f"still:{A}"]["label"] == "sea"
    assert by_identity[f"still:{A}"]["health"]["since"] == "2026-10-01"
    assert by_identity[f"still:{B}"]["label"] == "kept too"
    assert by_identity[f"still:{B}"]["health"] == {
        "state": "borked",
        "reason": "renderer crashed",
        "source": "runtime",
    }

    # Clearing the health removes that nested object and its unknown fields
    # with it; the record's own unknown field stays.
    store.clear_borked(_item(A))
    record = {item["identity"]: item for item in _saved(target)["pairings"]}[f"still:{A}"]
    assert "health" not in record
    assert record["label"] == "sea"


def test_display_assignments_carry_unknown_top_level_keys(tmp_path: Path) -> None:
    target = tmp_path / "displays.json"
    _write(target, {"version": 1, "note": "kept", "displays": {"DP-1": "X"}})
    store = displays.Store.open(target)

    store.assign("DP-2", "Y")
    store.unassign("DP-1")

    assert _saved(target) == {"version": 1, "displays": {"DP-2": "Y"}, "note": "kept"}


def test_favourites_carry_unknown_top_level_keys(tmp_path: Path) -> None:
    target = tmp_path / "favourites.json"
    _write(target, {"version": 1, "note": "kept", "paths": [str(A)]})
    store = favourites.Store.open(target)

    store.add(B)
    store.discard(A)

    assert _saved(target) == {"version": 1, "paths": [str(B)], "note": "kept"}


def test_the_removal_journal_carries_unknown_keys_on_its_intents(tmp_path: Path) -> None:
    target = tmp_path / "pending-removals.json"
    first = removals.Store.open(target).record_external(_item(A), (Path("/w"),))
    document = _saved(target)
    document["note"] = "kept"
    document["removals"][0]["why"] = "user asked"
    _write(target, document)

    store = removals.Store.open(target)
    assert store.fault is None
    second = store.record_external(_item(B), (Path("/w"),))
    store.discard(second)

    saved = _saved(target)
    assert saved["note"] == "kept"
    assert [record["identity"] for record in saved["removals"]] == [first.identity]
    assert saved["removals"][0]["why"] == "user asked"


def test_a_corrupt_file_recovery_still_carries_what_it_could_parse(tmp_path: Path) -> None:
    """A same-version file with a malformed record keeps today's recovery."""
    target = tmp_path / "playlists.json"
    original = _write(
        target,
        {
            "version": 1,
            "note": "kept",
            "playlists": [
                {"id": "x", "name": "X", "description": "d", "entries": []},
                {"id": "", "name": "malformed"},
            ],
        },
    )
    store = playlists.Store.open(target)
    assert store.fault_kind == state_file.UNREADABLE

    store.create("Y", entry_id="y")

    assert (tmp_path / "playlists.json.broken").read_bytes() == original
    saved = _saved(target)
    assert saved["note"] == "kept"
    assert {record["id"]: record.get("description") for record in saved["playlists"]} == {
        "x": "d",
        "y": None,
    }


# -- newer files never feed runtime compilation -------------------------------


def _profile(tmp_path: Path) -> Path:
    from wall_in_one import config

    media = tmp_path / "media"
    media.mkdir()
    (media / "a.png").write_bytes(b"fixture")
    (media / "b.png").write_bytes(b"fixture")
    config.save(config.Settings(roots=(media,), scan_workshop=False))
    return media


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_refused_edit_never_lets_a_newer_file_reach_the_runtime_compiler(
    tmp_path: Path, case: Case, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before the guard, the edit narrowed the file and the next compile used it."""
    from wall_in_one import cli, config, paths, runtime_config
    from wall_in_one.session import Session

    _profile(tmp_path)
    module: Any = {
        "playlists.json": playlists,
        "schedules.json": schedules,
        "pairings.json": pairings,
        "displays.json": displays,
        "favourites.json": favourites,
        "pending-removals.json": removals,
    }[case.filename]
    target = module.state_path()
    original = _write(target, case.newer)
    runtime = paths.runtime_config_path()
    runtime.parent.mkdir(parents=True, exist_ok=True)
    last_good = b'schema_version = 4\nlast_known_good = "preserve me"\n'
    runtime.write_bytes(last_good)

    store = case.open(target)
    for _label, mutate in case.mutations:
        with pytest.raises(case.error):
            mutate(store)
    assert target.read_bytes() == original

    session = Session(config.Settings(scan_workshop=False))
    try:
        with pytest.raises(runtime_config.RuntimeConfigError) as caught:
            runtime_config.render(session.settings, session)
    finally:
        session.shutdown()
    assert f"{case.filename} was saved by a newer version of Wall-in-One" in str(caught.value)

    assert cli.main(["--write-config"]) == 1
    error = capsys.readouterr().err
    assert "authoring state is unreadable or was saved by a newer version" in error
    assert f"{case.filename} was saved by a newer version" in error
    assert "left untouched" in error
    assert runtime.read_bytes() == last_good
    assert target.read_bytes() == original
    assert not list(target.parent.glob(f"{case.filename}.broken*"))


def test_unknown_keys_in_current_files_do_not_change_the_compiled_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from wall_in_one import cli, paths

    media = _profile(tmp_path)
    a, b = str(media / "a.png"), str(media / "b.png")
    documents: dict[Path, tuple[dict[str, Any], dict[str, Any]]] = {
        playlists.state_path(): (
            {
                "version": 1,
                "playlists": [
                    {"id": "x", "name": "X", "entries": [{"id": "e", "source": a}]},
                ],
            },
            {
                "version": 1,
                "unknown_top": "UNKNOWN-1",
                "playlists": [
                    {
                        "id": "x",
                        "name": "X",
                        "description": "UNKNOWN-2",
                        "entries": [{"id": "e", "source": a, "weight": "UNKNOWN-3"}],
                    },
                ],
            },
        ),
        schedules.state_path(): (
            {"version": 2, "rules": [{"id": "r", "playlist": "x"}]},
            {
                "version": 2,
                "unknown_top": "UNKNOWN-4",
                "rules": [{"id": "r", "playlist": "x", "name": "UNKNOWN-5"}],
            },
        ),
        pairings.state_path(): (
            {"version": 2, "pairings": [{"identity": f"still:{b}", "palette": "keep"}]},
            {
                "version": 2,
                "unknown_top": "UNKNOWN-6",
                "pairings": [{"identity": f"still:{b}", "palette": "keep", "x": "UNKNOWN-7"}],
            },
        ),
        displays.state_path(): (
            {"version": 1, "displays": {"DP-1": "x"}},
            {"version": 1, "displays": {"DP-1": "x"}, "unknown_top": "UNKNOWN-8"},
        ),
        favourites.state_path(): (
            {"version": 1, "paths": [a]},
            {"version": 1, "paths": [a], "unknown_top": "UNKNOWN-9"},
        ),
    }

    for target, (plain, _extended) in documents.items():
        _write(target, plain)
    assert cli.main(["--write-config"]) == 0
    runtime = paths.runtime_config_path()
    plain_runtime = runtime.read_bytes()

    for target, (_plain, extended) in documents.items():
        _write(target, extended)
    assert cli.main(["--write-config"]) == 0
    capsys.readouterr()

    assert runtime.read_bytes() == plain_runtime
    assert b"UNKNOWN" not in plain_runtime
