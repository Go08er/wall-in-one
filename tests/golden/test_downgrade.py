"""v0.1.4 edits what this build wrote; this build reads it back.

There is no 0.1.5. People go from 0.1.4 straight to 0.2.0, so v0.1.4
(``dbfbaa0``) is the only release anyone rolls back to, and it has no
forward-compatibility guard. These tests pin what it does with this build's
files after a rollback, which is what docs/updating.md tells people to expect.

Opt-in: ``-m downgrade`` with ``WIO_OLD_SRC`` pointing at a v0.1.4 checkout's
``src`` (``tools/golden-downgrade.sh`` sets it up); any other build fails the
session instead of being half-tested. ``checks.golden-downgrade`` runs it
against the flake's ``wio-v0-1-4`` input and fails on any skip. v0.1.4 runs in a child process
(``downgrade_driver.py``) against the same sandbox, with a fake runtime
answering on a real socket, as its service would after a rollback.

* Same-version files lose nothing with v0.1.4's edits. ``settings.toml`` as
  this build writes it, every value away from its default, is one v0.1.4's
  strict loader reads back exactly and its service unit compiles from; with
  one key more, v0.1.4 cannot use the file at all.
* What a newer format adds, unknown keys or a version bump, v0.1.4 narrows on
  its next edit of that file: keys it does not model are dropped, and a newer
  version is moved aside as ``.broken`` and rewritten as its own.
* Rule names are 0.2.0's first real bump (schedules.json version 3). An
  unnamed edit writes the same bytes in both builds, so it stays safe; a
  named schedule is narrowed by v0.1.4's next schedule edit.
* Per-playlist rotation and the display opt-in bump playlists.json and
  displays.json to version 2 and add runtime-overrides.toml beside an
  unchanged runtime.toml. v0.1.4's service unit still starts on that
  runtime.toml, but v0.1.4 publishes nothing while a bumped store is still at
  its new version, and narrows each store on its next edit.
* Copying a ``.broken`` file back by hand on this build restores what was
  narrowed, as docs/updating.md describes.

What this build does with a file from a release newer than itself (0.2.0
opening a 0.3.0 file read-only) needs no old build: that is
``test_forward_compat``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, fields, replace
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
    runtime_document,
    write_json,
)
from wall_in_one import cli, config, paths, runtime_config
from wall_in_one.library import displays, favourites, pairings, playlists, schedules
from wall_in_one.session import QUICK_CHOICE_ID
from wall_in_one.theme.noctalia import ALL_SCHEMES
from wall_in_one.wallpaper import renderer, scenes

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
#: v0.1.4's ``__version__``. The unreleased interim commits of 0.2.0 still
#: say the same, so the formats and the missing store guard identify it too.
V0_1_4_VERSION: Final = "0.1.4"
#: The newest version of each store file v0.1.4 reads and writes:
#: ``FORMAT_VERSION`` in each ``src/wall_in_one/library`` module at tag v0.1.4.
V0_1_4_FORMATS: Final = {
    "playlists.json": 1,
    "schedules.json": 2,
    "pairings.json": 2,
    "displays.json": 1,
    "favourites.json": 1,
}

#: The exit status (``EX_CONFIG``) of v0.1.4's ``--service-startup-prepare``
#: when its startup check refuses the profile; systemd then never starts the
#: service.
V0_1_4_EXIT_CONFIG: Final = 78


def _old_source() -> Path:
    raw = os.environ.get(OLD_SOURCE_ENV)
    if not raw:
        pytest.skip(f"set {OLD_SOURCE_ENV} to a v0.1.4 checkout's src (tools/golden-downgrade.sh)")
    source = Path(raw).resolve()
    if not (source / "wall_in_one" / "__init__.py").is_file():
        pytest.fail(f"{OLD_SOURCE_ENV}={source} is not a wall-in-one src directory")
    return source


def _run_old(environment_for: Profile, action: str, *arguments: str) -> dict[str, Any]:
    """Run the driver under v0.1.4, sealed in ``environment_for``."""
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


@pytest.fixture(scope="session")
def old_build(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Check once that ``WIO_OLD_SRC`` is v0.1.4, the only rollback target."""
    root = tmp_path_factory.mktemp("old-build")
    probe = Profile(root, root, root / "home", harness.FIXTURE_HOME, root / "run")
    for path in probe.environment().values():
        Path(path).mkdir(parents=True, exist_ok=True)
    report = _run_old(probe, "version")
    found = {
        "version": report["version"],
        "formats": report["formats"],
        "store_guard": report["store_guard"],
    }
    expected = {"version": V0_1_4_VERSION, "formats": V0_1_4_FORMATS, "store_guard": False}
    if found != expected:
        pytest.fail(f"{OLD_SOURCE_ENV} must be v0.1.4 (dbfbaa0), the only rollback target: {found}")
    return str(report["version"])


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
    old_build: str, tmp_path: Path, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Golden]:
    """A golden profile whose files this build has just edited.

    v0.1.4 runs in a child process, so the sandbox's process guard is lifted
    and the fake runtime answers on a real socket: after a package rollback
    v0.1.4's service is what is running, and it supports schema 5.
    """
    golden = sandbox.enter(harness.FIXTURE, tmp_path / "sandbox", runtime_dir, monkeypatch)
    _current_build_edits(golden.profile)
    monkeypatch.setattr(subprocess, "Popen", sandbox.REAL_POPEN)
    with harness.serve(golden.runtime, paths.runtime_socket_path()):
        yield golden


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


def test_downgrade_v0_1_4_edits_lose_nothing(downgrade: Golden) -> None:
    """Same-version files: v0.1.4's edits keep everything this build wrote."""
    profile = downgrade.profile
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


# -- settings.toml: every value this build writes, read by v0.1.4 -----------------------


def _other(choices: tuple[str, ...], default: str) -> str:
    return next(choice for choice in reversed(choices) if choice != default)


def _every_setting_away_from_its_default(profile: Profile) -> config.Settings:
    """Change every setting through this build's own writer; return what it saved.

    Without the Workshop scan the fixture's Quick choice, one Workshop video,
    has nothing left to play and no build would compile it; a picture keeps
    it playable, so the start below is a real compile.
    """
    elsewhere = profile.home / "Pictures" / "Elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    pictures = sorted(profile.home.glob("Pictures/Wallpapers/*.png"))
    playlists.Store.open().add(QUICK_CHOICE_ID, pictures[0])
    defaults = config.Settings()
    other_default = next(
        playlist["id"]
        for playlist in read_json(profile.app_state / "playlists.json")["playlists"]
        if playlist["entries"]
        and playlist["id"] not in (QUICK_CHOICE_ID, config.load().active_playlist)
    )
    written = config.update(
        {
            "opacity": 0.45,
            "preview_scheme": _other(ALL_SCHEMES, defaults.preview_scheme),
            "follow_noctalia_palette": False,
            "cycle_interval": 4321,
            "cycle_enabled": True,
            "shuffle": True,
            "dynamics_enabled": False,
            "stop_animations_on_battery": True,
            "video_muted": False,
            "video_volume": 37,
            "video_when_hidden": _other(renderer.WHEN_HIDDEN_CHOICES, defaults.video_when_hidden),
            "video_interpolation": _other(
                renderer.INTERPOLATION_CHOICES, defaults.video_interpolation
            ),
            "video_hardware_decode": False,
            "scene_fps": 24,
            "scene_scaling": _other(scenes.SCALING_CHOICES, defaults.scene_scaling),
            "scene_clamp": _other(scenes.CLAMP_CHOICES, defaults.scene_clamp),
            "display_mode": config.DISPLAY_MODE_INDEPENDENT,
            "theme_source_connector": "DP-1",
            "output": "DP-1",
            "own_scene_renderer": False,
            "scan_workshop": False,
            "active_playlist": other_default,
            "cycle_favourites_only": True,
            "roots": (*config.load().roots, elsewhere),
        }
    )
    # A field added later fails here until this test sets it as well.
    for field in fields(config.Settings):
        assert getattr(written, field.name) != getattr(defaults, field.name), field.name
    return written


def _as_reported(settings: config.Settings) -> dict[str, Any]:
    """The values the driver reports for a file, from this build's reading of it."""
    values: dict[str, Any] = {
        field.name: getattr(settings, field.name) for field in fields(settings)
    }
    values["roots"] = [str(root) for root in settings.roots]
    return values


def _variants(written: config.Settings, folder: Path) -> list[Path]:
    """One file per value of every enumerated setting and every range's ends.

    Saved with this build's writer, beside the profile rather than over it.
    """
    values: dict[str, tuple[Any, ...]] = {
        "preview_scheme": ALL_SCHEMES,
        "video_when_hidden": renderer.WHEN_HIDDEN_CHOICES,
        "video_interpolation": renderer.INTERPOLATION_CHOICES,
        "scene_scaling": scenes.SCALING_CHOICES,
        "scene_clamp": scenes.CLAMP_CHOICES,
        "display_mode": config.DISPLAY_MODES,
        "opacity": (config.MIN_OPACITY, 1.0),
        "cycle_interval": (5, 24 * 60 * 60),
        "video_volume": (0, renderer.MAX_VOLUME),
        "scene_fps": (scenes.MIN_FPS, scenes.MAX_FPS),
        "stop_animations_on_battery": (False, True),
    }
    folder.mkdir()
    made = []
    for key, choices in values.items():
        for index, value in enumerate(choices):
            made.append(config.save(replace(written, **{key: value}), folder / f"{key}-{index}"))
    return made


def test_downgrade_v0_1_4_reads_every_setting_this_build_writes(downgrade: Golden) -> None:
    """v0.1.4's strict loader accepts every settings.toml this build can write.

    0.1.4 refuses a key it does not know, and its service then does not
    start. This build writes the same keys (``tests/test_settings_v0_1_4_keys``
    pins that without an old build); here v0.1.4 itself reads the file with
    every setting away from its default, plus one file per enumerated value
    and range end, and must read back exactly what this build saved. Its
    service unit's start then compiles for real from those settings.
    """
    profile = downgrade.profile
    written = _every_setting_away_from_its_default(profile)
    variants = _variants(written, profile.root / "settings-variants")
    target = paths.settings_path()

    read = _run_old(profile, "settings", str(target), *map(str, variants))["settings"]

    for path in (target, *variants):
        assert "error" not in read[str(path)], read[str(path)]
        assert read[str(path)]["settings"] == _as_reported(config.load_strict(path)), path.name
    assert read[str(target)]["settings"] == _as_reported(written)

    prepared = _run_old(profile, "prepare")["prepare"]

    assert prepared["status"] == 0, prepared
    assert "could not be compiled" not in prepared["stderr"], prepared["stderr"]
    assert prepared["stdout"].startswith("wrote: "), prepared
    assert runtime_document(profile)["settings"]["cycle_interval_seconds"] == 4321
    assert target.read_bytes() == written.to_toml().encode()


#: What marks a profile whose deployed upgrade (from the old schema-2 app) has
#: completed: v0.1.4's startup then never reads settings.toml to classify it.
UPGRADE_MARKERS: Final = ("deployed-upgrade-v1.json", "deployed-capture-adoption-v1.json")


@pytest.mark.parametrize("profile_kind", ["upgraded", "never-upgraded"])
def test_downgrade_v0_1_4_cannot_use_settings_with_a_key_it_does_not_know(
    downgrade: Golden, profile_kind: str
) -> None:
    """Why the pin above matters: v0.1.4's service cannot use such a file.

    Its strict loader refuses the key. On a profile that never went through
    the deployed upgrade, its startup check classifies the profile as corrupt
    and the unit exits 78, so the wallpaper service does not start. On one
    that did (like the fixture), the start is softened to the last
    runtime.toml and nothing new is ever published. Either way, no 0.2.0
    writer may add a key to settings.toml.
    """
    profile = downgrade.profile
    if profile_kind == "never-upgraded":
        for name in UPGRADE_MARKERS:
            (profile.app_state / name).unlink()
    control = _run_old(profile, "prepare")["prepare"]
    assert (control["status"], control["stdout"][:7]) == (0, "wrote: "), control
    target = paths.settings_path()
    target.write_bytes(target.read_bytes() + b"ui_glass_frost = 0.3\n")
    runtime = profile.app_state / "runtime.toml"
    published = runtime.read_bytes()

    read = _run_old(profile, "settings", str(target))["settings"][str(target)]
    prepared = _run_old(profile, "prepare")["prepare"]

    assert "unknown setting(s): ui_glass_frost" in read.get("error", ""), read
    if profile_kind == "never-upgraded":
        assert prepared["status"] == V0_1_4_EXIT_CONFIG, prepared
    else:
        assert prepared["status"] == 0, prepared
        assert "could not be compiled" in prepared["stderr"], prepared
    assert "ui_glass_frost" in prepared["stderr"], prepared
    assert runtime.read_bytes() == published


# -- what a newer format adds, edited by v0.1.4 ------------------------------------------


def _newer_keys(target: Path) -> set[str]:
    """Write what a newer format adds to ``target``; return the marked records."""
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
    """Make ``target`` one version newer than v0.1.4 reads and writes."""
    document = read_json(target)
    document["version"] = V0_1_4_FORMATS[target.name] + 1
    return write_json(target, document), int(document["version"])


@pytest.mark.parametrize("filename", DOWNGRADE_FILES)
def test_downgrade_v0_1_4_drops_unknown_keys(downgrade: Golden, filename: str) -> None:
    """v0.1.4 accepts the edit and silently drops every key it does not model.

    Pinned, not merely expected to fail: this is the narrowing that makes a
    rollback to v0.1.4 lossy for anything a newer format adds.
    """
    target = downgrade.profile.app_state / filename
    decorated = _newer_keys(target)

    report = _run_old(downgrade.profile, "edit", filename)

    lost, kept = _unknown_keys_left(target, decorated)
    assert report["errors"] == {}, report["errors"]
    assert kept == len(decorated), "records themselves are kept"
    expected = [UNKNOWN_TOP, *(f"{UNKNOWN_RECORD} on {key}" for key in decorated)]
    assert sorted(lost) == sorted(expected), lost
    assert broken_copies(target) == [], "nothing is kept aside: the keys are simply gone"


@pytest.mark.parametrize("filename", DOWNGRADE_FILES)
def test_downgrade_v0_1_4_rewrites_a_newer_version(downgrade: Golden, filename: str) -> None:
    """v0.1.4 moves a newer file aside as ``.broken`` and rewrites it as its own.

    The original survives only in the ``.broken`` copy; the live file is
    narrowed to the old version, and the edit reports success.
    """
    target = downgrade.profile.app_state / filename
    original, version = _bump_version(target)

    report = _run_old(downgrade.profile, "edit", filename)

    assert report["errors"] == {}, report["errors"]
    (broken,) = broken_copies(target)
    assert broken.read_bytes() == original
    assert read_json(target)["version"] == version - 1


# -- rule names: schedules.json version 3 ------------------------------------------------


def test_downgrade_an_unnamed_schedule_edit_writes_the_same_bytes_in_both_builds(
    downgrade: Golden,
) -> None:
    """Guard 1: without a name, this build's file is v0.1.4's, byte for byte."""
    target = downgrade.profile.app_state / "schedules.json"
    start = target.read_bytes()
    assert read_json(target)["version"] == 2, "this build's unnamed edits must stay version 2"

    report = _run_old(downgrade.profile, "same-schedule-edit")
    theirs = target.read_bytes()
    target.write_bytes(start)
    ours = downgrade_driver.same_schedule_edit()

    assert report["errors"] == {}
    assert report["touched"] == {"schedules.json": ours}
    assert target.read_bytes() == theirs
    assert read_json(target)["version"] == 2
    assert sorted(downgrade.profile.app_state.glob("*-backup")) == []


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


def test_downgrade_v0_1_4_narrows_a_named_schedule(downgrade: Golden) -> None:
    """v0.1.4 cannot safely edit this build's version 3: pinned, not merely expected.

    While the file is version 3, v0.1.4 publishes nothing: its compile
    refuses the version it does not know. Its next schedule edit moves the
    file aside as ``.broken`` and rewrites it as version 2 without the names,
    reporting success. Nothing is lost outright -- the names are in
    ``.broken`` and the released bytes in ``.v2-backup`` -- but the live
    schedule loses its names, and re-upgrading does not bring them back.
    Back on this build, naming again bumps again and the first backup is
    never overwritten.
    """
    profile = downgrade.profile
    target = profile.app_state / "schedules.json"
    backup = target.with_name("schedules.json.v2-backup")
    released, named = _name_a_rule(profile)

    compiled_while_named = _run_old(profile, "compile")["compile"]
    report = _run_old(profile, "edit", "schedules.json")

    assert compiled_while_named.startswith("refused:"), compiled_while_named
    assert "schedules.json has unsupported version 3" in compiled_while_named
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
    downgrade: Golden, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rollback keeps the wallpaper running: v0.1.4's unit starts cleanly.

    v0.1.4's ``--service-startup-prepare`` refuses to compile from the two
    version-2 stores, exits 0 and leaves runtime.toml as this build wrote it
    -- byte for byte the compile with neither feature in use, at a schema its
    service loads -- and never touches ``runtime-overrides.toml``, which its
    service never opens. Until a bumped store is narrowed, no v0.1.4 compile
    publishes anything. Back on this build, both features still apply.
    """
    profile = downgrade.profile
    in_use = _use_overrides(profile, independent=True)
    runtime = profile.app_state / "runtime.toml"
    sidecar = profile.app_state / runtime_config.OVERRIDES_FILENAME

    prepared = _run_old(profile, "prepare")["prepare"]
    compiled_by_old = _run_old(profile, "compile")["compile"]

    assert prepared["status"] == 0, prepared
    assert "could not be compiled" in prepared["stderr"], prepared["stderr"]
    assert compiled_by_old.startswith("refused:"), compiled_by_old
    for name in OVERRIDE_STORES:
        assert f"{name} has unsupported version 2" in compiled_by_old, compiled_by_old
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


def test_downgrade_v0_1_4_narrows_both_stores(downgrade: Golden) -> None:
    """v0.1.4 cannot safely edit this build's version 2: pinned, not merely expected.

    Its next edit moves each version-2 file aside as ``.broken`` and rewrites
    it as version 1 without the new fields, reporting success; it then
    compiles runtime.toml from what is left. The wallpaper keeps running
    throughout. Nothing is lost outright (the fields are in ``.broken``, the
    released bytes in ``.v1-backup``), but the settings stop applying, and
    re-upgrading does not bring them back. The overrides file it never knew
    about stays until this build's next compile finds nothing in use and
    removes it; using a field again then bumps again and never overwrites
    the first backup.
    """
    profile = downgrade.profile
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


# -- getting narrowed data back by hand ----------------------------------------------------


def test_downgrade_copying_the_broken_files_back_restores_what_v0_1_4_narrowed(
    downgrade: Golden, capsys: pytest.CaptureFixture[str]
) -> None:
    """docs/updating.md's recovery, on this build after re-upgrading.

    Each ``.broken`` file is this build's file as it was when v0.1.4 first
    edited it. Copied over the live file (with the app closed), this build
    reads it with no fault, the names, rotation and opt-in are back, and the
    next compile publishes the overrides again. What v0.1.4 changed in that
    file is not in the copy; the ``.v<old>-backup`` files are never needed.
    """
    profile = downgrade.profile
    state = profile.app_state
    sidecar = state / runtime_config.OVERRIDES_FILENAME
    _name_a_rule(profile)
    in_use = _use_overrides(profile, independent=False)
    backups = {path.name: path.read_bytes() for path in state.glob("*-backup")}
    bumped = ("schedules.json", *OVERRIDE_STORES)
    assert _run_old(profile, "edit", *bumped)["errors"] == {}
    assert cli.main(["--write-config"]) == 0
    assert not sidecar.exists(), "re-upgraded, nothing is in use any more"

    for name in bumped:
        (broken,) = broken_copies(state / name)
        (state / name).write_bytes(broken.read_bytes())

    for store in (schedules, playlists, displays):
        assert store.Store.open().fault is None, store.__name__
    assert any(rule.name == "Frog day" for rule in schedules.Store.open().rules)
    assert any(playlist.has_rotation_override for playlist in playlists.Store.open().all())
    assert displays.Store.open().beats_global_rules("DP-1")
    assert not any(
        playlist.name == downgrade_driver.OLD_PLAYLIST for playlist in playlists.Store.open().all()
    ), "v0.1.4's own edit of the file is not in the .broken copy"
    capsys.readouterr()
    assert cli.main(["--write-config"]) == 0
    assert sidecar.read_bytes() == in_use.overrides
    assert {path.name: path.read_bytes() for path in state.glob("*-backup")} == backups
