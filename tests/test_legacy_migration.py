from __future__ import annotations

import errno
import json
import multiprocessing
import os
import shutil
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import Any, cast

import pytest

from wall_in_one import cli, config, legacy_migration, paths
from wall_in_one.library import (
    displays,
    favourites,
    manage,
    pairing,
    pairings,
    playlists,
    scan,
    schedules,
    state_file,
)
from wall_in_one.providers import credentials

FIXTURES = Path(__file__).parent / "fixtures" / "legacy"


def _spawn_migration(source: str, barrier: Event, results: Queue[tuple[str, object]]) -> None:
    """Process entry point: wait until both snapshots are ready, then import."""
    if not barrier.wait(timeout=30):
        results.put(("error", "migration test barrier timed out"))
        return
    try:
        outcome = legacy_migration.migrate(source_dir=Path(source))
    except Exception as error:
        results.put(("error", f"{type(error).__name__}: {error}"))
    else:
        results.put(("ok", outcome.changed))


def _install_schema_fixture(schema: int) -> tuple[Path, bytes]:
    legacy = legacy_migration.legacy_data_dir()
    legacy.mkdir(parents=True)
    source = FIXTURES / f"schema-{schema}" / "config.json"
    contents = source.read_bytes()
    (legacy / "config.json").write_bytes(contents)
    return legacy, contents


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def _bundle(
    identifier: str,
    *,
    media: dict[str, object] | None,
    still: dict[str, object],
    theme: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": identifier,
        "label": identifier,
        "media": media,
        "still": still,
        "theme": theme or {"mode": "auto", "source": "wallpaper", "selection": "m3-content"},
        "added_at": "2026-08-20 12:00:00",
        "customized": True,
    }


def _engines(*, fps: int = 30, mute: bool = True) -> dict[str, object]:
    return {
        "layer": "bottom",
        "video": {
            "enabled": True,
            "mute": mute,
            "hardware_decode": True,
            "auto_pause": True,
            "auto_pause_mode": "FULL",
            "options": "",
        },
        "workshop": {
            "enabled": True,
            "fps": fps,
            "volume": 0,
            "silent": True,
            "scaling": "fill",
            "clamp": "border",
            "flags": {},
        },
    }


def _legacy_fixture(tmp_path: Path) -> tuple[Path, dict[Path, bytes], Path, Path, Path]:
    image_root = tmp_path / "pictures"
    video_root = tmp_path / "videos"
    image_root.mkdir()
    video_root.mkdir()
    video = video_root / "rain.mp4"
    video.write_bytes(b"video")
    manual = image_root / "forest.png"
    manual.write_bytes(b"png")

    automatic = image_root / "Wall-in-One" / "Automatic Stills" / "wall-in-one-video-rain.png"
    automatic.parent.mkdir(parents=True)
    automatic.write_bytes(b"legacy automatic still")
    automatic_sidecar = automatic.with_name(automatic.name + pairing.SIDECAR_SUFFIX)
    automatic_sidecar.write_text(
        json.dumps(
            {
                "schema": 1,
                "plugin": "goober/wall-in-one",
                "kind": "automatic-still",
                "path": str(automatic),
                "dynamic_id": f"video:{video}",
                "provider": "ffmpeg",
                "source": str(video),
            }
        ),
        encoding="utf-8",
    )

    video_bundle = _bundle(
        "video-entry",
        media={"kind": "video", "source": str(video)},
        still={"mode": "automatic"},
    )
    static_bundle = _bundle(
        "still-entry",
        media=None,
        still={"mode": "selected", "path": str(manual)},
        theme={"mode": "light", "source": "custom", "selection": "My palette"},
    )
    legacy = legacy_migration.legacy_data_dir()
    legacy.mkdir(parents=True)
    config_document = {
        "schema_version": 5,
        "gestures": {"left": "next", "middle": "pause", "right": "random"},
        "pairings": {
            "profile-video": {**video_bundle, "id": "profile-video"},
            "profile-still": {**static_bundle, "id": "profile-still"},
        },
        "playlists": {
            "moving": {
                "name": "Moving",
                "order": "rotate",
                "interval_seconds": 61,
                "entries": [{**video_bundle, "pairing_id": "profile-video"}],
                "quick_choice": False,
            },
            "quiet": {
                "name": "Quiet",
                "order": "shuffle",
                "interval_seconds": 127,
                "entries": [{**static_bundle, "pairing_id": "profile-still"}],
                "quick_choice": False,
            },
        },
        "outputs": {
            "eDP-1": {
                "fallback_playlist": "moving",
                "quick_choice_playlist": "",
                "schedules": [
                    {
                        "id": "monday-night",
                        "name": "Monday night",
                        "playlist": "quiet",
                        "enabled": True,
                        "weekdays": [0],
                        "months": [8],
                        "start_minute": 22 * 60,
                        "end_minute": 6 * 60,
                        "all_day": False,
                    }
                ],
                "engines": _engines(fps=30),
            },
            "DP-1": {
                "fallback_playlist": "quiet",
                "quick_choice_playlist": "",
                "schedules": [],
                "engines": _engines(fps=45, mute=False),
            },
        },
    }
    config_path = legacy / "config.json"
    config_path.write_text(json.dumps(config_document, indent=2) + "\n", encoding="utf-8")
    runtime_path = legacy / "runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "schema_version": 6,
                "pair_registry": {
                    f"video:{video}": {
                        "dynamic_id": f"video:{video}",
                        "still_path": str(automatic),
                        "still_managed": True,
                        "sidecar_path": str(automatic_sidecar),
                    }
                },
                "pairs": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    noctalia = paths.noctalia_settings_path()
    noctalia.parent.mkdir(parents=True, exist_ok=True)
    noctalia.write_text(
        f'''[plugin_settings."goober/wall-in-one"]
capture_directory = "{image_root}"
video_directory = "{video_root}"
wallhaven_api_key = "abc_123"
color_scheme = "m3-content"
palette_output = "DP-1"
cycle_interval_minutes = 15
cycle_order = "sequential"
cycle_start_on_load = true
''',
        encoding="utf-8",
    )
    watched = {
        path: path.read_bytes()
        for path in (config_path, runtime_path, noctalia, automatic, automatic_sidecar)
    }
    return legacy, watched, video, manual, automatic


@pytest.mark.parametrize(
    ("schema", "playlist_count", "pairing_count", "schedule_count"),
    (
        (1, 0, 0, 0),
        (2, 1, 2, 0),
        (3, 1, 1, 1),
        (4, 1, 1, 0),
        (5, 1, 1, 0),
    ),
)
def test_representative_persisted_fixtures_for_every_supported_schema_import(
    isolated_xdg: Path,
    schema: int,
    playlist_count: int,
    pairing_count: int,
    schedule_count: int,
) -> None:
    legacy, original = _install_schema_fixture(schema)

    outcome = legacy_migration.migrate(source_dir=legacy)

    assert outcome.changed
    assert (legacy / "config.json").read_bytes() == original
    marker = cast(dict[str, object], json.loads(legacy_migration.marker_path().read_bytes()))
    assert marker["legacy_schema"] == schema
    assert len(playlists.Store.open().all()) == playlist_count
    assert len(pairings.Store.open()) == pairing_count
    assert len(schedules.Store.open().rules) == schedule_count
    if schema == 3:
        assert schedules.Store.open().rules[0].months == frozenset(range(1, 13))


def test_repeated_import_is_idempotent_and_does_not_rewrite_current_or_legacy_bytes(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    first = legacy_migration.migrate(source_dir=legacy)
    current_files = tuple(
        path
        for path in (*paths.app_config_dir().iterdir(), *paths.app_state_dir().iterdir())
        if path.is_file()
    )
    current = {path: path.read_bytes() for path in current_files}

    second = legacy_migration.migrate(source_dir=legacy)

    assert first.changed
    assert not second.changed
    assert {path: path.read_bytes() for path in current_files} == current
    assert all(path.read_bytes() == before for path, before in watched.items())


@pytest.mark.parametrize(
    "document",
    (
        {"version": 1, "disposition": "imported"},
        {"version": 1, "disposition": "declined"},
        {"version": True, "disposition": "imported"},
    ),
)
def test_semantically_incomplete_marker_never_suppresses_or_starts_import(
    isolated_xdg: Path,
    document: dict[str, object],
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    marker = legacy_migration.marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    original = (json.dumps(document) + "\n").encode()
    marker.write_bytes(original)

    found = legacy_migration.probe(source_dir=legacy)

    assert found.status == "conflict"
    assert found.needs_decision
    with pytest.raises(legacy_migration.MigrationError):
        legacy_migration.migrate(source_dir=legacy)
    assert marker.read_bytes() == original
    assert not any(path.exists() for path in legacy_migration._authoring_targets())
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_duplicate_marker_keys_fail_closed_without_follow_on_writes(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    marker = legacy_migration.marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"version":1,"version":1,"disposition":"imported"}\n'
    marker.write_bytes(original)

    assert legacy_migration.probe(source_dir=legacy).status == "conflict"
    with pytest.raises(legacy_migration.MigrationError, match="readable JSON"):
        legacy_migration.migrate(source_dir=legacy)
    assert marker.read_bytes() == original
    assert not any(path.exists() for path in legacy_migration._authoring_targets())
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_symbolic_link_legacy_source_directory_is_not_followed(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    alias = isolated_xdg / "legacy-link"
    alias.symlink_to(legacy, target_is_directory=True)

    found = legacy_migration.probe(source_dir=alias)

    assert found.status == "corrupt"
    assert "symbolic link" in found.detail
    with pytest.raises(legacy_migration.MigrationError, match="symbolic link"):
        legacy_migration.migrate(source_dir=alias)
    assert not any(path.exists() for path in legacy_migration._authoring_targets())
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_imported_marker_binds_complete_target_manifest_but_not_mutable_bytes(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)

    legacy_migration.migrate(source_dir=legacy)
    marker = cast(dict[str, Any], json.loads(legacy_migration.marker_path().read_bytes()))
    manifest = cast(dict[str, dict[str, str]], marker["targets"])

    assert set(manifest) == {
        "settings",
        "pairings",
        "playlists",
        "schedules",
        "displays",
        "favourites",
        "report",
        "wallhaven-key",
    }
    assert manifest["settings"]["path"] == str(paths.settings_path())
    assert len(manifest["settings"]["sha256"]) == 64

    # Ordinary authoring replaces target bytes after import, and clearing an
    # imported credential removes its file.  Neither invalidates completion.
    paths.settings_path().write_bytes(paths.settings_path().read_bytes() + b"\n")
    credentials.key_path().unlink()

    assert legacy_migration.probe(source_dir=legacy).status == "imported"
    assert not legacy_migration.migrate(source_dir=legacy).changed
    assert all(path.read_bytes() == before for path, before in watched.items())


@pytest.mark.parametrize("failure", ("missing", "symlink", "wrong-manifest-path"))
def test_imported_marker_requires_safe_core_target_identity_and_presence(
    isolated_xdg: Path,
    failure: str,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    legacy_migration.migrate(source_dir=legacy)
    target = pairings.state_path()
    marker_path = legacy_migration.marker_path()
    before_marker = marker_path.read_bytes()

    if failure == "missing":
        target.unlink()
    elif failure == "symlink":
        target.unlink()
        target.symlink_to(legacy / "config.json")
    else:
        document = cast(dict[str, Any], json.loads(before_marker))
        manifest = cast(dict[str, dict[str, str]], document["targets"])
        manifest["pairings"]["path"] = str(isolated_xdg / "other-pairings.json")
        marker_path.write_text(json.dumps(document), encoding="utf-8")

    found = legacy_migration.probe(source_dir=legacy)

    assert found.status == "conflict"
    with pytest.raises(legacy_migration.MigrationError):
        legacy_migration.migrate(source_dir=legacy)
    if failure != "wrong-manifest-path":
        assert marker_path.read_bytes() == before_marker
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_orphaned_report_is_an_initial_no_overwrite_conflict(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    report = legacy_migration.report_path()
    report.parent.mkdir(parents=True, exist_ok=True)
    original = b"user recovery notes\n"
    report.write_bytes(original)

    found = legacy_migration.probe(source_dir=legacy)

    assert found.status == "conflict"
    assert report in found.conflicts
    with pytest.raises(legacy_migration.MigrationError, match="not merged or overwritten"):
        legacy_migration.migrate(source_dir=legacy)
    assert report.read_bytes() == original
    assert not any(path.exists() for path in legacy_migration._authoring_targets())
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_cli_status_and_explicit_import_share_the_no_overwrite_transaction(
    isolated_xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)

    assert cli.main(["--legacy-migration-status"]) == 0
    assert "ready:" in capsys.readouterr().out
    assert cli.main(["--migrate-legacy"]) == 0
    imported_output = capsys.readouterr().out
    assert "legacy bytes were left untouched" in imported_output
    assert str(legacy_migration.report_path()) in imported_output
    assert cli.main(["--legacy-migration-status"]) == 0
    assert "imported:" in capsys.readouterr().out
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_schema_five_import_preserves_authoring_and_never_edits_source(
    isolated_xdg: Path,
) -> None:
    legacy, watched, video, manual, automatic = _legacy_fixture(isolated_xdg)

    outcome = legacy_migration.migrate(source_dir=legacy)

    assert outcome.changed
    assert outcome.report == legacy_migration.report_path()
    assert all(path.read_bytes() == before for path, before in watched.items())

    settings = config.load_strict()
    assert settings.roots == (isolated_xdg / "pictures", isolated_xdg / "videos")
    assert settings.display_mode == config.DISPLAY_MODE_INDEPENDENT
    assert settings.theme_source_connector == "DP-1"
    assert settings.active_playlist == "quiet"
    assert settings.scene_fps == 45
    assert settings.scene_scaling == "fill"
    assert settings.scene_clamp == "border"
    assert not settings.video_muted
    assert settings.cycle_interval == 127
    assert settings.shuffle

    migrated_playlists = playlists.Store.open()
    assert migrated_playlists.fault is None
    assert {one.name for one in migrated_playlists.all()} == {"Moving", "Quiet"}
    assert migrated_playlists.find("Moving").entries[0].source == str(video)
    assert migrated_playlists.find("Quiet").entries[0].source == str(manual)

    migrated_pairings = pairings.Store.open()
    assert migrated_pairings.fault is None
    video_record = migrated_pairings.get(pairings.Identity(pairings.Medium.VIDEO, str(video)))
    assert video_record is not None and video_record.still == automatic
    still_record = migrated_pairings.get(pairings.Identity(pairings.Medium.STILL, str(manual)))
    assert still_record is not None
    assert still_record.palette == pairings.PalettePolicy(
        "custom", "My palette", pairings.Mode.LIGHT
    )

    migrated_rules = schedules.Store.open()
    assert migrated_rules.fault is None
    rule = migrated_rules.rules[0]
    assert rule.connector == "eDP-1"
    assert rule.matches(datetime(2026, 8, 4, 2, 0), "eDP-1")
    assert dict(displays.Store.open().all()) == {"DP-1": "quiet", "eDP-1": "moving"}
    assert favourites.Store.open().favourites.entries == ()
    assert credentials.key_path().read_text(encoding="utf-8") == "abc_123\n"
    assert credentials.key_path().stat().st_mode & 0o777 == 0o600
    report = legacy_migration.report_path().read_text(encoding="utf-8")
    assert "Per-display engine settings differed" in report
    assert "Legacy bytes modified: no" in report
    assert "abc_123" not in report


@pytest.mark.parametrize(
    ("field", "legacy_value", "expected"),
    (
        ("scene_scaling", "default", ""),
        ("scene_scaling", "stretch", "stretch"),
        ("scene_scaling", "fit", "fit"),
        ("scene_scaling", "fill", "fill"),
        ("scene_clamp", "clamp", "clamp"),
        ("scene_clamp", "border", "border"),
        ("scene_clamp", "repeat", "repeat"),
    ),
)
def test_all_legacy_scene_presentation_values_map_to_typed_current_settings(
    isolated_xdg: Path,
    field: str,
    legacy_value: str,
    expected: str,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    config_path = legacy / "config.json"
    document = cast(dict[str, Any], json.loads(config_path.read_bytes()))
    outputs = cast(dict[str, Any], document["outputs"])
    engines = cast(dict[str, Any], cast(dict[str, Any], outputs["DP-1"])["engines"])
    scene = cast(dict[str, Any], engines["workshop"])
    legacy_field = "scaling" if field == "scene_scaling" else "clamp"
    scene[legacy_field] = legacy_value
    config_path.write_text(json.dumps(document), encoding="utf-8")
    watched[config_path] = config_path.read_bytes()

    legacy_migration.migrate(source_dir=legacy)

    assert getattr(config.load_strict(), field) == expected
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_import_refuses_any_current_authoring_without_touching_either_side(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    paths.settings_path().parent.mkdir(parents=True)
    current = b"roots = []\n"
    paths.settings_path().write_bytes(current)

    with pytest.raises(legacy_migration.MigrationError, match="not merged or overwritten"):
        legacy_migration.migrate(source_dir=legacy)

    assert paths.settings_path().read_bytes() == current
    assert all(path.read_bytes() == before for path, before in watched.items())
    assert not legacy_migration.marker_path().exists()


@pytest.mark.parametrize(
    "case",
    ("ready", "current-conflict", "in-progress", "corrupt-marker", "changed-decline"),
)
def test_unattended_guard_blocks_every_state_which_needs_an_explicit_decision(
    isolated_xdg: Path,
    case: str,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    if case == "current-conflict":
        paths.settings_path().parent.mkdir(parents=True)
        paths.settings_path().write_text("roots = []\n", encoding="utf-8")
    elif case == "in-progress":
        legacy_migration.journal_path().parent.mkdir(parents=True)
        legacy_migration.journal_path().write_text("{}\n", encoding="utf-8")
    elif case == "corrupt-marker":
        legacy_migration.marker_path().parent.mkdir(parents=True)
        legacy_migration.marker_path().write_text("{", encoding="utf-8")
    elif case == "changed-decline":
        legacy_migration.decline(source_dir=legacy)
        config_path = legacy / "config.json"
        config_path.write_bytes(config_path.read_bytes() + b"\n")

    found = legacy_migration.probe(source_dir=legacy)

    assert found.needs_decision
    with pytest.raises(legacy_migration.MigrationError, match="Open Wall-in-One"):
        legacy_migration.unattended_guard()
    if case != "changed-decline":
        assert all(path.read_bytes() == before for path, before in watched.items())


def test_unattended_writer_excludes_an_import_from_its_guard_through_commit(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An importer cannot enter the old guard/write TOCTOU window."""
    entered_import = threading.Event()
    original = legacy_migration._migrate_locked

    def observed_migrate(
        *, source_dir: Path | None = None, noctalia_settings: Path | None = None
    ) -> legacy_migration.Outcome:
        entered_import.set()
        return original(source_dir=source_dir, noctalia_settings=noctalia_settings)

    monkeypatch.setattr(legacy_migration, "_migrate_locked", observed_migrate)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        # The guard initially sees no predecessor data. It must retain the
        # marker lock while a new current-profile write becomes durable.
        with legacy_migration.unattended_transaction():
            legacy, _watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
            future = pool.submit(legacy_migration.migrate, source_dir=legacy)
            assert not entered_import.wait(0.05), "import entered beside a guarded writer"
            config.save(config.Settings(roots=(isolated_xdg / "library",)))

        with pytest.raises(legacy_migration.MigrationError, match="not merged or overwritten"):
            future.result(timeout=2)
        assert entered_import.is_set()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def test_unattended_transaction_does_not_mislabel_a_writer_io_failure(
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    with (
        pytest.raises(OSError, match="writer fsync failed"),
        legacy_migration.unattended_transaction(),
    ):
        raise OSError("writer fsync failed")


@pytest.mark.parametrize(
    "case",
    (
        "missing-reference",
        "wrong-type",
        "output-limit",
        "unknown-scene-scaling",
        "wrong-scene-clamp-type",
    ),
)
def test_invalid_legacy_references_types_and_limits_fail_without_installing(
    isolated_xdg: Path,
    case: str,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    config_path = legacy / "config.json"
    document = cast(dict[str, Any], json.loads(config_path.read_bytes()))
    outputs = cast(dict[str, Any], document["outputs"])
    playlists_document = cast(dict[str, Any], document["playlists"])
    if case == "missing-reference":
        cast(dict[str, Any], outputs["eDP-1"])["fallback_playlist"] = "does-not-exist"
    elif case == "wrong-type":
        cast(dict[str, Any], playlists_document["moving"])["entries"] = {}
    elif case == "output-limit":
        template = cast(dict[str, Any], outputs["eDP-1"])
        document["outputs"] = {
            f"DP-{index}": dict(template)
            for index in range(legacy_migration.MAX_LEGACY_OUTPUTS + 1)
        }
    else:
        engines = cast(dict[str, Any], cast(dict[str, Any], outputs["DP-1"])["engines"])
        scene = cast(dict[str, Any], engines["workshop"])
        if case == "unknown-scene-scaling":
            scene["scaling"] = "crop-somehow"
        else:
            scene["clamp"] = 7
    config_path.write_text(json.dumps(document), encoding="utf-8")
    before_invalid = config_path.read_bytes()

    with pytest.raises(legacy_migration.MigrationError):
        legacy_migration.migrate(source_dir=legacy)

    assert config_path.read_bytes() == before_invalid
    assert not paths.settings_path().exists()
    assert not legacy_migration.marker_path().exists()
    for path, before in watched.items():
        if path != config_path:
            assert path.read_bytes() == before


def test_missing_file_backed_media_and_stills_are_preserved_and_reported(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    config_path = legacy / "config.json"
    document = cast(dict[str, Any], json.loads(config_path.read_bytes()))
    missing_video = isolated_xdg / "unmounted" / "missing.mp4"
    missing_still = isolated_xdg / "unmounted" / "missing.png"
    for profile in cast(dict[str, dict[str, Any]], document["pairings"]).values():
        if profile.get("media") is not None:
            cast(dict[str, Any], profile["media"])["source"] = str(missing_video)
            cast(dict[str, Any], profile["still"])["mode"] = "selected"
            cast(dict[str, Any], profile["still"])["path"] = str(missing_still)
    moving = cast(dict[str, Any], cast(dict[str, Any], document["playlists"])["moving"])
    entry = cast(dict[str, Any], cast(list[object], moving["entries"])[0])
    cast(dict[str, Any], entry["media"])["source"] = str(missing_video)
    cast(dict[str, Any], entry["still"])["mode"] = "selected"
    cast(dict[str, Any], entry["still"])["path"] = str(missing_still)
    config_path.write_text(json.dumps(document), encoding="utf-8")
    watched[config_path] = config_path.read_bytes()

    legacy_migration.migrate(source_dir=legacy)

    assert playlists.Store.open().find("Moving").entries[0].source == str(missing_video)
    migrated = pairings.Store.open().get(
        pairings.Identity(pairings.Medium.VIDEO, str(missing_video))
    )
    assert migrated is not None and migrated.still == missing_still
    report = legacy_migration.report_path().read_text(encoding="utf-8")
    assert "currently missing" in report
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_installed_workshop_video_imports_with_file_identity_and_legacy_dynamic_id(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    content = isolated_xdg / "workshop-content"
    item = content / "123456789"
    item.mkdir(parents=True)
    motion = item / "wall.mp4"
    motion.write_bytes(b"workshop video")
    (item / "project.json").write_text(
        json.dumps({"type": "Video", "title": "Workshop video", "file": motion.name}),
        encoding="utf-8",
    )
    noctalia = paths.noctalia_settings_path()
    noctalia.write_text(
        noctalia.read_text(encoding="utf-8") + f'workshop_directory = "{content}"\n',
        encoding="utf-8",
    )
    config_path = legacy / "config.json"
    document = cast(dict[str, Any], json.loads(config_path.read_bytes()))
    bundle = _bundle(
        "workshop-video",
        media={"kind": "workshop", "source": "123456789"},
        still={"mode": "automatic"},
    )
    cast(dict[str, Any], document["pairings"])["workshop-profile"] = {
        **bundle,
        "id": "workshop-profile",
    }
    moving = cast(dict[str, Any], cast(dict[str, Any], document["playlists"])["moving"])
    cast(list[object], moving["entries"]).append({**bundle, "pairing_id": "workshop-profile"})
    config_path.write_text(json.dumps(document), encoding="utf-8")

    auto = isolated_xdg / "pictures" / "Wall-in-One" / "Automatic Stills" / "workshop-video.png"
    auto.write_bytes(b"workshop automatic still")
    sidecar = auto.with_name(auto.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_text(
        json.dumps(
            {
                "schema": 1,
                "plugin": "goober/wall-in-one",
                "kind": "automatic-still",
                "path": str(auto),
                "dynamic_id": "123456789",
            }
        ),
        encoding="utf-8",
    )
    watched.update(
        {
            noctalia: noctalia.read_bytes(),
            config_path: config_path.read_bytes(),
            auto: auto.read_bytes(),
            sidecar: sidecar.read_bytes(),
        }
    )

    legacy_migration.migrate(source_dir=legacy)

    assert playlists.Store.open().find("Moving").entries[1].source == str(motion)
    record = pairings.Store.open().get(pairings.Identity(pairings.Medium.VIDEO, str(motion)))
    assert record is not None and record.still == auto
    assert pairings.Store.open().get(pairings.Identity(pairings.Medium.SCENE, "123456789")) is None
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_interrupted_multi_file_import_resumes_exactly_without_overwrite(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    real_link = os.link
    calls = 0

    def fail_second_link(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "injected migration interruption")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr("wall_in_one.legacy_migration.os.link", fail_second_link)
    with pytest.raises(legacy_migration.MigrationError, match="injected migration interruption"):
        legacy_migration.migrate(source_dir=legacy)
    assert legacy_migration.journal_path().is_file()
    assert paths.settings_path().is_file()
    assert all(path.read_bytes() == before for path, before in watched.items())

    monkeypatch.setattr("wall_in_one.legacy_migration.os.link", real_link)
    outcome = legacy_migration.migrate(source_dir=legacy)

    assert outcome.changed
    assert legacy_migration.marker_path().is_file()
    assert not legacy_migration.journal_path().exists()
    assert all(path.read_bytes() == before for path, before in watched.items())


@pytest.mark.parametrize(
    "journal_bytes",
    (
        b"{}\n",
        b'{"version":true,"legacy_source":"/tmp/legacy","legacy_digest":"'
        + b"0" * 64
        + b'","created_at":"2026-08-24T00:00:00+00:00","targets":{}}\n',
    ),
)
def test_malformed_journal_is_never_advertised_as_resumable_even_without_source(
    isolated_xdg: Path,
    journal_bytes: bytes,
) -> None:
    journal = legacy_migration.journal_path()
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(journal_bytes)
    missing_source = isolated_xdg / "missing-legacy-source"

    found = legacy_migration.probe(source_dir=missing_source)

    assert found.status == "conflict"
    assert found.conflicts == (journal,)
    with pytest.raises(legacy_migration.MigrationError):
        legacy_migration.migrate(source_dir=missing_source)
    assert journal.read_bytes() == journal_bytes
    assert not any(path.exists() for path in legacy_migration._authoring_targets())


def test_interrupted_journal_cannot_silently_switch_an_explicit_source(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    second = isolated_xdg / "other-legacy-source"
    shutil.copytree(first, second)
    real_link = os.link
    calls = 0

    def interrupt_install(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "injected migration interruption")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr("wall_in_one.legacy_migration.os.link", interrupt_install)
    with pytest.raises(legacy_migration.MigrationError, match="injected migration interruption"):
        legacy_migration.migrate(source_dir=first)
    monkeypatch.setattr("wall_in_one.legacy_migration.os.link", real_link)
    journal_before = legacy_migration.journal_path().read_bytes()
    current_before = {
        path: path.read_bytes() for path in legacy_migration._authoring_targets() if path.is_file()
    }

    with pytest.raises(legacy_migration.MigrationError, match="cannot Start fresh"):
        legacy_migration.decline(source_dir=first)
    assert not legacy_migration.marker_path().exists()
    assert legacy_migration.probe(source_dir=second).status == "conflict"
    with pytest.raises(legacy_migration.MigrationError, match="belongs to"):
        legacy_migration.migrate(source_dir=second)
    assert legacy_migration.journal_path().read_bytes() == journal_before
    assert {path: path.read_bytes() for path in current_before} == current_before
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_changed_installed_target_makes_interrupted_journal_non_resumable(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    real_link = os.link
    calls = 0

    def interrupt_install(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "injected migration interruption")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr("wall_in_one.legacy_migration.os.link", interrupt_install)
    with pytest.raises(legacy_migration.MigrationError, match="injected migration interruption"):
        legacy_migration.migrate(source_dir=legacy)
    monkeypatch.setattr("wall_in_one.legacy_migration.os.link", real_link)
    paths.settings_path().write_text("roots = []\n", encoding="utf-8")
    changed = paths.settings_path().read_bytes()

    assert legacy_migration.probe(source_dir=legacy).status == "conflict"
    with pytest.raises(legacy_migration.MigrationError, match="different or unsafe bytes"):
        legacy_migration.migrate(source_dir=legacy)
    assert paths.settings_path().read_bytes() == changed
    assert not legacy_migration.marker_path().exists()
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_surviving_completed_journal_must_match_marker_before_cleanup(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    real_unlink = Path.unlink

    def fail_completed_journal_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == legacy_migration.journal_path():
            raise OSError(errno.EIO, "injected journal cleanup failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_completed_journal_unlink)
    outcome = legacy_migration.migrate(source_dir=legacy)
    monkeypatch.setattr(Path, "unlink", real_unlink)

    assert outcome.changed
    assert legacy_migration.marker_path().is_file()
    journal = legacy_migration.journal_path()
    document = cast(dict[str, Any], json.loads(journal.read_bytes()))
    targets = cast(dict[str, str], document["targets"])
    targets["settings"] = "0" * 64
    journal.write_text(json.dumps(document), encoding="utf-8")
    journal_before = journal.read_bytes()

    assert legacy_migration.probe(source_dir=legacy).status == "conflict"
    with pytest.raises(legacy_migration.MigrationError, match="does not match"):
        legacy_migration.migrate(source_dir=legacy)
    assert journal.read_bytes() == journal_before
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_exact_surviving_completed_journal_is_cleaned_idempotently(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    real_unlink = Path.unlink

    def fail_completed_journal_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == legacy_migration.journal_path():
            raise OSError(errno.EIO, "injected journal cleanup failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_completed_journal_unlink)
    assert legacy_migration.migrate(source_dir=legacy).changed
    monkeypatch.setattr(Path, "unlink", real_unlink)

    assert legacy_migration.probe(source_dir=legacy).status == "imported"
    second = legacy_migration.migrate(source_dir=legacy)

    assert not second.changed
    assert not legacy_migration.journal_path().exists()
    assert all(path.read_bytes() == before for path, before in watched.items())


@pytest.mark.parametrize("failure", ("stage", "journal"))
def test_stage_and_journal_failures_install_no_authoring_and_leave_source_exact(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    real_write = state_file.write_atomic_text

    def fail_selected(path: Path, contents: str) -> None:
        is_stage = path.name.endswith(".legacy-migration-v1.stage")
        is_journal = path == legacy_migration.journal_path()
        if (failure == "stage" and is_stage) or (failure == "journal" and is_journal):
            raise OSError(errno.EIO, f"injected {failure} failure")
        real_write(path, contents)

    monkeypatch.setattr(state_file, "write_atomic_text", fail_selected)

    with pytest.raises(legacy_migration.MigrationError, match=f"injected {failure} failure"):
        legacy_migration.migrate(source_dir=legacy)

    assert not any(path.exists() for path in legacy_migration._authoring_targets())
    assert not legacy_migration.marker_path().exists()
    assert all(path.read_bytes() == before for path, before in watched.items())

    monkeypatch.setattr(state_file, "write_atomic_text", real_write)
    assert legacy_migration.migrate(source_dir=legacy).changed


def test_cross_process_import_lock_never_mixes_two_legacy_snapshots(
    isolated_xdg: Path,
) -> None:
    first, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    second = isolated_xdg / "legacy-second-snapshot"
    shutil.copytree(first, second)
    second_config = cast(dict[str, Any], json.loads((second / "config.json").read_bytes()))
    second_playlists = cast(dict[str, Any], second_config["playlists"])
    cast(dict[str, Any], second_playlists["moving"])["name"] = "Moving from snapshot B"
    (second / "config.json").write_text(json.dumps(second_config), encoding="utf-8")
    second_before = {path: path.read_bytes() for path in second.iterdir() if path.is_file()}

    context = multiprocessing.get_context("spawn")
    barrier = context.Event()
    results: Queue[tuple[str, object]] = context.Queue()
    processes = (
        context.Process(target=_spawn_migration, args=(str(first), barrier, results)),
        context.Process(target=_spawn_migration, args=(str(second), barrier, results)),
    )
    for process in processes:
        process.start()
    barrier.set()
    for process in processes:
        process.join(timeout=60)
        assert not process.is_alive()
        assert process.exitcode == 0
    outcomes = [results.get(timeout=10), results.get(timeout=10)]

    assert all(status == "ok" for status, _value in outcomes)
    assert sorted(cast(bool, value) for _status, value in outcomes) == [False, True]
    marker = cast(dict[str, object], json.loads(legacy_migration.marker_path().read_bytes()))
    winner = Path(cast(str, marker["legacy_source"]))
    names = {playlist.name for playlist in playlists.Store.open().all()}
    if winner == first:
        assert names == {"Moving", "Quiet"}
    else:
        assert winner == second
        assert names == {"Moving from snapshot B", "Quiet"}
    report = legacy_migration.report_path().read_text(encoding="utf-8")
    assert f"Source: `{winner}`" in report
    assert all(path.read_bytes() == before for path, before in watched.items())
    assert all(path.read_bytes() == before for path, before in second_before.items())


def test_start_fresh_is_durable_for_only_the_exact_source_and_not_now_is_not(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)

    postponed = legacy_migration.postpone(source_dir=legacy)

    assert not postponed.changed
    assert not legacy_migration.marker_path().exists()
    assert legacy_migration.probe(source_dir=legacy).status == "ready"
    assert all(path.read_bytes() == before for path, before in watched.items())

    declined = legacy_migration.decline(source_dir=legacy)

    assert declined.changed
    marker = json.loads(legacy_migration.marker_path().read_bytes())
    assert marker["disposition"] == "declined"
    assert marker["legacy_schema"] == 5
    assert marker["legacy_config_sha256"]
    assert legacy_migration.probe(source_dir=legacy).status == "declined"
    assert all(path.read_bytes() == before for path, before in watched.items())

    copied_source = isolated_xdg / "same-bytes-different-source"
    shutil.copytree(legacy, copied_source)
    assert legacy_migration.probe(source_dir=copied_source).status == "conflict"

    config_path = legacy / "config.json"
    config_path.write_bytes(config_path.read_bytes() + b"\n")
    assert legacy_migration.probe(source_dir=legacy).status == "conflict"


def test_start_fresh_can_durably_bind_a_future_schema_without_accepting_it(
    isolated_xdg: Path,
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    config_path = legacy / "config.json"
    document = cast(dict[str, Any], json.loads(config_path.read_bytes()))
    document["schema_version"] = 999
    config_path.write_text(json.dumps(document), encoding="utf-8")
    watched[config_path] = config_path.read_bytes()

    assert legacy_migration.probe(source_dir=legacy).status == "corrupt"
    outcome = legacy_migration.decline(source_dir=legacy)
    found = legacy_migration.probe(source_dir=legacy)

    assert outcome.changed
    assert found.status == "declined"
    assert found.schema == 999
    assert all(path.read_bytes() == before for path, before in watched.items())


def test_deeply_nested_legacy_json_is_reported_and_left_byte_exact(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = legacy_migration.legacy_data_dir()
    legacy.mkdir(parents=True)
    document = b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    config_path = legacy / "config.json"
    config_path.write_bytes(document)
    real_loads = json.loads

    def recursion_bounded_loads(
        candidate: str | bytes | bytearray, *args: Any, **kwargs: Any
    ) -> object:
        # CPython 3.14's decoder is iterative for this fixture, while older
        # supported/recovery interpreters can raise at the same nesting. Force
        # that documented failure mode at the parser boundary without changing
        # the genuinely deeply nested source bytes under test.
        if candidate == document:
            raise RecursionError("injected legacy nesting limit")
        return cast(object, real_loads(candidate, *args, **kwargs))

    monkeypatch.setattr(json, "loads", recursion_bounded_loads)

    found = legacy_migration.probe(source_dir=legacy)

    assert found.status == "corrupt"
    assert "readable JSON" in found.detail
    assert config_path.read_bytes() == document
    assert not paths.settings_path().exists()
    assert not legacy_migration.marker_path().exists()


def test_deeply_nested_noctalia_toml_recursion_is_bounded_and_left_byte_exact(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy, watched, _video, _manual, _automatic = _legacy_fixture(isolated_xdg)
    settings_path = paths.noctalia_settings_path()
    document = settings_path.read_bytes()

    def reject_nested_settings(candidate: str, /, *, parse_float: Any = float) -> dict[str, Any]:
        del candidate, parse_float
        raise RecursionError("injected TOML nesting limit")

    monkeypatch.setattr(tomllib, "loads", reject_nested_settings)

    found = legacy_migration.probe(source_dir=legacy)

    assert found.status == "corrupt"
    assert "valid UTF-8 TOML" in found.detail
    assert settings_path.read_bytes() == document
    assert all(path.read_bytes() == before for path, before in watched.items())
    assert not legacy_migration.marker_path().exists()


def test_migrated_automatic_still_is_hidden_and_cleanup_is_identity_bounded(
    isolated_xdg: Path,
) -> None:
    _legacy, _watched, video, _manual, automatic = _legacy_fixture(isolated_xdg)
    root = isolated_xdg / "pictures"
    video_root = isolated_xdg / "videos"

    library = scan.scan((root, video_root))

    assert automatic not in {item.path for item in library.items}
    video_item = next(item for item in library.items if item.path == video)
    artifacts = manage.pairing_artifact_paths(
        video_item,
        (root, video_root),
        legacy_selected_still=automatic,
    )
    assert automatic in artifacts
    assert automatic.with_name(automatic.name + pairing.SIDECAR_SUFFIX) in artifacts

    forged = automatic.with_name("other.png")
    forged.write_bytes(b"user still")
    assert pairing.legacy_automatic_identity(forged) is None
