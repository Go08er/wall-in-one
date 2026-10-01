"""Release 2's per-playlist rotation and display opt-in in their stores.

Per-playlist interval and shuffle live in ``playlists.json`` version 2; a
display's opt-in to beat global schedule rules lives in ``displays.json``
version 2. The transition rule: nothing changes for a profile until
somebody uses one.

* Lazy bump: a store moves to version 2 only on the save that first uses a
  new field, and never moves back.
* One-time backup: before that first bump the version-1 bytes are kept as
  ``<file>.v1-backup``, byte for byte, never overwritten.
* Saving never consults the running service: the runtime reads these from a
  file only this release's service opens (see the compiler's tests).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import displays, playlists
from wall_in_one.library.playlists import KEEP, PlaylistError


def _no_service_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_arguments: object, **_keywords: object) -> Response:
        pytest.fail("this save consulted the session runtime")

    monkeypatch.setattr(client, "send_runtime", unexpected)


def _backup(path: Path) -> Path:
    return path.with_name(f"{path.name}.v1-backup")


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


@pytest.fixture
def evening(tmp_path: Path) -> tuple[playlists.Store, str, Path]:
    """A version-1 ``playlists.json`` at the profile's own path, one entry long."""
    picture = tmp_path / "library" / "evening.png"
    picture.parent.mkdir()
    picture.write_bytes(b"fixture")
    store = playlists.Store.open()
    made = store.create("Evening")
    store.add(made.id, picture)
    target = playlists.state_path()
    assert _json(target)["version"] == 1
    return store, made.id, target


# -- playlists.json --------------------------------------------------------------------


def test_edits_that_use_no_new_field_keep_version_one_byte_for_byte(
    evening: tuple[playlists.Store, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identifier, target = evening
    _no_service_call(monkeypatch)
    store.rename(identifier, "Late evening")
    store.create("Morning")
    document = _json(target)
    assert document["version"] == 1
    for playlist in document["playlists"]:
        assert set(playlist) == {"id", "name", "entries"}
    # Exactly what Release 1 writes for the same playlists: key order included.
    expected = {
        "version": 1,
        "playlists": [
            {
                "id": playlist.id,
                "name": playlist.name,
                "entries": [{"id": entry.id, "source": entry.source} for entry in playlist.entries],
            }
            for playlist in sorted(store.all(), key=lambda one: one.id)
        ],
    }
    assert target.read_text(encoding="utf-8") == json.dumps(expected, indent=2) + "\n"
    assert not _backup(target).exists()


def test_the_first_rotation_bumps_once_after_keeping_the_version_one_bytes(
    evening: tuple[playlists.Store, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identifier, target = evening
    original = target.read_bytes()

    updated = store.set_rotation(identifier, cycle_interval=60)

    assert (updated.cycle_interval, updated.shuffle) == (60, None)
    document = _json(target)
    assert document["version"] == 2
    (saved,) = document["playlists"]
    assert saved["cycle_interval"] == 60 and "shuffle" not in saved
    assert _backup(target).read_bytes() == original
    assert _backup(target).stat().st_mode & 0o777 == target.stat().st_mode & 0o777

    # Later saves -- more rotation, clearing it, unrelated edits -- leave the
    # backup alone, and the file never moves back to version 1.
    store.set_rotation(identifier, shuffle=True)
    assert playlists.Store.open().find(identifier).shuffle is True
    store.set_rotation(identifier, cycle_interval=None, shuffle=None)
    store.rename(identifier, "Night")
    assert _json(target)["version"] == 2
    assert set(_json(target)["playlists"][0]) == {"id", "name", "entries"}
    store.set_rotation(identifier, cycle_interval=300)
    assert _backup(target).read_bytes() == original
    assert sorted(path.name for path in target.parent.glob("playlists.json.*backup*")) == [
        "playlists.json.v1-backup"
    ]


def test_an_existing_backup_is_never_overwritten(
    evening: tuple[playlists.Store, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identifier, target = evening
    _backup(target).write_bytes(b"an earlier first bump")
    store.set_rotation(identifier, shuffle=False)
    assert _backup(target).read_bytes() == b"an earlier first bump"
    assert _json(target)["version"] == 2


def test_no_backup_means_no_bump(
    evening: tuple[playlists.Store, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identifier, target = evening
    original = target.read_bytes()
    _backup(target).mkdir()
    with pytest.raises(PlaylistError) as caught:
        store.set_rotation(identifier, cycle_interval=60)
    assert caught.value.kind == "no-backup"
    assert "Nothing was changed" in str(caught.value)
    assert target.read_bytes() == original
    assert store.find(identifier).cycle_interval is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"cycle_interval": 4}, "5 to 86400"),
        ({"cycle_interval": 86_401}, "5 to 86400"),
        ({"cycle_interval": True}, "5 to 86400"),
        ({"cycle_interval": 60.0}, "5 to 86400"),
        ({"shuffle": "yes"}, "on, off or the default"),
        ({"shuffle": 1}, "on, off or the default"),
    ],
)
def test_rotation_values_are_validated_before_anything_is_read(
    evening: tuple[playlists.Store, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    message: str,
) -> None:
    store, identifier, target = evening
    original = target.read_bytes()
    _no_service_call(monkeypatch)
    with pytest.raises(PlaylistError, match=message) as caught:
        store.set_rotation(identifier, **changes)
    assert caught.value.kind == "validation"
    assert target.read_bytes() == original


def test_setting_what_is_already_there_writes_nothing(
    evening: tuple[playlists.Store, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, identifier, target = evening
    store.set_rotation(identifier, cycle_interval=60, shuffle=True)
    written = target.read_bytes()
    identity = target.stat().st_ino
    store.set_rotation(identifier, cycle_interval=60, shuffle=KEEP)
    store.set_rotation(identifier, cycle_interval=KEEP, shuffle=KEEP)
    assert target.read_bytes() == written
    assert target.stat().st_ino == identity


def test_a_file_written_fresh_starts_at_the_version_its_data_needs(tmp_path: Path) -> None:
    plain = playlists.Playlist(id="a", name="A")
    spaced = playlists.Playlist(id="b", name="B", cycle_interval=120)
    playlists.save({"a": plain}, tmp_path / "plain.json")
    playlists.save({"a": plain, "b": spaced}, tmp_path / "spaced.json")
    assert _json(tmp_path / "plain.json")["version"] == 1
    assert _json(tmp_path / "spaced.json")["version"] == 2
    with pytest.raises(ValueError, match="need 2"):
        playlists.save({"b": spaced}, tmp_path / "narrow.json", version=1)
    assert list(tmp_path.glob("*backup*")) == []


def test_quick_choice_keeps_its_own_rotation_when_it_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    store = playlists.Store.open()
    store.set_singleton("quick-choice", "Quick choice", first)
    store.set_display_singleton("DP-1", first)
    display_choice = playlists.display_quick_choice_id("DP-1")
    store.set_rotation("quick-choice", cycle_interval=30)
    store.set_rotation(display_choice, shuffle=True)
    store.set_singleton("quick-choice", "Quick choice", second)
    store.set_display_singleton("DP-1", second)
    reopened = playlists.Store.open()
    assert reopened.find("quick-choice").cycle_interval == 30
    assert reopened.find("quick-choice").entries[0].source == str(second)
    assert reopened.find(display_choice).shuffle is True


def test_an_unusable_rotation_value_is_damage_and_never_compiled(tmp_path: Path) -> None:
    target = tmp_path / "playlists.json"
    target.write_text(
        json.dumps(
            {
                "version": 2,
                "playlists": [
                    {"id": "a", "name": "A", "cycle_interval": 3, "entries": []},
                    {"id": "b", "name": "B", "shuffle": "on", "entries": []},
                    {"id": "c", "name": "C", "cycle_interval": 90, "shuffle": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = playlists.Store.open(target)
    assert store.fault is not None and "2 invalid playlist rotation settings" in store.fault
    assert store.fault_kind == "unreadable"
    assert store.find("a").cycle_interval is None
    assert store.find("b").shuffle is None
    assert (store.find("c").cycle_interval, store.find("c").shuffle) == (90, False)


# -- displays.json ---------------------------------------------------------------------


@pytest.fixture
def docked() -> tuple[displays.Store, Path]:
    store = displays.Store.open()
    store.assign("DP-1", "Evening")
    store.assign("HDMI-A-1", "Morning")
    target = displays.state_path()
    assert _json(target) == {
        "version": 1,
        "displays": {"DP-1": "Evening", "HDMI-A-1": "Morning"},
    }
    return store, target


def test_display_edits_that_use_no_new_field_keep_version_one(
    docked: tuple[displays.Store, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, target = docked
    _no_service_call(monkeypatch)
    store.assign("DP-2", "Night")
    store.unassign("DP-2")
    store.set_beats_global_rules("DP-1", False)
    assert _json(target) == {"version": 1, "displays": {"DP-1": "Evening", "HDMI-A-1": "Morning"}}
    assert not _backup(target).exists()


def test_the_display_opt_in_bumps_once_and_belongs_to_its_assignment(
    docked: tuple[displays.Store, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, target = docked
    original = target.read_bytes()

    assert store.set_beats_global_rules("DP-1", True) is True
    assert store.set_beats_global_rules("DP-1", True) is False

    assert _json(target) == {
        "version": 2,
        "displays": {"DP-1": "Evening", "HDMI-A-1": "Morning"},
        "beats_global_rules": ["DP-1"],
    }
    assert _backup(target).read_bytes() == original
    reopened = displays.Store.open()
    assert reopened.beats_global_rules("DP-1")
    assert not reopened.beats_global_rules("HDMI-A-1")
    assert reopened.describe() == (
        "DP-1\tEvening  (beats global rules)",
        "HDMI-A-1\tMorning",
    )

    # Reassigning keeps the opt-in; losing the assignment loses it.
    store.assign("DP-1", "Night")
    assert displays.Store.open().beats_global_rules("DP-1")
    store.set_beats_global_rules("HDMI-A-1", True)
    store.unassign("DP-1")
    assert store.forget_playlist("Morning") == 1
    assert _json(target) == {"version": 2, "displays": {}}
    assert _backup(target).read_bytes() == original

    with pytest.raises(displays.DisplayError, match="assign one before") as caught:
        store.set_beats_global_rules("DP-1", True)
    assert caught.value.kind == "validation"


def test_a_worker_forgetting_a_playlist_takes_the_opt_in_with_it(
    docked: tuple[displays.Store, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _target = docked
    store.set_beats_global_rules("DP-1", True)
    assert store.adopt_worker_forget_playlists(["Evening"]) == 1
    assert not store.beats_global_rules("DP-1")


@pytest.mark.parametrize(
    "flagged",
    [["DP-9"], ["DP-1", "DP-1"], "DP-1", [7]],
    ids=["unassigned", "duplicate", "not-a-list", "not-a-connector"],
)
def test_a_malformed_opt_in_is_damage(tmp_path: Path, flagged: object) -> None:
    target = tmp_path / "displays.json"
    target.write_text(
        json.dumps({"version": 2, "displays": {"DP-1": "A"}, "beats_global_rules": flagged}),
        encoding="utf-8",
    )
    store = displays.Store.open(target)
    assert store.fault is not None and "beats_global_rules" in store.fault
    assert store.fault_kind == "unreadable"


# -- the running service ---------------------------------------------------------------


def _first_use(kind: str) -> tuple[Callable[[], object], Path]:
    if kind == "playlists":
        store = playlists.Store.open()
        identifier = store.create("Evening").id
        return lambda: store.set_rotation(identifier, cycle_interval=60), playlists.state_path()
    display_store = displays.Store.open()
    display_store.assign("DP-1", "Evening")
    return lambda: display_store.set_beats_global_rules("DP-1", True), displays.state_path()


@pytest.mark.parametrize("kind", ["playlists", "displays"])
def test_saving_never_consults_the_running_service(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Unlike the battery option, these never reach a file an old service opens."""
    use, target = _first_use(kind)
    _no_service_call(monkeypatch)
    use()
    assert _json(target)["version"] == 2
