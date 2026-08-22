from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, favourites, pairings, playlists, schedules
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.session import Session


@pytest.mark.parametrize("old_schema", (1, 2))
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
    assert document["schema_version"] == 3
    assert "upgrade_fixture" not in document
    assert document["playlists"][0]["entries"][0]["still"] == str(media / "wallpaper.png")
    assert "wall_in_one.ui.app" not in sys.modules


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


@pytest.mark.parametrize(
    ("settings_text", "message"),
    (
        ('cycle_enabled = "yes"\n', "cycle_enabled must be a boolean"),
        ("cycle_interval = 2.5\n", "cycle_interval must be an integer"),
        ("cycle_interval = 2\n", "cycle_interval must be between"),
        ("opacity = nan\n", "opacity must be a finite number"),
        ('video_interpolation = "magic"\n', "video_interpolation must be one of"),
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
    previous = 'schema_version = 3\nlast_known_good = "preserve me"\n'
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
    previous = 'schema_version = 3\nlast_known_good = "preserve me"\n'
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
    previous = 'schema_version = 2\nlast_known_good = "preserve me"\n'
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
    previous = 'schema_version = 3\nlast_known_good = "preserve me"\n'
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
    assert document["schema_version"] == 3
    assert Path(document["renderer"]["niri_program"]).is_absolute()
    assert document["renderer"]["scene_fps"] == 75
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
    assert document["displays"] == [{"connector": "DP-1", "playlist": "evening"}]


def test_compiler_uses_a_pairing_specific_adaptive_generator(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    still = session.library.items[0]
    session.pairings.choose_palette(still, pairings.PalettePolicy(pairings.ADAPTIVE, "m3-rainbow"))

    document = tomllib.loads(runtime_config.render(settings, session))

    assert document["playlists"][0]["entries"][0]["palette"]["scheme"] == "m3-rainbow"


def test_compiler_write_is_atomic_and_leaves_no_temporary(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "state" / "runtime.toml"
    assert runtime_config.write(settings, session, target) == target
    assert tomllib.loads(target.read_text())["schema_version"] == 3
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
    previous = 'schema_version = 3\nlast_known_good = "preserve me"\n'
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
    previous = 'schema_version = 3\nlast_known_good = "preserve me"\n'
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
    previous = 'schema_version = 3\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")
    try:
        with pytest.raises(runtime_config.RuntimeConfigError, match=message):
            runtime_config.update(settings, session, target)
    finally:
        session.shutdown()
    assert target.read_text(encoding="utf-8") == previous


def test_compiler_rejects_schedule_and_display_strings_outside_wire_bounds(
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
    previous = 'schema_version = 3\nlast_known_good = "preserve me"\n'
    target.write_text(previous, encoding="utf-8")

    with pytest.raises(runtime_config.RuntimeConfigError, match="schedule id must be at most"):
        runtime_config.update(settings, session, target)
    assert target.read_text(encoding="utf-8") == previous

    # Remove the oversized rule so the next emitted field is examined.
    session.schedules.remove(session.schedules.rules[-1].id)
    with pytest.raises(
        runtime_config.RuntimeConfigError, match="display connector must be at most"
    ):
        runtime_config.update(settings, session, target)
    assert target.read_text(encoding="utf-8") == previous


def test_legacy_single_output_becomes_a_resolved_display_assignment(tmp_path: Path) -> None:
    settings, session = _session(tmp_path, display_assignments={})
    settings = config.Settings(
        roots=settings.roots,
        active_playlist="evening",
        output="eDP-1",
    )
    document = tomllib.loads(runtime_config.render(settings, session))
    assert document["displays"] == [{"connector": "eDP-1", "playlist": "evening"}]


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
