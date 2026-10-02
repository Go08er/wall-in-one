"""0.2.0's runtime-backed features, from authoring to the runtime files.

Per-playlist interval and shuffle live in ``playlists.json`` version 2; a
display's opt-in to beat global schedule rules lives in ``displays.json``
version 2. Both reach the runtime through ``runtime-overrides.toml``, a
sibling of ``runtime.toml`` that only this release's service reads. The
transition rule: nothing changes for a profile until somebody uses one.

* Lazy bump: a store moves to version 2 only on the save that first uses a
  new field, and never moves back.
* One-time backup: before that first bump the version-1 bytes are kept as
  ``<file>.v1-backup``, byte for byte, never overwritten.
* ``runtime.toml`` never changes because of them: with the fields in use it
  is byte-identical to the same profile with them cleared, so every released
  service still loads it. The overrides file exists only while one is used.
* Mixed versions: saving never consults the running service. One that
  predates the overrides file ignores it, so a setting applies once the
  updated service runs (``runtime_applies_overrides`` says which).
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from wall_in_one import cli, config, paths, runtime_config
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import displays, playlists
from wall_in_one.library.playlists import KEEP, PlaylistError
from wall_in_one.session import Session

V0_1_4_SERVICE = {"status_version": 2, "supported_config_schemas": [4, 5]}
THIS_SERVICE = {
    "status_version": 2,
    "supported_config_schemas": [4, 5],
    "supported_override_schemas": [1],
}


def _service(monkeypatch: pytest.MonkeyPatch, status: object) -> list[str]:
    asked: list[str] = []

    def send(verb: str, *, timeout: float) -> Response:
        asked.append(verb)
        return Response(ok=True, message=json.dumps(status))

    monkeypatch.setattr(client, "send_runtime", send)
    return asked


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
    # Exactly what 0.1.4 writes for the same playlists: key order included.
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


def test_status_says_whether_the_running_service_applies_overrides() -> None:
    assert runtime_config.runtime_applies_overrides(THIS_SERVICE)
    older: tuple[object, ...] = (
        V0_1_4_SERVICE,
        {"status_version": 2},
        None,
        [],
        {"supported_override_schemas": "1"},
    )
    for status in older:
        assert not runtime_config.runtime_applies_overrides(status)


# -- the runtime files ----------------------------------------------------------------


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    root.mkdir(exist_ok=True)
    (root / "one.png").write_bytes(b"fixture")
    (root / "two.png").write_bytes(b"fixture")
    return root


def _overrides(text: str | None) -> dict[str, Any]:
    assert text is not None
    document = tomllib.loads(text)
    assert document["schema_version"] == runtime_config.OVERRIDES_SCHEMA_VERSION
    return document


def test_runtime_toml_is_the_same_bytes_with_the_fields_in_use_or_cleared(
    library: Path,
) -> None:
    settings = config.Settings(roots=(library,), scan_workshop=False)
    session = Session(settings)
    session.refresh()
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    session.displays.assign("DP-1", made.id)
    cleared = runtime_config.render(settings, session)
    assert tomllib.loads(cleared)["schema_version"] == runtime_config.SCHEMA_VERSION
    assert runtime_config.render_overrides(settings, session) is None
    battery = replace(settings, stop_animations_on_battery=True)
    battery_cleared = runtime_config.render(battery, session)
    independent = replace(
        settings, display_mode=config.DISPLAY_MODE_INDEPENDENT, theme_source_connector="DP-1"
    )
    independent_cleared = runtime_config.render(independent, session)

    session.playlists.set_rotation(made.id, cycle_interval=60, shuffle=True)
    session.displays.set_beats_global_rules("DP-1", True)

    for chosen, expected in (
        (settings, cleared),
        (battery, battery_cleared),
        (independent, independent_cleared),
    ):
        assert runtime_config.render(chosen, session) == expected
        assert b"cycle_interval_seconds = 60" not in expected.encode()
        assert "beats_global_rules" not in expected
    assert tomllib.loads(battery_cleared)["schema_version"] == runtime_config.BATTERY_SCHEMA_VERSION
    assert _overrides(runtime_config.render_overrides(independent, session))["displays"] == [
        {"connector": "DP-1", "beats_global_rules": True}
    ]

    # Clearing them leaves the stores at version 2 and no overrides at all.
    session.playlists.set_rotation(made.id, cycle_interval=None, shuffle=None)
    session.displays.set_beats_global_rules("DP-1", False)
    assert _json(playlists.state_path())["version"] == 2
    assert runtime_config.render_overrides(independent, session) is None
    session.shutdown()


def test_overrides_carry_only_what_reaches_the_runtime(library: Path) -> None:
    mirrored = config.Settings(roots=(library,), scan_workshop=False)
    independent = replace(
        mirrored, display_mode=config.DISPLAY_MODE_INDEPENDENT, theme_source_connector="DP-1"
    )
    session = Session(mirrored)
    session.refresh()
    fast = session.playlists.create("Fast")
    session.playlists.add(fast.id, library / "one.png")
    session.playlists.set_rotation(fast.id, cycle_interval=45, shuffle=False)
    plain = session.playlists.create("Plain")
    session.playlists.add(plain.id, library / "two.png")
    # An override on a playlist that is not compiled goes nowhere.
    empty = session.playlists.create("Empty")
    session.playlists.set_rotation(empty.id, shuffle=True)
    session.displays.assign("DP-1", fast.id)
    session.displays.assign("HDMI-A-1", fast.id)
    session.displays.set_beats_global_rules("DP-1", True)

    routed = _overrides(runtime_config.render_overrides(independent, session))
    assert routed["playlists"] == [{"id": fast.id, "cycle_interval_seconds": 45, "shuffle": False}]
    assert routed["displays"] == [{"connector": "DP-1", "beats_global_rules": True}]
    # Mirrored mode leaves the assignments, and the opt-in, dormant.
    dormant = _overrides(runtime_config.render_overrides(mirrored, session))
    assert dormant == {
        "schema_version": 1,
        "playlists": [{"id": fast.id, "cycle_interval_seconds": 45, "shuffle": False}],
    }
    compiled_ids = {
        playlist["id"]
        for playlist in tomllib.loads(runtime_config.render(independent, session))["playlists"]
    }
    assert {entry["id"] for entry in routed["playlists"]} <= compiled_ids
    assert {display["connector"] for display in routed["displays"]} <= {
        display["connector"]
        for display in tomllib.loads(runtime_config.render(independent, session))["displays"]
    }
    session.shutdown()


def test_publication_writes_the_overrides_first_and_removes_them_durably(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = config.Settings(roots=(library,), scan_workshop=False)
    config.save(settings)
    session = Session(settings)
    session.refresh()
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    target = runtime_config.write(settings, session)
    sidecar = runtime_config.overrides_path(target)
    assert sidecar == target.with_name("runtime-overrides.toml")
    assert not sidecar.exists()
    released = target.read_bytes()
    identity = target.stat().st_ino
    assert runtime_config.update(settings, session) is False

    seen: list[bool] = []
    install = runtime_config._install

    def observing(document: str, path: Path) -> None:
        seen.append(runtime_config.overrides_path(path).exists())
        install(document, path)

    monkeypatch.setattr(runtime_config, "_install", observing)
    session.playlists.set_rotation(made.id, shuffle=True)
    assert runtime_config.update(settings, session) is True
    assert seen == [], "runtime.toml did not change, so it was not rewritten"
    assert (target.read_bytes(), target.stat().st_ino) == (released, identity)
    assert _overrides(sidecar.read_text())["playlists"] == [{"id": made.id, "shuffle": True}]
    assert runtime_config.update(settings, session) is False

    # Both change: the overrides land before runtime.toml.
    other = session.playlists.create("Other")
    session.playlists.add(other.id, library / "two.png")
    session.playlists.set_rotation(other.id, cycle_interval=600)
    assert runtime_config.update(settings, session) is True
    assert seen == [True]
    # In the compiled order: playlists by name, as runtime.toml lists them.
    assert [entry["id"] for entry in _overrides(sidecar.read_text())["playlists"]] == [
        made.id,
        other.id,
    ]

    session.playlists.set_rotation(made.id, shuffle=None)
    session.playlists.set_rotation(other.id, cycle_interval=None)
    assert runtime_config.update(settings, session) is True
    assert not sidecar.exists()
    assert runtime_config.update(settings, session) is False
    session.shutdown()


def test_a_refused_runtime_publication_writes_neither_file(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime.toml the service cannot load is refused before either file is written.

    Error copy matches what was written: the overrides wait for runtime.toml.
    A runtime.toml that fails while being written, after the overrides, is
    the next test's case.
    """
    settings = config.Settings(roots=(library,), scan_workshop=False)
    config.save(settings)
    session = Session(settings)
    session.refresh()
    target = runtime_config.write(settings, session)
    before = target.read_bytes()
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    session.playlists.set_rotation(made.id, shuffle=True)
    # Battery handling needs schema 5, which a v0.1.2 service cannot load.
    battery = replace(settings, stop_animations_on_battery=True)
    _service(monkeypatch, {"status_version": 2})
    for writer in (runtime_config.write, runtime_config.update):
        with pytest.raises(runtime_config.RuntimeConfigError, match="No changes were saved"):
            writer(battery, session)
        assert target.read_bytes() == before
        assert not runtime_config.overrides_path(target).exists()
    session.shutdown()


INJECTED_FAILURE = "injected runtime.toml write failure (ENOSPC)"


@pytest.mark.parametrize("writer", ["write", "update"])
@pytest.mark.parametrize("sidecar_change", ["changed", "appeared", "removed"])
def test_a_failed_runtime_install_puts_the_previous_overrides_back(
    library: Path, monkeypatch: pytest.MonkeyPatch, writer: str, sidecar_change: str
) -> None:
    """The overrides are published first; if runtime.toml then cannot be written,
    the previous pair is what remains, byte for byte, not new overrides beside
    the old runtime.toml."""
    settings = config.Settings(roots=(library,), scan_workshop=False)
    config.save(settings)
    session = Session(settings)
    session.refresh()
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    if sidecar_change != "appeared":
        session.playlists.set_rotation(made.id, cycle_interval=60)
    target = runtime_config.write(settings, session)
    sidecar = runtime_config.overrides_path(target)
    previous = (target.read_bytes(), sidecar.read_bytes() if sidecar.exists() else None)
    assert (previous[1] is None) == (sidecar_change == "appeared")

    # A change to both documents: the global interval and this playlist's own.
    changed = replace(settings, cycle_interval=600)
    session.playlists.set_rotation(
        made.id, cycle_interval=None if sidecar_change == "removed" else 120
    )
    published = runtime_config.render_overrides(changed, session)
    assert (published is None) == (sidecar_change == "removed")
    seen: list[bytes | None] = []

    def failing(document: str, path: Path) -> None:
        del document
        sidecar_now = runtime_config.overrides_path(path)
        seen.append(sidecar_now.read_bytes() if sidecar_now.exists() else None)
        raise runtime_config.RuntimeConfigError(INJECTED_FAILURE)

    install = runtime_config._install
    monkeypatch.setattr(runtime_config, "_install", failing)
    with pytest.raises(runtime_config.RuntimeConfigError, match=re.escape(INJECTED_FAILURE)):
        getattr(runtime_config, writer)(changed, session)

    assert seen == [None if published is None else published.encode()], (
        "the new overrides were published before runtime.toml was attempted"
    )
    assert (target.read_bytes(), sidecar.read_bytes() if sidecar.exists() else None) == previous
    temporaries = [
        entry.name
        for entry in target.parent.iterdir()
        if entry.name.startswith((f".{target.name}.", f".{sidecar.name}."))
        and not entry.name.endswith(".lock")
    ]
    assert temporaries == [], "no temporary of either write is left behind"

    # Nothing is stuck: the next save publishes the new pair. (Only `_install`
    # is put back: monkeypatch.undo() would also drop conftest's XDG sandbox.)
    monkeypatch.setattr(runtime_config, "_install", install)
    assert getattr(runtime_config, writer)(changed, session)
    assert target.read_bytes() != previous[0]
    assert (sidecar.read_bytes() if sidecar.exists() else None) == (
        None if published is None else published.encode()
    )
    session.shutdown()


def _intervals(target: Path) -> tuple[int, int | None]:
    """runtime.toml's global interval and the overrides file's one playlist interval."""
    main = tomllib.loads(target.read_text(encoding="utf-8"))["settings"]["cycle_interval_seconds"]
    sidecar = runtime_config.overrides_path(target)
    if not sidecar.exists():
        return main, None
    (playlist,) = tomllib.loads(sidecar.read_text(encoding="utf-8"))["playlists"]
    return main, playlist["cycle_interval_seconds"]


@pytest.mark.parametrize("writer", ["write", "update"])
@pytest.mark.parametrize("unsynced", ["runtime.toml", runtime_config.OVERRIDES_FILENAME])
def test_a_folder_sync_failure_after_publication_keeps_the_new_pair(
    library: Path, monkeypatch: pytest.MonkeyPatch, writer: str, unsynced: str
) -> None:
    """Sweep 3 S-1: published but not durable is not a failed publication.

    Main interval 300 with a playlist override of 60, saved as 600 and 120.
    The folder sync after one file's rename fails; every other sync runs.
    Putting the old overrides back would leave the new runtime.toml beside
    them (600 with 60), a pair nobody asked for. The new pair stays, and the
    error says it may not survive a power loss.
    """
    settings = config.Settings(roots=(library,), scan_workshop=False, cycle_interval=300)
    config.save(settings)
    session = Session(settings)
    session.refresh()
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    session.playlists.set_rotation(made.id, cycle_interval=60)
    target = runtime_config.write(settings, session)
    assert _intervals(target) == (300, 60)
    changed = replace(settings, cycle_interval=600)
    session.playlists.set_rotation(made.id, cycle_interval=120)

    renamed: list[str] = []
    failed: list[str] = []
    real_replace, real_fsync = os.replace, os.fsync

    def rename(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        renamed.append(Path(destination).name)

    def sync(descriptor: int) -> None:
        if renamed[-1:] == [unsynced] and not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed.append(unsynced)
            raise OSError(errno.EIO, "injected directory fsync failure after publication")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "replace", rename)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(runtime_config.RuntimeConfigError) as caught:
        getattr(runtime_config, writer)(changed, session)
    # Only the injections are put back (monkeypatch.undo() would also drop
    # conftest's XDG sandbox).
    monkeypatch.setattr(os, "replace", real_replace)
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert failed == [unsynced]
    assert _intervals(target) == (600, 120), "the requested pair, never new main with old overrides"
    assert renamed == [runtime_config.OVERRIDES_FILENAME, "runtime.toml"], "both were published"
    assert type(caught.value).__name__ == "RuntimeConfigNotDurableError", caught.value
    assert str(caught.value).startswith(f"{unsynced} was saved, but syncing its folder failed")
    assert "may not survive a power loss" in str(caught.value)
    assert runtime_config.update(changed, session) is False, "nothing is left to publish"
    session.shutdown()


def test_write_config_adopts_a_pair_published_without_its_folder_sync(
    library: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The service-start compiler: written, with a warning, not "could not be compiled"."""
    settings = config.Settings(roots=(library,), scan_workshop=False, cycle_interval=300)
    config.save(settings)
    store = playlists.Store.open()
    made = store.create("Evening")
    store.add(made.id, library / "one.png")
    store.set_rotation(made.id, cycle_interval=60)
    assert cli.main(["--write-config"]) == 0
    target = paths.runtime_config_path()
    assert _intervals(target) == (300, 60)
    config.save(replace(settings, cycle_interval=600))
    playlists.Store.open().set_rotation(made.id, cycle_interval=120)

    renamed: list[str] = []
    real_replace, real_fsync = os.replace, os.fsync

    def rename(source: Any, destination: Any) -> None:
        real_replace(source, destination)
        renamed.append(Path(destination).name)

    def sync(descriptor: int) -> None:
        if renamed[-1:] == ["runtime.toml"] and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            renamed.append("(sync refused)")
            raise OSError(errno.EIO, "Input/output error")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "replace", rename)
    monkeypatch.setattr(os, "fsync", sync)
    capsys.readouterr()
    status = cli.main(["--write-config"])
    monkeypatch.setattr(os, "replace", real_replace)
    monkeypatch.setattr(os, "fsync", real_fsync)

    out, err = capsys.readouterr()
    assert status == 0, err
    assert "(sync refused)" in renamed
    assert out == f"wrote: {target}\n"
    assert err.startswith("warning: runtime.toml was saved, but syncing its folder failed"), err
    assert "could not be compiled" not in err
    assert _intervals(target) == (600, 120)


def test_a_restore_that_is_put_back_but_not_synced_says_so(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runtime.toml refused after the overrides were published: they are put back;
    if only the sync of that restore fails, the previous pair is on disk."""
    settings = config.Settings(roots=(library,), scan_workshop=False, cycle_interval=300)
    config.save(settings)
    session = Session(settings)
    session.refresh()
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    session.playlists.set_rotation(made.id, cycle_interval=60)
    target = runtime_config.write(settings, session)
    session.playlists.set_rotation(made.id, cycle_interval=120)
    publish = runtime_config._publish_overrides
    calls: list[str | None] = []

    def restore_not_synced(overrides: str | None, path: Path) -> None:
        calls.append(overrides)
        publish(overrides, path)
        if len(calls) > 1:
            raise runtime_config.RuntimeConfigNotDurableError(
                (runtime_config.overrides_path(path),), OSError(errno.EIO, "injected")
            )

    def failing(document: str, path: Path) -> None:
        raise runtime_config.RuntimeConfigError(INJECTED_FAILURE)

    monkeypatch.setattr(runtime_config, "_publish_overrides", restore_not_synced)
    monkeypatch.setattr(runtime_config, "_install", failing)
    with pytest.raises(runtime_config.RuntimeConfigError) as caught:
        runtime_config.update(replace(settings, cycle_interval=600), session)

    assert not isinstance(caught.value, runtime_config.RuntimeConfigNotDurableError)
    message = str(caught.value)
    assert INJECTED_FAILURE in message and "was put back, but" in message
    assert "could not be put back" not in message
    assert _intervals(target) == (300, 60), "the previous pair"
    session.shutdown()


def test_a_restore_that_fails_too_says_the_pair_may_disagree(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = config.Settings(roots=(library,), scan_workshop=False)
    config.save(settings)
    session = Session(settings)
    session.refresh()
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    session.playlists.set_rotation(made.id, cycle_interval=60)
    runtime_config.write(settings, session)
    session.playlists.set_rotation(made.id, cycle_interval=120)
    publish = runtime_config._publish_overrides
    calls: list[str | None] = []

    def publish_once(overrides: str | None, target: Path) -> None:
        calls.append(overrides)
        if len(calls) > 1:
            raise runtime_config.RuntimeConfigError("injected restore failure")
        publish(overrides, target)

    def failing(document: str, path: Path) -> None:
        raise runtime_config.RuntimeConfigError(INJECTED_FAILURE)

    monkeypatch.setattr(runtime_config, "_publish_overrides", publish_once)
    monkeypatch.setattr(runtime_config, "_install", failing)
    with pytest.raises(runtime_config.RuntimeConfigError) as caught:
        runtime_config.update(replace(settings, cycle_interval=600), session)
    message = str(caught.value)
    assert INJECTED_FAILURE in message and "injected restore failure" in message
    assert "could not be put back" in message and "may not match runtime.toml" in message
    assert len(calls) == 2, "the restore was attempted with the previous bytes"
    session.shutdown()


def test_a_v0_1_4_service_still_gets_both_files_and_ignores_the_overrides(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = config.Settings(
        roots=(library,), scan_workshop=False, stop_animations_on_battery=True
    )
    _service(monkeypatch, V0_1_4_SERVICE)
    config.save(settings)
    session = Session(settings)
    session.refresh()
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    session.playlists.set_rotation(made.id, cycle_interval=120)
    target = runtime_config.write(settings, session)
    assert tomllib.loads(target.read_text())["schema_version"] == 5
    assert runtime_config.overrides_path(target).is_file()
    assert not runtime_config.runtime_applies_overrides(V0_1_4_SERVICE)
    session.shutdown()


NEWER_OVERRIDES = (
    'schema_version = 2\n[[playlists]]\nid = "x"\nweighting = "recent"\n[[wallpapers]]\n'
)


def test_an_overrides_file_from_a_newer_build_is_never_rewritten_or_removed(
    library: Path,
) -> None:
    """After a rollback, this build's compiler leaves the newer release's file as it was."""
    settings = config.Settings(roots=(library,), scan_workshop=False)
    config.save(settings)
    session = Session(settings)
    session.refresh()
    target = runtime_config.write(settings, session)
    sidecar = runtime_config.overrides_path(target)
    sidecar.write_text(NEWER_OVERRIDES, encoding="utf-8")
    assert runtime_config.overrides_from_a_newer_build(NEWER_OVERRIDES)

    # Nothing in use: no removal. In use: no replacement. runtime.toml is
    # still published as usual.
    assert runtime_config.update(settings, session) is False
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    session.playlists.set_rotation(made.id, shuffle=True)
    assert runtime_config.update(settings, session) is True
    assert tomllib.loads(target.read_text())["playlists"][-1]["id"] == made.id
    runtime_config.write(settings, session)
    assert sidecar.read_text(encoding="utf-8") == NEWER_OVERRIDES

    for ours in ("schema_version = 1\n", "not toml [", 'schema_version = "2"\n', None):
        assert not runtime_config.overrides_from_a_newer_build(ours)
    session.shutdown()


def test_the_constants_name_one_contract() -> None:
    assert runtime_config.OVERRIDES_FILENAME == "runtime-overrides.toml"
    assert runtime_config.OVERRIDES_SCHEMA_VERSION == 1
    assert playlists.ROTATION_VERSION == displays.PRECEDENCE_VERSION == 2


def _service_binary() -> Path | None:
    # Only an explicitly chosen build: never whatever happens to be installed.
    configured = os.environ.get("WALL_IN_ONE_SERVICE_BINARY", "")
    return Path(configured) if configured else None


def test_the_rust_service_loads_what_the_compiler_writes(library: Path, tmp_path: Path) -> None:
    """Cross-binary: the strict Rust loader takes runtime.toml plus its overrides."""
    binary = _service_binary()
    if binary is None:
        pytest.skip("set WALL_IN_ONE_SERVICE_BINARY to check the compiled files in Rust")
    settings = replace(
        config.Settings(roots=(library,), scan_workshop=False),
        display_mode=config.DISPLAY_MODE_INDEPENDENT,
        theme_source_connector="DP-1",
        stop_animations_on_battery=True,
    )
    session = Session(settings)
    session.refresh()
    made = session.playlists.create("Evening")
    session.playlists.add(made.id, library / "one.png")
    session.playlists.set_rotation(made.id, cycle_interval=60, shuffle=True)
    session.displays.assign("DP-1", made.id)
    session.displays.set_beats_global_rules("DP-1", True)
    folder = tmp_path / "compiled"
    folder.mkdir()
    document = folder / "runtime.toml"
    document.write_text(runtime_config.render(settings, session), encoding="utf-8")
    sidecar = runtime_config.overrides_path(document)
    overrides = runtime_config.render_overrides(settings, session)
    assert overrides is not None
    sidecar.write_text(overrides, encoding="utf-8")
    session.shutdown()

    def check() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(binary), "--config", str(document), "--check-config"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    checked = check()
    assert checked.returncode == 0, checked.stderr
    sidecar.write_text(overrides + "unknown = true\n", encoding="utf-8")
    refused = check()
    assert refused.returncode != 0
    assert "runtime-overrides.toml" in refused.stderr
    # A newer release's schema is not this service's to judge: it starts
    # without it and says so.
    sidecar.write_text(NEWER_OVERRIDES, encoding="utf-8")
    newer = check()
    assert newer.returncode == 0, newer.stderr
    assert "not applied: unsupported schema_version 2" in newer.stderr
