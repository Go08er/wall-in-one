from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, favourites, pairings, playlists, removals, schedules
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.session import Session


@pytest.mark.parametrize("old_schema", (1, 2, 3))
def test_write_config_upgrades_old_schema_without_importing_the_gui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_schema: int
) -> None:
    from wall_in_one import cli, paths

    media = tmp_path / "media"
    media.mkdir()
    (media / "wallpaper.png").write_bytes(b"fixture")
    config.save(config.Settings(roots=(media,), scan_workshop=False))
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True)
    target.write_text(f'schema_version = {old_schema}\nupgrade_fixture = "old"\n')
    monkeypatch.delitem(sys.modules, "wall_in_one.ui.app", raising=False)

    assert cli.main(["--write-config"]) == 0

    document = tomllib.loads(target.read_text())
    assert document["schema_version"] == 4
    assert "upgrade_fixture" not in document
    assert document["playlists"][0]["entries"][0]["still"] == str(media / "wallpaper.png")
    assert "wall_in_one.ui.app" not in sys.modules


def test_write_config_takes_the_compiler_lock_before_reading_authoring_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wall_in_one import cli

    entered = False

    class ObservedLock:
        def __enter__(self) -> None:
            nonlocal entered
            entered = True

        def __exit__(
            self,
            _kind: type[BaseException] | None,
            _error: BaseException | None,
            _traceback: object,
        ) -> None:
            nonlocal entered
            entered = False

    def load_under_lock(*, require_present: bool = False) -> config.LoadedSettings:
        assert entered
        assert require_present
        raise config.ConfigError("authoring fixture stops here")

    monkeypatch.setattr(runtime_config, "compiler_lock", ObservedLock)
    monkeypatch.setattr(config, "load_strict_document", load_under_lock)

    # Settings that cannot be read as written are a configuration error.
    assert cli.main(["--write-config"]) == cli.EXIT_CONFIG
    assert "authoring fixture stops here" in capsys.readouterr().err
    assert not entered


@pytest.mark.parametrize("argument", ["--write-config", "--service-startup-prepare"])
def test_fresh_preflight_leaves_the_profile_ready_for_graphical_setup(
    argument: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from wall_in_one import cli, deployed_upgrade, legacy_migration, paths

    assert deployed_upgrade.probe().status == "absent"
    assert config.load().roots == ()
    assert config.load_strict().roots == ()

    # Repeated startup attempts must not create a runtime without its owning
    # settings or turn the next GUI launch into an orphan-profile error.
    for _attempt in range(2):
        assert cli.main([argument]) == cli.EXIT_CONFIG
        assert "Open Wall-in-One and choose a library folder" in capsys.readouterr().err
        assert not paths.settings_path().exists()
        assert not paths.runtime_config_path().exists()
        assert deployed_upgrade.probe().status == "absent"
        assert not legacy_migration.probe().needs_decision
        assert cli._run_graphical_startup_upgrade(require_legacy_safe=False) is None


def test_strict_settings_can_require_a_saved_profile_without_changing_gui_defaults() -> None:
    from wall_in_one import paths

    with pytest.raises(config.MissingSettingsError, match="Open Wall-in-One"):
        config.load_strict(require_present=True)
    assert config.load_strict() == config.Settings()
    assert config.load() == config.Settings()
    assert not paths.settings_path().exists()


@pytest.mark.parametrize("empty_document", [False, True])
def test_saved_empty_settings_remain_valid_for_service_preparation(
    empty_document: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wall_in_one import cli, deployed_upgrade, paths

    config.save(config.Settings())
    if empty_document:
        paths.settings_path().write_text("", encoding="utf-8")
    saved = paths.settings_path().read_bytes()

    assert cli.main(["--service-startup-prepare"]) == 0
    assert not capsys.readouterr().err
    assert paths.settings_path().read_bytes() == saved
    assert paths.runtime_config_path().exists()
    assert deployed_upgrade.probe().status == "current"


def test_service_startup_prepare_retains_a_valid_lkg_after_compiler_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wall_in_one import cli, paths

    media = tmp_path / "media"
    media.mkdir()
    (media / "wallpaper.png").write_bytes(b"fixture")
    config.save(config.Settings(roots=(media,), scan_workshop=False))
    assert cli.main(["--write-config"]) == 0
    capsys.readouterr()

    runtime = paths.runtime_config_path()
    previous = runtime.read_bytes()
    authoring = playlists.state_path()
    authoring.parent.mkdir(parents=True, exist_ok=True)
    authoring.write_text("{ broken", encoding="utf-8")

    assert cli.main(["--service-startup-prepare"]) == 0
    error = capsys.readouterr().err
    assert "left untouched" in error
    assert "validating the last-known-good runtime" in error
    assert runtime.read_bytes() == previous


@pytest.mark.parametrize(
    ("status", "expected"),
    (("retry", 75), ("conflict", 78)),
)
def test_service_startup_prepare_never_softens_a_deployed_upgrade_boundary(
    monkeypatch: pytest.MonkeyPatch,
    status: Literal["retry", "conflict"],
    expected: int,
) -> None:
    from wall_in_one import cli, deployed_upgrade_transaction

    def refuse_upgrade(*, cutover: bool = True) -> deployed_upgrade_transaction.Outcome:
        del cutover
        raise deployed_upgrade_transaction.TransactionError("fixture boundary", status=status)

    monkeypatch.setattr(deployed_upgrade_transaction, "ensure", refuse_upgrade)

    assert cli.main(["--service-startup-prepare"]) == expected


def test_explicit_deployed_upgrade_command_requests_the_bounded_cutover(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wall_in_one import cli, deployed_upgrade_transaction

    cutovers: list[bool] = []

    def ensure(*, cutover: bool = True) -> deployed_upgrade_transaction.Outcome:
        cutovers.append(cutover)
        return deployed_upgrade_transaction.Outcome(
            changed=True,
            status="complete",
            detail="fixture cutover complete",
        )

    monkeypatch.setattr(deployed_upgrade_transaction, "ensure", ensure)

    assert cli.main(["--prepare-deployed-upgrade"]) == 0
    assert cutovers == [True]
    assert "fixture cutover complete" in capsys.readouterr().out


def test_write_config_refuses_a_malformed_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unattended upgrade must not compile defaults from corrupt settings."""
    from wall_in_one import cli, paths

    settings = paths.settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_text("roots = [\n", encoding="utf-8")
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = 'schema_version = 1\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    assert cli.main(["--write-config"]) == cli.EXIT_CONFIG
    assert "not valid TOML" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == previous


def test_generation_reader_turns_pathological_toml_nesting_into_a_config_error() -> None:
    document = "nested = " + "[" * 1_100 + "0" + "]" * 1_100

    with pytest.raises(runtime_config.RuntimeConfigError, match="cannot parse"):
        runtime_config.document_generation(document)


@pytest.mark.parametrize(
    ("settings_text", "message"),
    (
        ('cycle_enabled = "yes"\n', "cycle_enabled must be a boolean"),
        ("cycle_interval = 2.5\n", "cycle_interval must be an integer"),
        ("cycle_interval = 2\n", "cycle_interval must be between"),
        ("opacity = nan\n", "opacity must be a finite number"),
        ("opacity = " + "1" + "0" * 400 + "\n", "opacity must be between"),
        ('video_interpolation = "magic"\n', "video_interpolation must be one of"),
        ('scene_scaling = "zoom"\n', "scene_scaling must be one of"),
        ('scene_clamp = "mirror"\n', "scene_clamp must be one of"),
        ('roots = ["/valid", 7]\n', "roots must be an array"),
        (
            'roots = ["~wio_nonexistent_user_20260905/Pictures"]\n',
            "roots[0] cannot expand its path",
        ),
        ('roots = ["~bad\\u0000/Pictures"]\n', "roots[0] cannot expand its path"),
    ),
)
def test_write_config_refuses_semantically_invalid_settings_without_replacing_runtime(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    settings_text: str,
    message: str,
) -> None:
    """Parseable TOML must not be allowed to compile repaired defaults."""
    from wall_in_one import cli, paths

    settings = paths.settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_text(settings_text, encoding="utf-8")
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = 'schema_version = 4\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    assert cli.main(["--write-config"]) == cli.EXIT_CONFIG
    assert message in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == previous


@pytest.mark.parametrize(
    ("settings_text", "message"),
    (
        (
            f"active_playlist = {json.dumps('x' * (config.MAX_RUNTIME_REFERENCE_BYTES + 1))}\n",
            "active_playlist must be at most 480 UTF-8 bytes",
        ),
        (
            f"output = {json.dumps('DP-1' + chr(1))}\n",
            "output cannot contain control characters",
        ),
        (
            f"output = {json.dumps('x' * (config.MAX_RUNTIME_CONNECTOR_BYTES + 1))}\n",
            "output must be at most 256 UTF-8 bytes",
        ),
        (
            f"roots = [{json.dumps('/' + 'x' * config.MAX_RUNTIME_PATH_BYTES)}]\n",
            "roots[0] must be at most 4096 UTF-8 bytes",
        ),
        (
            f"roots = [{json.dumps('/wallpapers/' + chr(1))}]\n",
            "roots[0] cannot contain control characters",
        ),
    ),
)
def test_headless_settings_obey_rust_wire_bounds_without_replacing_runtime(
    settings_text: str,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wall_in_one import cli, paths

    settings = paths.settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_text(settings_text, encoding="utf-8")
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = 'schema_version = 4\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    assert cli.main(["--write-config"]) == cli.EXIT_CONFIG
    assert message in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == previous


@pytest.mark.parametrize(
    ("state_path", "store_name"),
    (
        (pairings.state_path, "pairings"),
        (playlists.state_path, "playlists"),
        (schedules.state_path, "schedules"),
        (displays.state_path, "display assignments"),
        (favourites.state_path, "favourites"),
        (removals.state_path, "pending removals"),
    ),
)
def test_write_config_refuses_unreadable_authoring_state_without_replacing_runtime(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state_path: Callable[[], Path],
    store_name: str,
) -> None:
    """A recovered empty Store must not erase unattended automation."""
    from wall_in_one import cli, paths

    media = tmp_path / "media"
    media.mkdir()
    (media / "wallpaper.png").write_bytes(b"fixture")
    config.save(config.Settings(roots=(media,), scan_workshop=False))
    corrupt_path = state_path()
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text("{ this is not JSON", encoding="utf-8")

    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = 'schema_version = 1\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    assert cli.main(["--write-config"]) == 1

    error = capsys.readouterr().err
    assert store_name in error
    assert corrupt_path.name in error
    assert "Repair or restore" in error
    assert "left untouched" in error
    assert target.read_text(encoding="utf-8") == previous


def test_write_config_does_not_consume_a_valid_pending_removal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A short-lived compiler must not race the GUI's metadata cleanup."""
    from wall_in_one import cli

    media = tmp_path / "media"
    media.mkdir()
    removed_path = media / "removed.png"
    removed_path.write_bytes(b"removed")
    (media / "survivor.png").write_bytes(b"survivor")
    config.save(config.Settings(roots=(media,), scan_workshop=False))
    removed = MediaItem(removed_path, Kind.STILL, 7, 1)
    pairing_store = pairings.Store.open()
    pairing_store.mark_borked(removed, "renderer crashed", "renderer-crash")
    removals.Store.open().prepare(removed, (media,))
    removed_path.unlink()

    assert cli.main(["--write-config"]) == 0
    assert "wrote:" in capsys.readouterr().out

    assert removals.Store.open().records
    saved = pairings.Store.open().get(pairings.Identity.of(removed))
    assert saved is not None and saved.health.is_borked


@pytest.mark.parametrize(
    ("state_path", "store_name", "valid", "malformed"),
    (
        (
            pairings.state_path,
            "pairings",
            {"version": 1, "pairings": []},
            {"version": 1, "pairings": [{"identity": "not-an-identity"}]},
        ),
        (
            playlists.state_path,
            "playlists",
            {"version": 1, "playlists": []},
            {"version": 1, "playlists": [{"name": "missing id"}]},
        ),
        (
            schedules.state_path,
            "schedules",
            {"version": 1, "rules": []},
            {"version": 1, "rules": [{"playlist": "missing id"}]},
        ),
        (
            displays.state_path,
            "display assignments",
            {"version": 1, "displays": {}},
            {"version": 1, "displays": {"eDP-1": 7}},
        ),
        (
            favourites.state_path,
            "favourites",
            {"version": 1, "paths": []},
            {"version": 1, "paths": ["relative/wallpaper.png"]},
        ),
        (
            removals.state_path,
            "pending removals",
            {"version": 1, "removals": []},
            {
                "version": 1,
                "removals": [
                    {
                        "identity": "still:relative.png",
                        "path": "relative.png",
                        "kind": "still",
                        "roots": ["/wallpapers"],
                    }
                ],
            },
        ),
    ),
)
@pytest.mark.parametrize("damage", ("symlink", "directory", "malformed-record", "future"))
def test_write_config_preserves_last_good_for_every_authoring_store_fault(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state_path: Callable[[], Path],
    store_name: str,
    valid: dict[str, object],
    malformed: dict[str, object],
    damage: str,
) -> None:
    """Recovery for the GUI must remain fail-closed for unattended automation."""
    from wall_in_one import cli, paths

    media = tmp_path / "media"
    media.mkdir()
    (media / "wallpaper.png").write_bytes(b"fixture")
    config.save(config.Settings(roots=(media,), scan_workshop=False))
    authoring = state_path()
    authoring.parent.mkdir(parents=True, exist_ok=True)
    if damage == "symlink":
        elsewhere = authoring.with_name(f"{authoring.stem}-elsewhere.json")
        elsewhere.write_text(json.dumps(valid), encoding="utf-8")
        authoring.symlink_to(elsewhere)
    elif damage == "directory":
        authoring.mkdir()
    elif damage == "malformed-record":
        authoring.write_text(json.dumps(malformed), encoding="utf-8")
    else:
        future = dict(valid)
        future["version"] = 99
        authoring.write_text(json.dumps(future), encoding="utf-8")

    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = 'schema_version = 1\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    assert cli.main(["--write-config"]) == 1

    error = capsys.readouterr().err
    assert store_name in error
    assert authoring.name in error
    assert "left untouched" in error
    assert target.read_text(encoding="utf-8") == previous


def test_shared_runtime_compiler_refuses_a_recovered_empty_authoring_store(
    tmp_path: Path,
) -> None:
    """The GUI publisher and headless CLI share the same fail-closed seam."""
    corrupt = pairings.state_path()
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{ broken JSON", encoding="utf-8")
    session = Session(config.Settings(scan_workshop=False))
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 1\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")
    try:
        with pytest.raises(runtime_config.RuntimeConfigError, match="pairings"):
            runtime_config.update(session.settings, session, target)
    finally:
        session.shutdown()
    assert target.read_text(encoding="utf-8") == previous


def _session(
    tmp_path: Path,
    *,
    display_assignments: dict[str, str] | None = None,
    extra_playlists: tuple[playlists.Playlist, ...] = (),
) -> tuple[config.Settings, Session]:
    still = tmp_path / "still.png"
    video = tmp_path / "video.mp4"
    representative = tmp_path / "video-still.png"
    for path in (still, video, representative):
        path.write_bytes(b"fixture")
    items = (
        MediaItem(still, Kind.STILL, 7, 1),
        MediaItem(video, Kind.VIDEO, 7, 1, paired_still=representative),
    )
    named = playlists.Playlist(
        id="evening",
        name="Evening",
        entries=(playlists.Entry(id="entry-video", source=str(video)),),
    )
    palette = pairings.Pairing(
        identity=pairings.Identity.of(items[1]),
        still=representative,
        palette=pairings.PalettePolicy(
            kind="community", name="Catppuccin", mode=pairings.Mode.DARK
        ),
        customized=True,
    )
    settings = config.Settings(
        roots=(tmp_path,),
        active_playlist="evening",
        cycle_enabled=True,
        shuffle=True,
        video_hardware_decode=False,
        video_interpolation="oversample",
        scene_fps=75,
        scene_scaling="fit",
        scene_clamp="repeat",
    )
    authored_playlists = {named.id: named}
    authored_playlists.update((playlist.id, playlist) for playlist in extra_playlists)
    session = Session(
        settings,
        scanner=lambda _roots: Library(roots=(tmp_path,), items=items),
        pairing_store=pairings.Store({palette.identity.key: palette}),
        playlist_store=playlists.Store(authored_playlists),
        schedule_store=schedules.Store(
            (
                schedules.Rule(
                    id="night",
                    playlist="evening",
                    weekdays=frozenset({4, 5}),
                    start=22 * 60,
                    end=6 * 60,
                ),
            )
        ),
        display_store=displays.Store(
            {"DP-1": "evening"} if display_assignments is None else display_assignments
        ),
    )
    session.refresh()
    return settings, session


def test_status_inventory_budget_matches_the_rust_protocol_limit() -> None:
    """A sub-8-MiB TOML document may still expand beyond one status reply."""
    lines = ["schema_version = 4", 'default_playlist = "p0"']
    for index in range(513):
        lines.extend(
            (
                "[[playlists]]",
                f'id = "p{index}"',
                f'name = "{index:03}{"🐈" * 117}"',
                "[[playlists.entries]]",
                'id = "e"',
                'kind = "still"',
                'still = "/x"',
            )
        )
    for index in range(512):
        lines.extend(
            (
                "[[schedules]]",
                f'id = "{"r" * 251}-{index:04}"',
                f'playlist = "p{index}"',
                "enabled = true",
            )
        )
    document = "\n".join(lines) + "\n"
    assert len(document.encode("utf-8")) < runtime_config.MAX_RUNTIME_CONFIG_BYTES

    with pytest.raises(runtime_config.RuntimeConfigError, match="protocol response limit"):
        runtime_config._validate_status_budget(document)


def test_status_budget_failure_never_replaces_last_known_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 4\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    def reject(_document: str) -> None:
        raise runtime_config.RuntimeConfigError("runtime status exceeds protocol response limit")

    monkeypatch.setattr(runtime_config, "_validate_status_budget", reject)
    with pytest.raises(runtime_config.RuntimeConfigError, match="protocol response limit"):
        runtime_config.update(settings, session, target)

    assert target.read_text(encoding="utf-8") == previous


_LOCK_HOLDER = """
import sys
import time
from pathlib import Path

from wall_in_one import runtime_config

target, ready, release = (Path(value) for value in sys.argv[1:4])
document = sys.argv[4]
with runtime_config.compiler_lock(target, timeout=5):
    ready.write_text("locked", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not release.exists():
        raise SystemExit("release barrier timed out")
    if document != "-":
        runtime_config._install(document, target)
"""


def _start_lock_holder(
    target: Path,
    ready: Path,
    release: Path,
    *,
    document: str = "-",
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(target), str(ready), str(release), document],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            output, error = process.communicate()
            pytest.fail(f"lock holder exited early: {output}{error}")
        time.sleep(0.01)
    if not ready.exists():
        process.kill()
        output, error = process.communicate()
        pytest.fail(f"lock holder did not reach its barrier: {output}{error}")
    return process


def _finish_lock_holder(process: subprocess.Popen[str], release: Path) -> None:
    release.touch(exist_ok=True)
    output, error = process.communicate(timeout=5)
    assert process.returncode == 0, f"lock holder failed: {output}{error}"


def test_newer_gui_compilation_lands_after_an_older_preflight_snapshot(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "runtime.toml"
    target.write_text('schema_version = 4\ngeneration = "last-known-good"\n', encoding="utf-8")
    ready = tmp_path / "older.ready"
    release = tmp_path / "older.release"
    older = 'schema_version = 4\ngeneration = "older-preflight"\n'
    process = _start_lock_holder(target, ready, release, document=older)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            newer = pool.submit(runtime_config.update, settings, session, target)
            time.sleep(0.1)
            assert not newer.done(), "the newer publisher bypassed the preflight compiler lock"
            release.touch()
            assert newer.result(timeout=5)
        _finish_lock_holder(process, release)
    finally:
        release.touch(exist_ok=True)
        if process.poll() is None:
            process.kill()
            process.communicate()
        session.shutdown()

    document = tomllib.loads(target.read_text(encoding="utf-8"))
    assert document["default_playlist"] == "evening"
    assert "generation" not in document


def test_compiler_lock_timeout_preserves_the_last_known_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 4\ngeneration = "last-known-good"\n'
    target.write_text(previous, encoding="utf-8")
    ready = tmp_path / "held.ready"
    release = tmp_path / "held.release"
    process = _start_lock_holder(target, ready, release)
    monkeypatch.setattr(runtime_config, "COMPILER_LOCK_TIMEOUT_SECONDS", 0.05)

    try:
        with pytest.raises(runtime_config.RuntimeConfigError, match="compiler lock"):
            runtime_config.update(settings, session, target)
        assert target.read_text(encoding="utf-8") == previous
    finally:
        _finish_lock_holder(process, release)
        session.shutdown()


def test_compiler_lock_refuses_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 4\ngeneration = "last-known-good"\n'
    target.write_text(previous, encoding="utf-8")
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("precious", encoding="utf-8")
    runtime_config._compiler_lock_path(target).symlink_to(sentinel)

    try:
        with pytest.raises(runtime_config.RuntimeConfigError, match=r"safely open.*compiler lock"):
            runtime_config.update(settings, session, target)
    finally:
        session.shutdown()

    assert target.read_text(encoding="utf-8") == previous
    assert sentinel.read_text(encoding="utf-8") == "precious"


@pytest.mark.parametrize("collision", ("duplicate-name", "id-name"))
def test_compiler_refuses_ambiguous_playlist_identity_before_replacing_runtime(
    tmp_path: Path, collision: str
) -> None:
    source = tmp_path / "still.png"
    if collision == "duplicate-name":
        first = playlists.Playlist(
            id="first",
            name="Same name",
            entries=(playlists.Entry(id="entry-first", source=str(source)),),
        )
        second = playlists.Playlist(
            id="second",
            name="same NAME",
            entries=(playlists.Entry(id="entry-second", source=str(source)),),
        )
    else:
        first = playlists.Playlist(
            id="opaque-id",
            name="First",
            entries=(playlists.Entry(id="entry-first", source=str(source)),),
        )
        second = playlists.Playlist(
            id="second",
            name="opaque-id",
            entries=(playlists.Entry(id="entry-second", source=str(source)),),
        )
    settings, session = _session(tmp_path, extra_playlists=(first, second))
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 4\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")
    try:
        with pytest.raises(runtime_config.RuntimeConfigError, match="playlist"):
            runtime_config.update(settings, session, target)
    finally:
        session.shutdown()
    assert target.read_text(encoding="utf-8") == previous


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("output-control", "output connector setting cannot contain control"),
        (
            "active-playlist-long",
            "active playlist setting must be at most",
        ),
        (
            "root-long",
            "library root 1 must be at most",
        ),
    ),
)
def test_shared_compiler_rechecks_wire_bound_settings_and_preserves_runtime(
    tmp_path: Path, case: str, message: str
) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 4\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")
    if case == "output-control":
        changed = replace(settings, output="DP-1\x01")
    elif case == "active-playlist-long":
        changed = replace(
            settings,
            active_playlist="x" * (runtime_config.MAX_REFERENCE_BYTES + 1),
        )
    else:
        changed = replace(
            settings,
            roots=(Path("/") / ("x" * runtime_config.MAX_PATH_BYTES),),
        )
    try:
        with pytest.raises(runtime_config.RuntimeConfigError, match=message):
            runtime_config.update(changed, session, target)
    finally:
        session.shutdown()
    assert target.read_text(encoding="utf-8") == previous


def test_compiler_resolves_authoring_identity_away(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    document = tomllib.loads(runtime_config.render(settings, session))
    assert document["schema_version"] == 4
    assert len(document["config_generation"]) == 64
    assert document["settings"]["display_mode"] == "mirrored"
    assert document["settings"]["theme_source_connector"] == ""
    assert "niri_program" not in document["renderer"], "display discovery needs no program"
    assert Path(document["renderer"]["noctalia_program"]).is_absolute()
    assert document["renderer"]["scene_fps"] == 75
    assert document["renderer"]["scene_scaling"] == "fit"
    assert document["renderer"]["scene_clamp"] == "repeat"
    assert document["renderer"]["video_hardware_decode"] is False
    assert document["renderer"]["video_interpolation"] == "oversample"
    assert document["renderer"]["scene_muted"] is True
    assert document["renderer"]["scene_volume"] == 0
    assert document["default_playlist"] == "evening"
    assert [one["id"] for one in document["playlists"]] == ["all-media", "evening"]
    entry = document["playlists"][1]["entries"][0]
    assert entry == {
        "id": "entry-video",
        "kind": "video",
        "still": str(tmp_path / "video-still.png"),
        "motion": str(tmp_path / "video.mp4"),
        "palette": {
            "kind": "named",
            "source": "community",
            "name": "Catppuccin",
            "mode": "dark",
        },
    }
    text = runtime_config.render(settings, session)
    assert "medium:source" not in text
    assert "pairings.json" not in text
    assert document["schedules"][0]["weekdays"] == [4, 5]
    assert document.get("displays", []) == []


def test_compiler_generation_is_stable_non_recursive_and_semantic(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    rendered = runtime_config.render(settings, session)
    generation = runtime_config.document_generation(rendered)
    identity_line = f'config_generation = "{generation}"\n'
    semantic_body = rendered.replace(identity_line, "", 1)

    assert generation == hashlib.sha256(semantic_body.encode("utf-8")).hexdigest()
    assert (
        runtime_config.document_generation(runtime_config.render(settings, session)) == generation
    )
    assert (
        runtime_config.document_generation(
            runtime_config.render(replace(settings, cycle_interval=301), session)
        )
        != generation
    )

    target = tmp_path / "runtime.toml"
    target.write_text(rendered, encoding="utf-8")
    assert runtime_config.read_config_generation(target) == generation


@pytest.mark.parametrize(
    "value",
    ("", "a" * 63, "a" * 65, "A" * 64, "z" * 64),
)
def test_document_generation_rejects_noncanonical_tokens(value: str) -> None:
    with pytest.raises(runtime_config.RuntimeConfigError, match="config_generation"):
        runtime_config.document_generation(f'schema_version = 4\nconfig_generation = "{value}"\n')


def test_compiler_uses_a_pairing_specific_adaptive_generator(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    still = session.library.items[0]
    session.pairings.choose_palette(still, pairings.PalettePolicy(pairings.ADAPTIVE, "m3-rainbow"))

    document = tomllib.loads(runtime_config.render(settings, session))

    assert document["playlists"][0]["entries"][0]["palette"]["scheme"] == "m3-rainbow"


def test_compiler_marks_every_occurrence_of_a_borked_wallpaper(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    video = next(item for item in session.library.items if item.kind is Kind.VIDEO)
    session.pairings.mark_borked(
        video,
        "mpvpaper rejected this wallpaper",
        "automatic-apply",
    )

    document = tomllib.loads(runtime_config.render(settings, session))
    occurrences = [
        entry
        for playlist in document["playlists"]
        for entry in playlist["entries"]
        if entry.get("motion") == str(video.path)
    ]

    assert len(occurrences) == 2, "All media and the authored playlist both carry health"
    assert {tuple(sorted(entry["taboo"].items())) for entry in occurrences} == {
        (
            ("reason", "mpvpaper rejected this wallpaper"),
            ("source", "automatic-apply"),
        )
    }

    session.pairings.clear_borked(video)
    cleared = tomllib.loads(runtime_config.render(settings, session))
    assert all(
        "taboo" not in entry for playlist in cleared["playlists"] for entry in playlist["entries"]
    )


def test_compiler_write_is_atomic_and_leaves_no_temporary(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "state" / "runtime.toml"
    assert runtime_config.write(settings, session, target) == target
    assert tomllib.loads(target.read_text())["schema_version"] == 4
    assert list(target.parent.glob(".*.tmp")) == []


def test_unchanged_compilation_does_not_replace_the_runtime_document(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "state" / "runtime.toml"

    assert runtime_config.update(settings, session, target)
    inode = target.stat().st_ino
    assert not runtime_config.update(settings, session, target)

    assert target.stat().st_ino == inode


@pytest.mark.parametrize("kind", ("symlink", "fifo"))
def test_compiler_never_follows_or_waits_on_an_unsafe_runtime_target(
    tmp_path: Path, kind: str
) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "state" / "runtime.toml"
    target.parent.mkdir()
    outside = tmp_path / "outside.toml"
    outside.write_text('precious = "yes"\n', encoding="utf-8")
    if kind == "symlink":
        target.symlink_to(outside)
    else:
        os.mkfifo(target)

    with pytest.raises(runtime_config.RuntimeConfigError, match="cannot read"):
        runtime_config.update(settings, session, target)

    assert outside.read_text(encoding="utf-8") == 'precious = "yes"\n'


def test_unresolved_playlist_entries_are_omitted_not_looked_up(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    session.playlists.add("evening", tmp_path / "not-in-library.mp4", entry_id="missing")
    document = tomllib.loads(runtime_config.render(settings, session))
    entries = document["playlists"][1]["entries"]
    assert [entry["id"] for entry in entries] == ["entry-video"]


def test_a_default_with_no_resolved_entries_is_built_as_all_media(tmp_path: Path) -> None:
    """It refused the whole compile ("playlist 'Empty' has no playable entries; all 1 authored
    entries are missing or unresolved") and kept the last document. The playlist is now left
    out of the new one, with the default that names it, and the rest is published and said."""
    settings, session = _session(tmp_path)
    empty = session.playlists.create("Empty")
    session.playlists.add(empty.id, tmp_path / "not-in-library.mp4")
    settings = config.Settings(roots=settings.roots, active_playlist=empty.id)
    target = tmp_path / "runtime.toml"
    target.write_text('schema_version = 4\nlast_known_good = "replaced"\n', encoding="utf-8")

    published = runtime_config.publish_changes(settings, session, target)

    assert published.written == (target,)
    assert published.left_out == (
        runtime_config.LeftOut(
            runtime_config.DEFAULT_OWNER, "", empty.id, "Empty", runtime_config.NOTHING_TO_PLAY
        ),
    )
    document = tomllib.loads(target.read_text(encoding="utf-8"))
    assert document["default_playlist"] == runtime_config.FALLBACK_PLAYLIST_ID
    assert [playlist["name"] for playlist in document["playlists"]] == ["All media", "Evening"]
    assert [rule["playlist"] for rule in document["schedules"]] == ["evening"]


def test_an_unreferenced_empty_playlist_remains_a_safe_authoring_draft(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    draft = session.playlists.create("Draft")

    document = tomllib.loads(runtime_config.render(settings, session))

    assert draft.id not in {playlist["id"] for playlist in document["playlists"]}


def test_a_schedule_cannot_silently_lose_its_empty_playlist(tmp_path: Path) -> None:
    """Not silently: the rule is left out and the compile says which and why.

    It used to refuse ("schedule 'draft-rule' refers to playlist 'Draft',
    which has no playable entries") and keep the last document; the rest is
    now published.
    """
    settings, session = _session(tmp_path)
    draft = session.playlists.create("Draft")
    session.schedules.add(draft.id, rule_id="draft-rule")
    target = tmp_path / "runtime.toml"
    target.write_text('schema_version = 4\nlast_known_good = "replaced"\n', encoding="utf-8")

    published = runtime_config.publish_changes(settings, session, target)

    assert published.written == (target,)
    (lost,) = published.left_out
    assert lost == runtime_config.LeftOut(
        runtime_config.SCHEDULE_OWNER,
        "draft-rule",
        draft.id,
        "Draft",
        runtime_config.NOTHING_TO_PLAY,
    )
    assert lost.warning() == (
        "schedule rule 'draft-rule' names playlist 'Draft', which has nothing to play; "
        "building without that rule"
    )
    document = tomllib.loads(target.read_text(encoding="utf-8"))
    assert [rule["id"] for rule in document["schedules"]] == ["night"]


@pytest.mark.parametrize(
    ("identifier", "name", "entry_id", "message"),
    (
        ("x" * (runtime_config.MAX_IDENTIFIER_BYTES + 1), "Valid", "entry", "playlist id"),
        ("valid", "x" * (runtime_config.MAX_PLAYLIST_NAME_CHARS + 1), "entry", "name"),
        ("valid", "Valid", "entry\x01", "playlist entry id"),
    ),
)
def test_compiler_rejects_authored_strings_outside_the_rust_wire_contract(
    tmp_path: Path,
    identifier: str,
    name: str,
    entry_id: str,
    message: str,
) -> None:
    source = tmp_path / "still.png"
    invalid = playlists.Playlist(
        id=identifier,
        name=name,
        entries=(playlists.Entry(id=entry_id, source=str(source)),),
    )
    settings, session = _session(tmp_path, extra_playlists=(invalid,))
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 4\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")
    try:
        with pytest.raises(runtime_config.RuntimeConfigError, match=message):
            runtime_config.update(settings, session, target)
    finally:
        session.shutdown()
    assert target.read_text(encoding="utf-8") == previous


def test_compiler_rejects_schedule_strings_outside_wire_bounds(
    tmp_path: Path,
) -> None:
    settings, session = _session(
        tmp_path,
        display_assignments={"x" * (runtime_config.MAX_CONNECTOR_BYTES + 1): "evening"},
    )
    session.schedules.add(
        "evening",
        rule_id="x" * (runtime_config.MAX_IDENTIFIER_BYTES + 1),
    )
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 4\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    with pytest.raises(runtime_config.RuntimeConfigError, match="schedule id must be at most"):
        runtime_config.update(settings, session, target)
    assert target.read_text(encoding="utf-8") == previous

    # Dormant independent assignments are authoring state, not executable wire
    # data, so they cannot poison the last working mirrored document.
    session.schedules.remove(session.schedules.rules[-1].id)
    assert runtime_config.update(settings, session, target) is True
    assert tomllib.loads(target.read_text(encoding="utf-8")).get("displays", []) == []


def test_legacy_single_output_is_dormant_under_the_new_mirrored_default(tmp_path: Path) -> None:
    settings, session = _session(tmp_path, display_assignments={})
    settings = config.Settings(
        roots=settings.roots,
        active_playlist="evening",
        output="eDP-1",
    )
    document = tomllib.loads(runtime_config.render(settings, session))
    assert document.get("displays", []) == []


def test_independent_authoring_compiles_assignments_and_connector_rules(
    tmp_path: Path,
) -> None:
    settings, session = _session(
        tmp_path,
        display_assignments={"DP-1": "evening", "DP-2": "evening"},
    )
    settings = replace(
        settings,
        display_mode=config.DISPLAY_MODE_INDEPENDENT,
        theme_source_connector="DP-1",
    )
    session.schedules.add("evening", connector="DP-2", rule_id="dock")

    document = tomllib.loads(runtime_config.render(settings, session))

    assert document["settings"]["display_mode"] == "independent"
    assert document["settings"]["theme_source_connector"] == "DP-1"
    assert document["displays"] == [
        {"connector": "DP-1", "playlist": "evening"},
        {"connector": "DP-2", "playlist": "evening"},
    ]
    assert [(rule["id"], rule.get("connector", "")) for rule in document["schedules"]] == [
        ("night", ""),
        ("dock", "DP-2"),
    ]


def test_independent_authoring_requires_a_designated_theme_connector(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    settings = replace(settings, display_mode=config.DISPLAY_MODE_INDEPENDENT)

    with pytest.raises(runtime_config.RuntimeConfigError, match="designated theme source"):
        runtime_config.render(settings, session)


def test_connector_schedule_is_not_silently_emitted_as_global_in_mirrored_mode(
    tmp_path: Path,
) -> None:
    settings, session = _session(tmp_path)
    session.schedules.add("evening", connector="DP-2", rule_id="dock")

    document = tomllib.loads(runtime_config.render(settings, session))

    assert [rule["id"] for rule in document["schedules"]] == ["night"]


def test_favourites_only_filters_the_builtin_fallback_playlist(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    favourite = tmp_path / "video.mp4"
    session.favourites.add(favourite)

    document = tomllib.loads(
        runtime_config.render(replace(settings, cycle_favourites_only=True), session)
    )

    assert [entry["motion"] for entry in document["playlists"][0]["entries"]] == [str(favourite)]


def test_favourites_only_falls_back_to_everything_when_none_are_starred(
    tmp_path: Path,
) -> None:
    settings, session = _session(tmp_path)

    document = tomllib.loads(
        runtime_config.render(replace(settings, cycle_favourites_only=True), session)
    )

    assert len(document["playlists"][0]["entries"]) == 2


# -- Wallpaper Engine items: presets carry two optional fields, nothing else does --


def _workshop_session(
    tmp_path: Path, *extra: MediaItem
) -> tuple[config.Settings, Session, dict[str, MediaItem]]:
    """A library with a still, a true scene and a web wallpaper, plus ``extra``."""
    from wall_in_one.library.scan import WORKSHOP_PROVIDER

    root = tmp_path / "library"
    content = tmp_path / "steam" / "steamapps" / "workshop" / "content" / "431960"
    automatic = root / "Wall-in-One" / "Automatic Stills"

    def workshop_item(workshop_id: str, title: str, kind: str) -> MediaItem:
        return MediaItem(
            content / workshop_id,
            Kind.SCENE,
            7,
            1,
            provider=WORKSHOP_PROVIDER,
            scene=workshop_id,
            title=title,
            paired_still=automatic / f"{workshop_id}.png",
            workshop_type=kind,
        )

    items = {
        "still": MediaItem(root / "still.png", Kind.STILL, 7, 1),
        "scene": workshop_item("1647046763", "Toothless", "scene"),
        "web": workshop_item("2777242023", "Electric Eye", "web"),
    }
    for item in extra:
        items[item.title] = item
    settings = config.Settings(roots=(root,))
    library = Library(roots=(root,), items=tuple(items.values()))
    session = Session(settings, scanner=lambda _roots: library)
    session.refresh()
    return settings, session, items


def _preset_items(tmp_path: Path) -> tuple[MediaItem, ...]:
    from wall_in_one.library.model import Unplayable
    from wall_in_one.library.scan import WORKSHOP_PROVIDER

    content = tmp_path / "steam" / "steamapps" / "workshop" / "content" / "431960"
    automatic = tmp_path / "library" / "Wall-in-One" / "Automatic Stills"

    def item(workshop_id: str, title: str, kind: str) -> MediaItem:
        return MediaItem(
            content / workshop_id,
            Kind.SCENE,
            7,
            1,
            provider=WORKSHOP_PROVIDER,
            scene=workshop_id,
            title=title,
            # Even with a still on record, nothing that can't play is compiled.
            paired_still=automatic / f"{workshop_id}.png",
            workshop_type=kind,
        )

    return (
        replace(
            item("2946165131", "Y2K Matrix", "preset"),
            scene_base="2777242023",
            scene_properties=(
                ("arcs", "false"),
                ("arcsVisualsColor1", "0.000000 0.501961 0.000000"),
            ),
        ),
        replace(
            item("3000000001", "Rainmeter", "application"),
            unplayable=Unplayable.RUNTIME,
            unplayable_reason="linux-wallpaperengine can\u2019t play it",
        ),
        replace(
            item("3000000002", "Lonely preset", "preset"),
            scene_base="3999999999",
            unplayable=Unplayable.NEEDS_BASE,
            unplayable_reason="its base isn\u2019t installed",
        ),
    )


def _entries(document: str) -> list[dict[str, object]]:
    decoded = tomllib.loads(document)
    return [entry for playlist in decoded["playlists"] for entry in playlist["entries"]]


#: The keys a 0.4.0 service reads on an entry; it refuses any other.
RELEASED_ENTRY_KEYS = frozenset({"id", "kind", "still", "motion", "scene_id", "taboo", "palette"})


def test_a_preset_compiles_its_base_and_values_and_nothing_else_carries_them(
    tmp_path: Path,
) -> None:
    settings, session, _items = _workshop_session(tmp_path, *_preset_items(tmp_path))

    document = runtime_config.render(settings, session)
    entries = _entries(document)

    presets = [entry for entry in entries if entry.get("scene_id") == "2946165131"]
    assert presets and all(
        entry["scene_base"] == "2777242023"
        and entry["properties"]
        == {"arcs": "false", "arcsVisualsColor1": "0.000000 0.501961 0.000000"}
        for entry in presets
    )
    assert (
        'properties = { arcs = "false", arcsVisualsColor1 = "0.000000 0.501961 0.000000" }'
        in document.splitlines()
    )
    others = [entry for entry in entries if entry.get("scene_id") != "2946165131"]
    assert others and all(set(entry) <= RELEASED_ENTRY_KEYS for entry in others)
    assert {entry.get("scene_id") for entry in entries} == {
        None,
        "1647046763",
        "2777242023",
        "2946165131",
    }, "an item that can't play is never compiled, whatever still it has"


def test_wallpaper_engine_items_that_cant_play_say_why_and_never_compile(tmp_path: Path) -> None:
    _settings, session, items = _workshop_session(tmp_path, *_preset_items(tmp_path))

    for title, reason in (
        ("Rainmeter", "linux-wallpaperengine can\u2019t play it"),
        ("Lonely preset", "its base isn\u2019t installed"),
    ):
        can = runtime_config.entry_playability(items[title], session)
        assert can == runtime_config.Playability(False, False, reason)
    assert runtime_config.entry_playability(items["Y2K Matrix"], session).plays
    assert runtime_config.entry_playability(items["web"], session).plays


def test_scene_properties_out_of_bounds_are_never_compiled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset = _preset_items(tmp_path)[0]
    for properties in (
        (("-arcs", "true"),),
        (("arcs", "1\n--screen-root"),),
        (("arcs", "x" * 257),),
        (("arcs", "true"), ("arcs", "false")),
    ):
        settings, session, _items = _workshop_session(
            tmp_path, replace(preset, scene_properties=properties)
        )
        assert "2946165131" not in runtime_config.render(settings, session)
    from wall_in_one.library import workshop

    monkeypatch.setattr(workshop, "MAX_PROPERTIES", 1)
    settings, session, _items = _workshop_session(tmp_path, preset)
    assert "2946165131" not in runtime_config.render(settings, session)


#: What 0.4.0's compiler (38bc5ea) writes for `_workshop_session`'s playlists,
#: recorded by running that compiler over this fixture: a document without
#: presets must stay byte for byte this, so a 0.4.0 service keeps reading it.
#: `{root}` is the test's `tmp_path`; the others are entry ids, which hash it.
RELEASED_PLAYLISTS = """\
[[playlists]]
id = "all-media"
name = "All media"

[[playlists.entries]]
id = "{still}"
kind = "still"
still = "{root}/library/still.png"
palette = { kind = "adaptive", scheme = "m3-tonal-spot", mode = "keep" }

[[playlists.entries]]
id = "{scene}"
kind = "scene"
still = "{root}/library/Wall-in-One/Automatic Stills/1647046763.png"
scene_id = "1647046763"
palette = { kind = "adaptive", scheme = "m3-tonal-spot", mode = "keep" }

[[playlists.entries]]
id = "{web}"
kind = "scene"
still = "{root}/library/Wall-in-One/Automatic Stills/2777242023.png"
scene_id = "2777242023"
palette = { kind = "adaptive", scheme = "m3-tonal-spot", mode = "keep" }
"""


def test_a_document_without_presets_is_byte_for_byte_what_0_4_0_wrote(tmp_path: Path) -> None:
    settings, session, items = _workshop_session(tmp_path)

    document = runtime_config.render(settings, session)

    playlists_part = document[document.index("[[playlists]]") :]
    expected = RELEASED_PLAYLISTS.replace("{root}", str(tmp_path))
    for key in ("still", "scene", "web"):
        expected = expected.replace(
            f"{{{key}}}", runtime_config.entry_id_for_source(items[key].path)
        )
    assert playlists_part == expected


def test_executable_wallpaper_engine_items_never_reach_a_renderer(tmp_path: Path) -> None:
    """From the scan to the document: an `application` and a preset of one are listed,
    have a still on record, and still never become an entry the service would run."""
    from wall_in_one.library import scan, workshop
    from wall_in_one.library.model import Unplayable

    content = tmp_path / "steam" / "steamapps" / "workshop" / "content" / workshop.APP_ID
    for workshop_id, project in (
        ("100", {"type": "application", "title": "Trojan", "file": "run.exe"}),
        ("200", {"title": "Trojan preset", "dependency": "100", "preset": {"a": True}}),
        ("300", {"type": "web", "title": "Page", "file": "index.html"}),
    ):
        (content / workshop_id).mkdir(parents=True)
        (content / workshop_id / "project.json").write_text(json.dumps(project))
    root = tmp_path / "library"
    automatic = root / "Wall-in-One" / "Automatic Stills"
    automatic.mkdir(parents=True)
    for workshop_id in ("100", "200", "300"):
        (automatic / f"{workshop_id}.png").write_bytes(b"\x89PNG")
    # Web wallpapers on, so the web item stands for what does reach a renderer.
    settings = config.Settings(roots=(root,), allow_web_wallpapers=True)
    session = Session(
        settings,
        scanner=lambda roots: scan.scan(
            roots, include_workshop=True, workshop_roots=(tmp_path / "steam",), records={}
        ),
    )
    session.refresh()
    listed = {item.scene: item for item in session.library.items}
    assert listed["100"].unplayable is Unplayable.RUNTIME
    assert listed["200"].unplayable is Unplayable.RUNTIME
    assert listed["100"].paired_still == automatic / "100.png"

    entries = _entries(runtime_config.render(settings, session))

    assert {entry.get("scene_id") for entry in entries} == {"300"}
    assert not any(entry.get("scene_base") == "100" for entry in entries)


# -- presets need schema 6, and an older live service gets the rest (review of r11, 2) --


def _live_service(monkeypatch: pytest.MonkeyPatch, schemas: list[int]) -> None:
    """The wallpaper service answering status, reading ``schemas``."""
    from wall_in_one.control import client
    from wall_in_one.control.protocol import Response

    status = {"status_version": 2, "supported_config_schemas": schemas}

    def send(verb: str, *, timeout: float) -> Response:
        assert verb == "status"
        return Response(ok=True, message=json.dumps(status))

    monkeypatch.setattr(client, "send_runtime", send)


def test_only_a_document_with_a_preset_is_schema_6(tmp_path: Path) -> None:
    settings, session, _items = _workshop_session(tmp_path)
    assert tomllib.loads(runtime_config.render(settings, session))["schema_version"] == 4
    battery = replace(settings, stop_animations_on_battery=True)
    assert tomllib.loads(runtime_config.render(battery, session))["schema_version"] == 5

    settings, session, _items = _workshop_session(tmp_path, *_preset_items(tmp_path))
    assert tomllib.loads(runtime_config.render(settings, session))["schema_version"] == 6
    both = tomllib.loads(
        runtime_config.render(replace(settings, stop_animations_on_battery=True), session)
    )
    assert both["schema_version"] == 6
    assert both["settings"]["stop_animations_on_battery"] is True
    without = runtime_config.render(settings, session, presets=False)
    assert tomllib.loads(without)["schema_version"] == 4
    assert "scene_base" not in without and "properties = " not in without


@pytest.mark.parametrize("writer", ("publish_changes", "write"))
def test_an_older_live_service_gets_everything_but_the_presets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    """0.4.0 refuses a document with `properties` whole; it gets one without, never nothing."""
    from wall_in_one import paths

    settings, session, _items = _workshop_session(tmp_path, *_preset_items(tmp_path))
    _live_service(monkeypatch, [4, 5])

    if writer == "write":
        runtime_config.write(settings, session)
        left_out = runtime_config.compiled(settings, session, presets=False).left_out
    else:
        left_out = runtime_config.publish_changes(settings, session).left_out

    document = paths.runtime_config_path().read_text(encoding="utf-8")
    assert tomllib.loads(document)["schema_version"] == 4
    assert "scene_base" not in document and "properties = " not in document
    assert {entry.get("scene_id") for entry in _entries(document)} == {
        None,
        "1647046763",
        "2777242023",
    }, "everything else is published"
    assert left_out == (
        runtime_config.LeftOut(
            runtime_config.PRESET_OWNER,
            "2946165131",
            "Y2K Matrix",
            "",
            runtime_config.NEEDS_NEWER_SERVICE,
        ),
    )
    assert runtime_config.left_out_notice(left_out) == (
        "The Wallpaper Engine preset \u201cY2K Matrix\u201d isn\u2019t played until the "
        "wallpaper service is restarted after the update: the one running doesn\u2019t read "
        "presets."
    )
    assert "restart it to play presets" in left_out[0].warning()


def test_presets_left_out_are_said_in_one_sentence() -> None:
    presets = [
        runtime_config.LeftOut(
            runtime_config.PRESET_OWNER, workshop_id, title, "", runtime_config.NEEDS_NEWER_SERVICE
        )
        for workshop_id, title in (("1", "A"), ("2", "B"))
    ]
    assert runtime_config.left_out_notice(presets) == (
        "2 Wallpaper Engine presets aren\u2019t played until the wallpaper service is "
        "restarted after the update: the one running doesn\u2019t read presets."
    )


def test_a_live_service_that_reads_presets_gets_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one import paths

    settings, session, _items = _workshop_session(tmp_path, *_preset_items(tmp_path))
    _live_service(monkeypatch, [4, 5, 6])

    published = runtime_config.publish_changes(settings, session)

    document = paths.runtime_config_path().read_text(encoding="utf-8")
    assert tomllib.loads(document)["schema_version"] == 6
    assert 'scene_base = "2777242023"' in document
    assert published.left_out == ()


def test_the_health_sync_compiles_what_was_published_for_its_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its generation check must not mistake a document published without presets for stale."""
    from wall_in_one import paths

    settings, session, _items = _workshop_session(tmp_path, *_preset_items(tmp_path))
    for schemas in ([4, 5], [4, 5, 6]):
        _live_service(monkeypatch, schemas)
        runtime_config.publish_changes(settings, session)
        status = {"status_version": 2, "supported_config_schemas": schemas}
        rebuilt = runtime_config.render(
            settings, session, presets=runtime_config.presets_supported(status)
        )
        assert runtime_config.document_generation(rebuilt) == (
            runtime_config.read_config_generation(paths.runtime_config_path())
        ), schemas
    assert not runtime_config.presets_supported({"status_version": 2})
    assert not runtime_config.presets_supported(
        {"status_version": 2, "supported_config_schemas": ["6"]}
    )
    assert not runtime_config.presets_supported(None)


def test_a_scene_id_that_is_not_ascii_digits_is_never_compiled(tmp_path: Path) -> None:
    """`str.isdigit` takes "\u0661\u0662\u0663"; the service and Steam don't."""
    _settings, _session, items = _workshop_session(tmp_path)
    eastern = replace(items["scene"], scene="\u0661\u0662\u0663", title="Eastern")
    settings, session, _items = _workshop_session(tmp_path, eastern)

    entries = _entries(runtime_config.render(settings, session))

    assert "\u0661\u0662\u0663" not in {entry.get("scene_id") for entry in entries}


def test_a_playlist_of_only_presets_is_said_as_waiting_for_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not "has nothing to play": it has presets, which the running service doesn't read yet."""
    from wall_in_one import paths

    _settings, _session, items = _workshop_session(tmp_path, *_preset_items(tmp_path))
    preset = items["Y2K Matrix"]
    only = playlists.Playlist(
        id="presets", name="Presets", entries=(playlists.Entry(id="y2k", source=str(preset.path)),)
    )
    settings = config.Settings(roots=(tmp_path / "library",), active_playlist="presets")
    library = Library(roots=(tmp_path / "library",), items=tuple(items.values()))
    session = Session(
        settings,
        scanner=lambda _roots: library,
        playlist_store=playlists.Store({only.id: only}),
    )
    session.refresh()
    _live_service(monkeypatch, [4, 5])

    left_out = runtime_config.publish_changes(settings, session).left_out

    document = tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))
    assert document["default_playlist"] == "all-media"
    assert set(left_out) == {
        runtime_config.LeftOut(
            runtime_config.DEFAULT_OWNER,
            "",
            "presets",
            "Presets",
            runtime_config.NEEDS_NEWER_SERVICE,
        ),
        runtime_config.LeftOut(
            runtime_config.PRESET_OWNER,
            "2946165131",
            "Y2K Matrix",
            "",
            runtime_config.NEEDS_NEWER_SERVICE,
        ),
    }
    notice = runtime_config.left_out_notice(left_out)
    assert "has nothing to play" not in notice
    assert (
        "The default names \u201cPresets\u201d, which holds only Wallpaper Engine presets the "
        "running wallpaper service doesn\u2019t read yet, so \u201cAll media\u201d plays instead."
    ) in notice
