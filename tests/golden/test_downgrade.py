"""An older build edits what this build wrote; this build reads it back.

Opt-in: ``-m downgrade`` with ``WIO_OLD_SRC`` pointing at an older checkout's
``src`` (``tools/golden-downgrade.sh`` sets it up). The old build runs in a
child process (``downgrade_driver.py``) against the same sandbox, with a fake
runtime answering on a real socket, as the old service would after a rollback.

Same-version files must lose nothing with any old build. For what Release 2
will write -- unknown keys, a version bump -- the outcome depends on the old
build: one with Release 1's guard keeps or refuses them; one without it
(v0.1.4) narrows them, which the ``pre_guard`` tests pin down as the reason
Release 1 must be installed before anything writes newer files.

Rule names are the first real bump (schedules.json version 3). An unnamed
edit writes the same bytes in every build, so it stays safe everywhere; a
named schedule is refused read-only by Release 1 and narrowed by v0.1.4,
which is what backs the release note "install 0.1.5 before 0.2.0".

Per-playlist rotation and the display opt-in bump playlists.json and
displays.json to version 2 and add runtime-overrides.toml beside an unchanged
runtime.toml. Every older build's service unit still starts on that
runtime.toml; Release 1 refuses both stores read-only, and v0.1.4 narrows
them on its next edit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from tests.golden import downgrade_driver, harness, sandbox
from tests.golden.harness import Profile
from tests.golden.sandbox import (
    UNKNOWN_RECORD,
    UNKNOWN_TOP,
    Golden,
    broken_copies,
    decorate,
    pairing_item,
    playlist_ids,
    read_json,
    write_json,
)
from wall_in_one import cli, config, paths, runtime_config
from wall_in_one.library import displays, favourites, pairings, playlists, schedules

pytestmark = pytest.mark.downgrade

OLD_SOURCE_ENV: Final = "WIO_OLD_SRC"
DRIVER: Final = Path(__file__).with_name("downgrade_driver.py")
DOWNGRADE_FILES: Final = (
    "playlists.json",
    "schedules.json",
    "pairings.json",
    "displays.json",
    "favourites.json",
)


def _old_source() -> Path:
    raw = os.environ.get(OLD_SOURCE_ENV)
    if not raw:
        pytest.skip(f"set {OLD_SOURCE_ENV} to an older checkout's src (tools/golden-downgrade.sh)")
    source = Path(raw).resolve()
    if not (source / "wall_in_one" / "__init__.py").is_file():
        pytest.fail(f"{OLD_SOURCE_ENV}={source} is not a wall-in-one src directory")
    return source


def _run_old(environment_for: Profile, action: str, *arguments: str) -> dict[str, Any]:
    """Run the driver under the old build, sealed in ``environment_for``."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("XDG_", "PYTHON", "DBUS_", "WAYLAND_", "NIRI_"))
    }
    environment.update(environment_for.environment())
    environment["PYTHONPATH"] = str(_old_source())
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, str(DRIVER), action, *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"the old-build driver failed:\n{completed.stderr}")
    report = json.loads(completed.stdout)
    if not isinstance(report, dict) or not Path(report["module"]).resolve().is_relative_to(
        _old_source()
    ):
        raise RuntimeError(f"the driver did not run the old build: {completed.stdout}")
    return report


@dataclass(frozen=True, slots=True)
class OldBuild:
    text: str
    #: Whether the old build has Release 1's forward-compatibility guard,
    #: by capability rather than by version number.
    has_guard: bool
    #: The newest schedules.json version the old build understands.
    schedules_format: int
    #: The newest playlists.json and displays.json versions it understands.
    playlists_format: int
    displays_format: int


@pytest.fixture(scope="session")
def old_build(tmp_path_factory: pytest.TempPathFactory) -> OldBuild:
    root = tmp_path_factory.mktemp("old-build")
    probe = Profile(root, root, root / "home", harness.FIXTURE_HOME, root / "run")
    for path in probe.environment().values():
        Path(path).mkdir(parents=True, exist_ok=True)
    report = _run_old(probe, "version")
    return OldBuild(
        str(report["version"]),
        bool(report["has_guard"]),
        int(report["schedules_format"]),
        int(report["playlists_format"]),
        int(report["displays_format"]),
    )


def _require(old: OldBuild, *, guard: bool) -> None:
    if old.has_guard != guard:
        state = "has" if old.has_guard else "lacks"
        pytest.skip(f"old build {old.text} {state} the forward-compatibility guard")


def _current_build_edits(profile: Profile) -> None:
    """One edit per store by *this* build, so every file is one it wrote."""
    ids = playlist_ids(profile)
    playlists.Store.open().create("Written by the current build")
    schedules.Store.open().add(ids[0], start="07:00", end="08:00")
    pairings.Store.open().choose_palette(
        pairing_item(profile), pairings.PalettePolicy(kind="adaptive", name="m3-content")
    )
    displays.Store.open().assign("DP-5", ids[1])
    favourites.Store.open().add(sorted(profile.home.glob("Pictures/Wallpapers/*.png"))[-1])
    config.update({"cycle_interval": 1200})


@pytest.fixture
def downgrade(
    old_build: OldBuild, tmp_path: Path, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Golden, OldBuild]]:
    """A golden profile whose files this build has just edited.

    The old build runs in a child process, so the sandbox's process guard is
    lifted and the fake runtime answers on a real socket: after a package
    rollback the old service is what is running, and it supports schema 5.
    """
    golden = sandbox.enter(harness.FIXTURE, tmp_path / "sandbox", runtime_dir, monkeypatch)
    _current_build_edits(golden.profile)
    monkeypatch.setattr(subprocess, "Popen", sandbox.REAL_POPEN)
    with harness.serve(golden.runtime, paths.runtime_socket_path()):
        yield golden, old_build


def _keyed(profile: Profile) -> dict[str, dict[str, Any]]:
    """Every record this build can read, keyed by its identity."""
    state = profile.app_state
    return {
        "playlists.json": {
            entry["id"]: entry for entry in read_json(state / "playlists.json")["playlists"]
        },
        "schedules.json": {
            rule["id"]: rule for rule in read_json(state / "schedules.json")["rules"]
        },
        "pairings.json": {
            record["identity"]: record for record in read_json(state / "pairings.json")["pairings"]
        },
        "displays.json": dict(read_json(state / "displays.json")["displays"]),
        "favourites.json": {path: True for path in read_json(state / "favourites.json")["paths"]},
        "settings.toml": tomllib.loads(paths.settings_path().read_text()),
    }


def test_downgrade_old_build_edits_lose_nothing(downgrade: tuple[Golden, OldBuild]) -> None:
    """Same-version files: an old build's edits keep everything this build wrote."""
    golden, _old = downgrade
    profile = golden.profile
    written = _keyed(profile)

    report = _run_old(profile, "edit")
    assert report["errors"] == {}, report["errors"]

    after = _keyed(profile)
    for name, records in written.items():
        touched = set(report["touched"].get(name, ()))
        for key, record in records.items():
            assert key in after[name], f"{name}: the old build lost {key!r}"
            if name == "playlists.json" and key in touched:
                kept = [entry["id"] for entry in after[name][key]["entries"]]
                assert [entry["id"] for entry in record["entries"]] == kept[
                    : len(record["entries"])
                ]
            elif key not in touched:
                assert after[name][key] == record, f"{name}: the old build changed {key!r}"
    assert after["settings.toml"]["opacity"] == pytest.approx(0.55)
    assert any(
        entry["name"] == "Written by the old build" for entry in after["playlists.json"].values()
    )
    for store in (playlists, schedules, pairings, displays, favourites):
        assert store.Store.open().fault is None, store.__name__
    config.load_strict()
    assert sorted(profile.app_state.glob("*.broken*")) == []


def _newer_keys(target: Path) -> set[str]:
    """Write what Release 2 will add to ``target``; return the marked records."""
    document = read_json(target)
    decorated = decorate(document)
    write_json(target, document)
    return decorated


def _unknown_keys_left(target: Path, decorated: set[str]) -> tuple[list[str], int]:
    """Which of the marked keys are gone now, and how many marked records remain."""
    after = read_json(target)
    lost = [] if after.get(UNKNOWN_TOP) == {"written-by": "a newer build"} else [UNKNOWN_TOP]
    kept = 0
    for value in after.values():
        if isinstance(value, list):
            for record in value:
                if not isinstance(record, dict):
                    continue
                key = str(record.get("id", record.get("identity")))
                if key in decorated:
                    kept += 1
                    if record.get(UNKNOWN_RECORD) != "kept":
                        lost.append(f"{UNKNOWN_RECORD} on {key}")
    return lost, kept


def _bump_version(target: Path) -> tuple[bytes, int]:
    document = read_json(target)
    document["version"] = int(document["version"]) + 1
    return write_json(target, document), int(document["version"])


@pytest.mark.parametrize("filename", DOWNGRADE_FILES)
def test_downgrade_guarded_build_keeps_unknown_keys(
    downgrade: tuple[Golden, OldBuild], filename: str
) -> None:
    """What Release 2 will add (new keys, same version) survives an old build's edit."""
    golden, old = downgrade
    _require(old, guard=True)
    target = golden.profile.app_state / filename
    decorated = _newer_keys(target)

    report = _run_old(golden.profile, "edit", filename)

    lost, kept = _unknown_keys_left(target, decorated)
    assert report["errors"] == {}, report["errors"]
    assert kept == len(decorated), f"the old build dropped {len(decorated) - kept} records"
    assert not lost, f"old build {old.text} dropped unknown key(s): {lost[:4]}"
    assert broken_copies(target) == [], report


@pytest.mark.parametrize("filename", DOWNGRADE_FILES)
def test_downgrade_guarded_build_refuses_a_newer_version(
    downgrade: tuple[Golden, OldBuild], filename: str
) -> None:
    """A Release-2 version bump: the old build refuses and leaves every byte."""
    golden, old = downgrade
    _require(old, guard=True)
    target = golden.profile.app_state / filename
    original, _version = _bump_version(target)

    report = _run_old(golden.profile, "edit", filename)

    assert target.read_bytes() == original
    assert broken_copies(target) == []
    assert "newer-version" in report["errors"].get(filename, ""), report


@pytest.mark.parametrize("filename", DOWNGRADE_FILES)
def test_downgrade_pre_guard_build_drops_unknown_keys(
    downgrade: tuple[Golden, OldBuild], filename: str
) -> None:
    """v0.1.4 accepts the edit and silently drops every key it does not model.

    Pinned, not merely expected to fail: this is the narrowing that makes a
    rollback from Release 2 to v0.1.4 lossy, and why Release 1 ships first.
    """
    golden, old = downgrade
    _require(old, guard=False)
    target = golden.profile.app_state / filename
    decorated = _newer_keys(target)

    report = _run_old(golden.profile, "edit", filename)

    lost, kept = _unknown_keys_left(target, decorated)
    assert report["errors"] == {}, report["errors"]
    assert kept == len(decorated), "records themselves are kept"
    expected = [UNKNOWN_TOP, *(f"{UNKNOWN_RECORD} on {key}" for key in decorated)]
    assert sorted(lost) == sorted(expected), lost
    assert broken_copies(target) == [], "nothing is kept aside: the keys are simply gone"


@pytest.mark.parametrize("filename", DOWNGRADE_FILES)
def test_downgrade_pre_guard_build_rewrites_a_newer_version(
    downgrade: tuple[Golden, OldBuild], filename: str
) -> None:
    """v0.1.4 moves a newer file aside as ``.broken`` and rewrites it as its own.

    The original survives only in the ``.broken`` copy; the live file is
    narrowed to the old version, and the edit reports success.
    """
    golden, old = downgrade
    _require(old, guard=False)
    target = golden.profile.app_state / filename
    original, version = _bump_version(target)

    report = _run_old(golden.profile, "edit", filename)

    assert report["errors"] == {}, report["errors"]
    (broken,) = broken_copies(target)
    assert broken.read_bytes() == original
    assert read_json(target)["version"] == version - 1


# -- rule names: schedules.json version 3 ------------------------------------------------


def _require_before_rule_names(old: OldBuild) -> None:
    if old.schedules_format >= schedules.NAMED_FORMAT_VERSION:
        pytest.skip(f"old build {old.text} already understands named rules")


def test_downgrade_an_unnamed_schedule_edit_writes_the_same_bytes_in_both_builds(
    downgrade: tuple[Golden, OldBuild],
) -> None:
    """Guard 1: without a name, this build's file is the old build's, byte for byte."""
    golden, old = downgrade
    _require_before_rule_names(old)
    target = golden.profile.app_state / "schedules.json"
    start = target.read_bytes()
    assert read_json(target)["version"] == 2, "this build's unnamed edits must stay version 2"

    report = _run_old(golden.profile, "same-schedule-edit")
    theirs = target.read_bytes()
    target.write_bytes(start)
    ours = downgrade_driver.same_schedule_edit()

    assert report["errors"] == {}
    assert report["touched"] == {"schedules.json": ours}
    assert target.read_bytes() == theirs
    assert read_json(target)["version"] == 2
    assert sorted(golden.profile.app_state.glob("*-backup")) == []


def _name_a_rule(profile: Profile) -> tuple[bytes, bytes]:
    """This build names one rule; returns the version-2 bytes and the version-3 ones."""
    target = profile.app_state / "schedules.json"
    released = target.read_bytes()
    rule = read_json(target)["rules"][0]["id"]
    schedules.Store.open().set_name(rule, "Frog day")
    named = target.read_bytes()
    assert read_json(target)["version"] == 3
    assert target.with_name("schedules.json.v2-backup").read_bytes() == released
    return released, named


def test_downgrade_guarded_build_refuses_a_named_schedule(
    downgrade: tuple[Golden, OldBuild],
) -> None:
    """Release 1 opens this build's version 3 read-only and never rewrites it."""
    golden, old = downgrade
    _require(old, guard=True)
    _require_before_rule_names(old)
    target = golden.profile.app_state / "schedules.json"
    released, named = _name_a_rule(golden.profile)

    report = _run_old(golden.profile, "edit", "schedules.json")

    assert "newer-version" in report["errors"].get("schedules.json", ""), report
    assert target.read_bytes() == named
    assert broken_copies(target) == []
    assert target.with_name("schedules.json.v2-backup").read_bytes() == released


def test_downgrade_pre_guard_build_narrows_a_named_schedule(
    downgrade: tuple[Golden, OldBuild],
) -> None:
    """v0.1.4 cannot safely edit this build's version 3: pinned, not merely expected.

    It moves the file aside as ``.broken`` and rewrites it as version 2
    without the names, reporting success. Nothing is lost outright -- the
    names are in ``.broken`` and the released bytes in ``.v2-backup`` -- but
    the live schedule loses its names, which is why 0.2.0's release notes say
    to install 0.1.5 first. Back on this build, naming again bumps again and
    the first backup is never overwritten.
    """
    golden, old = downgrade
    _require(old, guard=False)
    target = golden.profile.app_state / "schedules.json"
    backup = target.with_name("schedules.json.v2-backup")
    released, named = _name_a_rule(golden.profile)

    report = _run_old(golden.profile, "edit", "schedules.json")

    assert report["errors"] == {}, report["errors"]
    (broken,) = broken_copies(target)
    assert broken.read_bytes() == named
    narrowed = read_json(target)
    assert narrowed["version"] == 2
    assert not any("name" in rule for rule in narrowed["rules"])
    assert backup.read_bytes() == released

    schedules.Store.open().set_name(narrowed["rules"][0]["id"], "Frog day again")

    assert read_json(target)["version"] == 3
    assert backup.read_bytes() == released
    assert sorted(path.name for path in target.parent.glob("*-backup")) == [backup.name]


# -- per-playlist rotation and the display opt-in ----------------------------------------

OVERRIDE_STORES: Final = ("playlists.json", "displays.json")


def _require_before_overrides(old: OldBuild) -> None:
    if old.playlists_format >= playlists.ROTATION_VERSION or (
        old.displays_format >= displays.PRECEDENCE_VERSION
    ):
        pytest.skip(f"old build {old.text} already understands per-playlist rotation")


@dataclass(frozen=True, slots=True)
class InUse:
    """This build's files once both features are in use."""

    #: runtime.toml as compiled with neither feature in use.
    cleared_runtime: bytes
    #: Per store: the released version-1 bytes and the version-2 ones.
    stores: dict[str, tuple[bytes, bytes]]
    overrides: bytes


def _use_overrides(profile: Profile, *, independent: bool) -> InUse:
    """Compile with nothing in use, then give a playlist its own rotation and
    opt DP-1 in, and compile again.

    With ``independent`` the displays route on their own, so the opt-in
    reaches the overrides file too; mirrored, it stays dormant in the store.
    """
    runtime = profile.app_state / "runtime.toml"
    sidecar = profile.app_state / runtime_config.OVERRIDES_FILENAME
    if independent:
        config.update({"display_mode": "independent", "theme_source_connector": "DP-1"})
    assert cli.main(["--write-config"]) == 0
    cleared = runtime.read_bytes()
    targets = {name: profile.app_state / name for name in OVERRIDE_STORES}
    released = {name: target.read_bytes() for name, target in targets.items()}
    # A playlist that reaches the wire: an empty one (the current build's own
    # edit made one) is not compiled, so its rotation would go nowhere.
    playable = next(
        playlist["id"]
        for playlist in read_json(targets["playlists.json"])["playlists"]
        if playlist["entries"]
    )
    playlists.Store.open().set_rotation(playable, cycle_interval=120, shuffle=True)
    displays.Store.open().set_beats_global_rules("DP-1", True)
    assert cli.main(["--write-config"]) == 0

    assert runtime.read_bytes() == cleared, "runtime.toml changed for the overrides"
    overrides = tomllib.loads(sidecar.read_text())
    assert overrides["playlists"] == [
        {"id": playable, "cycle_interval_seconds": 120, "shuffle": True}
    ]
    expected_displays = [{"connector": "DP-1", "beats_global_rules": True}] if independent else None
    assert overrides.get("displays") == expected_displays
    stores: dict[str, tuple[bytes, bytes]] = {}
    for name, target in targets.items():
        assert read_json(target)["version"] == 2, name
        assert target.with_name(f"{name}.v1-backup").read_bytes() == released[name], name
        stores[name] = (released[name], target.read_bytes())
    return InUse(cleared, stores, sidecar.read_bytes())


def _runtime_schema(profile: Profile) -> int:
    value = tomllib.loads((profile.app_state / "runtime.toml").read_text())["schema_version"]
    assert isinstance(value, int)
    return value


def test_downgrade_service_start_keeps_runtime_toml_and_the_overrides(
    downgrade: tuple[Golden, OldBuild], capsys: pytest.CaptureFixture[str]
) -> None:
    """A rollback keeps the wallpaper running: the old unit starts cleanly.

    The old build's ``--service-startup-prepare`` succeeds and leaves
    runtime.toml as this build wrote it -- byte for byte the compile with
    neither feature in use, at a schema the old service loads -- and never
    touches ``runtime-overrides.toml``, which its service never opens. Back
    on this build, both features still apply.
    """
    golden, old = downgrade
    _require_before_overrides(old)
    profile = golden.profile
    in_use = _use_overrides(profile, independent=True)
    runtime = profile.app_state / "runtime.toml"
    sidecar = profile.app_state / runtime_config.OVERRIDES_FILENAME

    report = _run_old(profile, "prepare")

    assert report["prepare"] == 0, report
    assert runtime.read_bytes() == in_use.cleared_runtime
    assert _runtime_schema(profile) <= runtime_config.BATTERY_SCHEMA_VERSION
    assert sidecar.read_bytes() == in_use.overrides
    for name, (released, written) in in_use.stores.items():
        target = profile.app_state / name
        assert target.read_bytes() == written, name
        assert broken_copies(target) == [], name
        assert target.with_name(f"{name}.v1-backup").read_bytes() == released, name

    capsys.readouterr()
    assert cli.main(["--write-config"]) == 0
    assert "already current" in capsys.readouterr().out
    assert sidecar.read_bytes() == in_use.overrides
    assert any(playlist.has_rotation_override for playlist in playlists.Store.open().all())
    assert displays.Store.open().beats_global_rules("DP-1")


def test_downgrade_guarded_build_opens_both_stores_read_only(
    downgrade: tuple[Golden, OldBuild],
) -> None:
    """Release 1 refuses edits to both version-2 stores and will not recompile from them."""
    golden, old = downgrade
    _require(old, guard=True)
    _require_before_overrides(old)
    profile = golden.profile
    in_use = _use_overrides(profile, independent=True)
    runtime = profile.app_state / "runtime.toml"
    sidecar = profile.app_state / runtime_config.OVERRIDES_FILENAME

    report = _run_old(profile, "edit", *OVERRIDE_STORES)
    compiled_by_old = _run_old(profile, "compile")["compile"]

    for name, (released, written) in in_use.stores.items():
        target = profile.app_state / name
        assert "newer-version" in report["errors"].get(name, ""), report
        assert target.read_bytes() == written, name
        assert broken_copies(target) == [], name
        assert target.with_name(f"{name}.v1-backup").read_bytes() == released, name
    assert compiled_by_old.startswith("refused:"), compiled_by_old
    assert "newer version" in compiled_by_old
    assert runtime.read_bytes() == in_use.cleared_runtime
    assert sidecar.read_bytes() == in_use.overrides


def test_downgrade_pre_guard_build_narrows_both_stores(
    downgrade: tuple[Golden, OldBuild],
) -> None:
    """v0.1.4 cannot safely edit this build's version 2: pinned, not merely expected.

    Its next edit moves each version-2 file aside as ``.broken`` and rewrites
    it as version 1 without the new fields, reporting success; it then
    compiles runtime.toml from what is left. The wallpaper keeps running
    throughout. Nothing is lost outright (the fields are in ``.broken``, the
    released bytes in ``.v1-backup``), but the settings stop applying, which
    is why 0.2.0's release notes say to install 0.1.5 first. The overrides
    file it never knew about stays until this build's next compile finds
    nothing in use and removes it; using a field again then bumps again and
    never overwrites the first backup.
    """
    golden, old = downgrade
    _require(old, guard=False)
    _require_before_overrides(old)
    profile = golden.profile
    # Mirrored: the old driver's own display edit may name an empty playlist,
    # which only independent routing would refuse to compile.
    in_use = _use_overrides(profile, independent=False)
    runtime = profile.app_state / "runtime.toml"
    sidecar = profile.app_state / runtime_config.OVERRIDES_FILENAME

    report = _run_old(profile, "edit", *OVERRIDE_STORES)
    assert report["errors"] == {}, report["errors"]
    for name, (released, written) in in_use.stores.items():
        target = profile.app_state / name
        (broken,) = broken_copies(target)
        assert broken.read_bytes() == written, name
        narrowed = read_json(target)
        assert narrowed["version"] == 1, name
        assert "beats_global_rules" not in narrowed
        assert not any(
            key in playlist
            for playlist in narrowed.get("playlists", [])
            for key in ("cycle_interval", "shuffle")
        )
        assert target.with_name(f"{name}.v1-backup").read_bytes() == released, name

    assert _run_old(profile, "compile")["compile"] == "changed"
    assert _runtime_schema(profile) <= runtime_config.BATTERY_SCHEMA_VERSION
    assert sidecar.read_bytes() == in_use.overrides, "the old build never knew it"

    assert cli.main(["--write-config"]) == 0
    assert not sidecar.exists()
    assert runtime.read_bytes() != b""

    playlists.Store.open().set_rotation(playlist_ids(profile)[0], shuffle=False)
    target = profile.app_state / "playlists.json"
    assert read_json(target)["version"] == 2
    assert (
        target.with_name("playlists.json.v1-backup").read_bytes()
        == in_use.stores["playlists.json"][0]
    )
