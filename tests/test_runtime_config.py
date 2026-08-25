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

    def load_under_lock() -> config.Settings:
        assert entered
        raise config.ConfigError("authoring fixture stops here")

    monkeypatch.setattr(runtime_config, "compiler_lock", ObservedLock)
    monkeypatch.setattr(config, "load_strict", load_under_lock)

    assert cli.main(["--write-config"]) == 1
    assert "authoring fixture stops here" in capsys.readouterr().err
    assert not entered


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

    assert cli.main(["--write-config"]) == 1
    assert "cannot parse" in capsys.readouterr().err
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
        ('video_interpolation = "magic"\n', "video_interpolation must be one of"),
        ('scene_scaling = "zoom"\n', "scene_scaling must be one of"),
        ('scene_clamp = "mirror"\n', "scene_clamp must be one of"),
        ('roots = ["/valid", 7]\n', "roots must be an array"),
        ("future_setting = true\n", "unknown setting"),
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

    assert cli.main(["--write-config"]) == 1
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

    assert cli.main(["--write-config"]) == 1
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
    assert Path(document["renderer"]["niri_program"]).is_absolute()
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


def test_playlist_with_no_resolved_entries_preserves_last_known_good(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    empty = session.playlists.create("Empty")
    session.playlists.add(empty.id, tmp_path / "not-in-library.mp4")
    settings = config.Settings(roots=settings.roots, active_playlist=empty.id)
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 4\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    with pytest.raises(runtime_config.RuntimeConfigError, match=r"playlist 'Empty'.*all 1"):
        runtime_config.update(settings, session, target)

    assert target.read_text(encoding="utf-8") == previous


def test_an_unreferenced_empty_playlist_remains_a_safe_authoring_draft(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    draft = session.playlists.create("Draft")

    document = tomllib.loads(runtime_config.render(settings, session))

    assert draft.id not in {playlist["id"] for playlist in document["playlists"]}


def test_a_schedule_cannot_silently_lose_its_empty_playlist(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    draft = session.playlists.create("Draft")
    session.schedules.add(draft.id, rule_id="draft-rule")
    target = tmp_path / "runtime.toml"
    previous = 'schema_version = 4\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    with pytest.raises(
        runtime_config.RuntimeConfigError,
        match=r"schedule 'draft-rule'.*playlist 'Draft'.*no playable entries",
    ):
        runtime_config.update(settings, session, target)

    assert target.read_text(encoding="utf-8") == previous


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
