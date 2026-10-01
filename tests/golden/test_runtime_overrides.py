"""Per-playlist rotation and the display opt-in on a whole profile.

The transition rule: nothing changes what plays after an upgrade until the
person sets something. On the golden profile that means:

* an edit that uses no new field keeps ``playlists.json`` and
  ``displays.json`` at version 1, byte for byte what 0.1.4 writes, and no
  ``runtime-overrides.toml`` ever appears;
* the first per-playlist interval or shuffle, and the first display opt-in,
  bump their store once, after exactly one ``<file>.v1-backup`` of the
  released bytes;
* ``runtime.toml`` never changes because of them. The overrides go to
  ``runtime-overrides.toml``, which exists only while one is in use;
* a running 0.1.4 service never blocks saving them;
* idle with them in use writes only the unchanged first-start whitelist;
* an overrides file from a newer release (a schema this build does not know)
  neither stops the service unit's start nor is touched by this build.

Every check diffs the whole sandbox home. What older builds make of these
files after a rollback is in ``test_downgrade``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest

from tests.golden import harness, sandbox
from tests.golden.harness import Allowance, Change, Profile
from tests.golden.sandbox import STATE, Golden, playlist_ids, read_json
from tests.golden.test_idle import first_start_writes
from wall_in_one import cli, config, runtime_config
from wall_in_one.library import displays, playlists

PLAYLISTS: Final = f"{STATE}/playlists.json"
PLAYLISTS_BACKUP: Final = f"{STATE}/playlists.json.v1-backup"
DISPLAYS: Final = f"{STATE}/displays.json"
DISPLAYS_BACKUP: Final = f"{STATE}/displays.json.v1-backup"
OVERRIDES: Final = f"{STATE}/{runtime_config.OVERRIDES_FILENAME}"


def _serialized(document: dict[str, Any]) -> bytes:
    """How every version of these two files has been written."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()


def _only(change: Change) -> tuple[dict[str, Any], dict[str, Any]]:
    assert change.before is not None and change.before.content is not None
    assert change.after is not None and change.after.content is not None
    return json.loads(change.before.content), json.loads(change.after.content)


def _schema(profile: Profile) -> int:
    value = tomllib.loads((profile.app_state / "runtime.toml").read_text())["schema_version"]
    assert isinstance(value, int)
    return value


def _released_bytes(released: bytes, mode: int) -> Any:
    def check(change: Change) -> None:
        assert change.after is not None
        assert change.after.content == released, "the backup is not the released file"
        assert change.after.mode == mode

    return check


def _overrides_are(expected: dict[str, Any]) -> Any:
    def check(change: Change) -> None:
        assert change.after is not None and change.after.content is not None
        assert tomllib.loads(change.after.content.decode()) == expected

    return check


def _write_config(capsys: pytest.CaptureFixture[str]) -> str:
    assert cli.main(["--write-config"]) == 0
    return capsys.readouterr().out


def test_the_fixture_compiles_with_no_overrides_from_version_one_stores(
    golden: Golden, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = golden.profile
    for name in ("playlists.json", "displays.json"):
        target = profile.app_state / name
        document = read_json(target)
        assert document["version"] == 1, name
        assert target.read_bytes() == _serialized(document), name
    assert all(not playlist.has_rotation_override for playlist in playlists.Store.open().all())
    _write_config(capsys)
    assert _schema(profile) <= runtime_config.BATTERY_SCHEMA_VERSION
    assert not (profile.app_state / runtime_config.OVERRIDES_FILENAME).exists()
    assert sorted(profile.app_state.glob("*-backup")) == []


def test_edits_using_no_new_field_keep_both_files_at_version_one(golden: Golden) -> None:
    profile = golden.profile
    ids = playlist_ids(profile)
    before = harness.snapshot(profile.home)

    store = playlists.Store.open()
    store.rename(ids[0], "Renamed, still version 1")
    store.set_rotation(ids[1], cycle_interval=None, shuffle=None)
    display_store = displays.Store.open()
    display_store.assign("DP-9", ids[0])
    display_store.set_beats_global_rules("DP-1", False)

    def stays_version_one(change: Change) -> None:
        old, new = _only(change)
        assert old["version"] == new["version"] == 1
        assert change.after is not None and change.after.content == _serialized(new)
        for playlist in new.get("playlists", []):
            assert set(playlist) == {"id", "name", "entries"}

    harness.check_changes(
        harness.diff(before, harness.snapshot(profile.home)),
        [
            Allowance(PLAYLISTS, frozenset({"modified"}), "the rename", stays_version_one),
            Allowance(DISPLAYS, frozenset({"modified"}), "the assignment", stays_version_one),
        ],
    )


def test_the_first_playlist_rotation_bumps_once_and_never_touches_runtime_toml(
    golden: Golden, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = golden.profile
    target = profile.app_state / "playlists.json"
    released = target.read_bytes()
    mode = stat.S_IMODE(target.stat().st_mode)
    identifier = playlist_ids(profile)[0]
    _write_config(capsys)
    before = harness.snapshot(profile.home)

    playlists.Store.open().set_rotation(identifier, cycle_interval=120, shuffle=False)

    def gained_one_rotation(change: Change) -> None:
        old, new = _only(change)
        assert old["version"] == 1
        expected = [
            {**playlist, "cycle_interval": 120, "shuffle": False}
            if playlist["id"] == identifier
            else playlist
            for playlist in old["playlists"]
        ]
        assert new == {"version": 2, "playlists": expected}

    after = harness.snapshot(profile.home)
    harness.check_changes(
        harness.diff(before, after),
        [
            Allowance(
                PLAYLISTS,
                frozenset({"modified"}),
                "the first rotation moves the file to version 2",
                gained_one_rotation,
            ),
            Allowance(
                PLAYLISTS_BACKUP,
                frozenset({"created"}),
                "guard 2: the bytes before the bump",
                _released_bytes(released, mode),
            ),
        ],
    )

    # Publication adds the overrides beside runtime.toml and nothing else.
    _write_config(capsys)
    published = harness.snapshot(profile.home)
    harness.check_changes(
        harness.diff(after, published),
        [
            Allowance(
                OVERRIDES,
                frozenset({"created"}),
                "the playlist's own rotation, for this release's service",
                _overrides_are(
                    {
                        "schema_version": 1,
                        "playlists": [
                            {"id": identifier, "cycle_interval_seconds": 120, "shuffle": False}
                        ],
                    }
                ),
            )
        ],
    )

    # Clearing it removes the overrides again; the store stays at version 2.
    playlists.Store.open().set_rotation(identifier, cycle_interval=None, shuffle=None)
    _write_config(capsys)
    cleared = harness.snapshot(profile.home)
    harness.check_changes(
        harness.diff(published, cleared),
        [
            Allowance(PLAYLISTS, frozenset({"modified"}), "the cleared rotation"),
            Allowance(OVERRIDES, frozenset({"deleted"}), "no override is in use any more"),
        ],
    )
    assert read_json(target)["version"] == 2
    assert (profile.app_state / "playlists.json.v1-backup").read_bytes() == released


def test_the_first_display_opt_in_bumps_once_and_reaches_only_independent_routing(
    golden: Golden, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = golden.profile
    target = profile.app_state / "displays.json"
    runtime = profile.app_state / "runtime.toml"
    sidecar = profile.app_state / runtime_config.OVERRIDES_FILENAME
    released = target.read_bytes()
    mode = stat.S_IMODE(target.stat().st_mode)
    _write_config(capsys)
    before = harness.snapshot(profile.home)

    displays.Store.open().set_beats_global_rules("DP-1", True)

    def gained_one_opt_in(change: Change) -> None:
        old, new = _only(change)
        assert old["version"] == 1
        assert new == {**old, "version": 2, "beats_global_rules": ["DP-1"]}

    harness.check_changes(
        harness.diff(before, harness.snapshot(profile.home)),
        [
            Allowance(
                DISPLAYS,
                frozenset({"modified"}),
                "the first opt-in moves the file to version 2",
                gained_one_opt_in,
            ),
            Allowance(
                DISPLAYS_BACKUP,
                frozenset({"created"}),
                "guard 2: the bytes before the bump",
                _released_bytes(released, mode),
            ),
        ],
    )

    # The fixture is mirrored: assignments, and the opt-in, stay dormant.
    assert "already current" in _write_config(capsys)
    assert not sidecar.exists()

    config.update({"display_mode": "independent", "theme_source_connector": "DP-1"})
    _write_config(capsys)
    with_opt_in = runtime.read_bytes()
    assert tomllib.loads(sidecar.read_text())["displays"] == [
        {"connector": "DP-1", "beats_global_rules": True}
    ]
    assert b"beats_global_rules" not in with_opt_in
    assert _schema(profile) <= runtime_config.BATTERY_SCHEMA_VERSION

    displays.Store.open().set_beats_global_rules("DP-1", False)
    _write_config(capsys)
    assert runtime.read_bytes() == with_opt_in, "runtime.toml never carried the opt-in"
    assert not sidecar.exists()
    assert read_json(target)["version"] == 2
    assert (profile.app_state / "displays.json.v1-backup").read_bytes() == released


def test_a_v0_1_4_service_never_blocks_saving_them(
    golden: Golden, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = golden.profile
    golden.runtime.supported_override_schemas = None
    identifier = playlist_ids(profile)[0]

    playlists.Store.open().set_rotation(identifier, shuffle=True)
    displays.Store.open().set_beats_global_rules("DP-1", True)
    _write_config(capsys)

    assert (profile.app_state / runtime_config.OVERRIDES_FILENAME).is_file()
    assert not runtime_config.runtime_applies_overrides(golden.runtime.status())
    golden.runtime.supported_override_schemas = [1]
    assert runtime_config.runtime_applies_overrides(golden.runtime.status())


def test_idle_with_overrides_in_use_writes_only_the_whitelist(
    golden: Golden, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = golden.profile
    ids = playlist_ids(profile)
    playlists.Store.open().set_rotation(ids[0], cycle_interval=60)
    displays.Store.open().set_beats_global_rules("DP-1", True)
    _write_config(capsys)
    sidecar = profile.app_state / runtime_config.OVERRIDES_FILENAME
    overrides = sidecar.read_bytes()
    backups = sorted(path.name for path in profile.app_state.glob("*-backup"))
    assert backups == ["displays.json.v1-backup", "playlists.json.v1-backup"]

    before = harness.snapshot(profile.home)
    first = harness.run_idle()
    after_first = harness.snapshot(profile.home)
    harness.check_changes(harness.diff(before, after_first), first_start_writes(profile, None))
    assert first.newer_version_files == ()
    assert first.gui_compile in ("changed", "unchanged"), first.gui_compile
    assert first.service_prepare == 0

    second = harness.run_idle()
    harness.check_changes(harness.diff(after_first, harness.snapshot(profile.home)), ())
    assert second == first
    assert golden.processes == []
    assert sorted(path.name for path in profile.app_state.glob("*-backup")) == backups
    assert sidecar.read_bytes() == overrides
    assert _schema(profile) <= runtime_config.BATTERY_SCHEMA_VERSION


NEWER_OVERRIDES: Final = (
    "# Written by a later release.\n"
    "schema_version = 2\n"
    "[[playlists]]\n"
    'id = "79e68864b167b9d6"\n'
    'weighting = "recent"\n'
    "[[wallpapers]]\n"
    'id = "x"\n'
)


def _newer_overrides_beside_runtime(profile: Profile) -> Path:
    sidecar = profile.app_state / runtime_config.OVERRIDES_FILENAME
    sidecar.write_text(NEWER_OVERRIDES, encoding="utf-8")
    return sidecar


def test_a_newer_overrides_file_never_stops_the_service_start_and_is_left_alone(
    golden: Golden,
) -> None:
    """After a rollback from a later release, this build's unit still starts."""
    profile = golden.profile
    sidecar = _newer_overrides_beside_runtime(profile)
    before = harness.snapshot(profile.home)

    assert cli.main(["--service-startup-prepare"]) == 0
    second = cli.main(["--service-startup-prepare"])

    harness.check_changes(
        harness.diff(before, harness.snapshot(profile.home)), first_start_writes(profile, None)
    )
    assert second == 0
    assert sidecar.read_text(encoding="utf-8") == NEWER_OVERRIDES
    assert _schema(profile) <= runtime_config.BATTERY_SCHEMA_VERSION


def test_the_service_check_passes_beside_a_newer_overrides_file(
    golden: Golden, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit's second preflight, this build's own loader, starts without it."""
    binary = os.environ.get("WALL_IN_ONE_SERVICE_BINARY", "")
    if not binary:
        pytest.skip("set WALL_IN_ONE_SERVICE_BINARY to run this build's --check-config")
    profile = golden.profile
    sidecar = _newer_overrides_beside_runtime(profile)
    assert cli.main(["--service-startup-prepare"]) == 0
    monkeypatch.setattr(subprocess, "Popen", sandbox.REAL_POPEN)

    checked = subprocess.run(
        [binary, "--config", str(profile.app_state / "runtime.toml"), "--check-config"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert checked.returncode == 0, checked.stderr
    assert "not applied: unsupported schema_version 2" in checked.stderr
    assert sidecar.read_text(encoding="utf-8") == NEWER_OVERRIDES
