"""An older build edits what this build wrote; this build reads it back.

Opt-in: ``-m downgrade`` with ``WIO_OLD_SRC`` pointing at an older checkout's
``src`` (``tools/golden-downgrade.sh`` sets it up). The old build runs in a
child process (``downgrade_driver.py``) against the same sandbox, with a fake
runtime answering on a real socket, as the old service would after a rollback.
"""

from __future__ import annotations

import json
import os
import re
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
    pairing_item,
    playlist_ids,
    read_json,
    write_json,
)
from wall_in_one import config, paths
from wall_in_one.library import displays, favourites, pairings, playlists, schedules

pytestmark = pytest.mark.downgrade

OLD_SOURCE_ENV: Final = "WIO_OLD_SRC"
GUARD: Final = "needs r1-store-guard"
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
    version: tuple[int, ...]
    text: str

    @property
    def has_guard(self) -> bool:
        # Release 1 (0.1.5) is the first build meant to carry the forward-
        # compatibility guard. Every build before it narrows newer files.
        return self.version >= (0, 1, 5)


@pytest.fixture(scope="session")
def old_build(tmp_path_factory: pytest.TempPathFactory) -> OldBuild:
    root = tmp_path_factory.mktemp("old-build")
    probe = Profile(root, root, root / "home", harness.FIXTURE_HOME, root / "run")
    for path in probe.environment().values():
        Path(path).mkdir(parents=True, exist_ok=True)
    text = str(_run_old(probe, "version")["version"])
    return OldBuild(tuple(int(part) for part in re.findall(r"\d+", text)[:3]), text)


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


def _guard_expected(request: pytest.FixtureRequest, old: OldBuild) -> None:
    if not old.has_guard:
        request.applymarker(
            pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason=f"{GUARD}: old build {old.text} predates the forward-compatibility guard",
            )
        )


def _decorate(document: dict[str, Any]) -> set[str]:
    """Add what a newer build would: a top-level key and a key on every record."""
    document[UNKNOWN_TOP] = {"written-by": "a newer build"}
    keys: set[str] = set()
    for value in document.values():
        if isinstance(value, list):
            for record in value:
                if isinstance(record, dict):
                    record[UNKNOWN_RECORD] = "kept"
                    keys.add(str(record.get("id", record.get("identity"))))
    return keys


@pytest.mark.parametrize("filename", DOWNGRADE_FILES)
def test_downgrade_old_build_keeps_unknown_keys(
    request: pytest.FixtureRequest, downgrade: tuple[Golden, OldBuild], filename: str
) -> None:
    """What Release 2 will add (new keys, same version) survives an old build's edit."""
    golden, old = downgrade
    _guard_expected(request, old)
    target = golden.profile.app_state / filename
    document = read_json(target)
    decorated = _decorate(document)
    write_json(target, document)

    report = _run_old(golden.profile, "edit", filename)

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
    assert kept == len(decorated), f"the old build dropped {len(decorated) - kept} records"
    assert not lost, f"old build {old.text} silently dropped {len(lost)} unknown key(s): {lost[:4]}"
    assert broken_copies(target) == [], report


@pytest.mark.parametrize("filename", DOWNGRADE_FILES)
def test_downgrade_old_build_does_not_rewrite_a_newer_version(
    request: pytest.FixtureRequest, downgrade: tuple[Golden, OldBuild], filename: str
) -> None:
    """A Release-2 version bump: the old build must refuse, not rewrite it as its own."""
    golden, old = downgrade
    _guard_expected(request, old)
    target = golden.profile.app_state / filename
    document = read_json(target)
    document["version"] = int(document["version"]) + 1
    original = write_json(target, document)

    report = _run_old(golden.profile, "edit", filename)

    after = target.read_bytes()
    broken = [path.name for path in broken_copies(target)]
    assert after == original and not broken, (
        f"old build {old.text} accepted the edit (errors: {report['errors']}), moved the "
        f"version-{document['version']} file aside as {broken} and rewrote it as version "
        f"{json.loads(after).get('version')}"
    )
