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

from tests.golden import harness, sandbox
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
from wall_in_one import config, paths
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


@pytest.fixture(scope="session")
def old_build(tmp_path_factory: pytest.TempPathFactory) -> OldBuild:
    root = tmp_path_factory.mktemp("old-build")
    probe = Profile(root, root, root / "home", harness.FIXTURE_HOME, root / "run")
    for path in probe.environment().values():
        Path(path).mkdir(parents=True, exist_ok=True)
    report = _run_old(probe, "version")
    return OldBuild(str(report["version"]), bool(report["has_guard"]))


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
