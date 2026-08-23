from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config
from wall_in_one.library import (
    favourites,
    pairing,
    pairings,
    playlists,
    removals,
    scan,
    schedules,
)
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.session import Session
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper.applier import Applier, ApplyError
from wall_in_one.wallpaper.renderer import RendererError


class FakeRenderer:
    def __init__(self) -> None:
        self.started: list[Path] = []
        self.stops = 0

    def start(self, video: Path) -> None:
        self.started.append(video)

    def stop(self) -> None:
        self.stops += 1


def _still(name: str) -> MediaItem:
    return MediaItem(path=Path(f"/w/{name}.png"), kind=Kind.STILL, size=1, mtime=0)


def _video(name: str, *, paired: bool = True) -> MediaItem:
    return MediaItem(
        path=Path(f"/w/{name}.mp4"),
        kind=Kind.VIDEO,
        size=1,
        mtime=0,
        paired_still=Path(f"/w/{name}-still.png") if paired else None,
    )


@pytest.fixture
def applied_paths(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def record(path: Path, connector: str | None = None) -> None:
        calls.append(Path(path))

    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", record)
    # Applying also settles the palette now, and `conftest` refuses the live
    # calls -- so a test that means to apply has to say it means all of it.
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_scheme", lambda _selection: None)
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_mode", lambda _mode: None)
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.current_wallpaper",
        lambda: (_ for _ in ()).throw(noctalia.NoctaliaUnavailableError("no shell")),
    )
    return calls


def _session(items: Sequence[MediaItem], **overrides: object) -> Session:
    settings = replace(config.Settings(), **overrides)  # type: ignore[arg-type]
    library = Library(roots=(Path("/w"),), items=tuple(items))
    return Session(
        settings.validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda roots: library,
        rng=random.Random(11),
    )


def test_refresh_builds_the_playlist(applied_paths: list[Path]) -> None:
    session = _session([_still("a"), _still("b")])
    session.refresh()
    assert len(session.playlist) == 2


def test_navigation_applies_wallpapers(applied_paths: list[Path]) -> None:
    session = _session([_still("a"), _still("b"), _still("c")])
    session.refresh()

    assert session.next().path == Path("/w/b.png")
    assert session.previous().path == Path("/w/a.png")
    assert applied_paths == [Path("/w/b.png"), Path("/w/a.png")]


def test_navigation_and_direct_choice_never_apply_a_borked_wallpaper(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    items = [_still("a"), _video("b"), _still("c")]
    health = pairings.Store(path=tmp_path / "pairings.json")
    lists = playlists.Store(path=tmp_path / "playlists.json")
    health.mark_borked(items[1], "decoder crashed", "renderer-crash")
    session = Session(
        config.Settings(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda _roots: Library(roots=(Path("/w"),), items=tuple(items)),
        pairing_store=health,
        playlist_store=lists,
        rng=random.Random(4),
    )
    session.refresh()

    assert session.next().item == items[2]
    assert session.previous().item == items[0]
    for _ in range(12):
        assert session.random().item != items[1]
    with pytest.raises(ApplyError, match="marked Borked and cannot play"):
        session.choose(items[1].path)

    assert lists.get("quick-choice") is None
    assert items[1].path not in applied_paths


def test_new_health_marker_blocks_play_and_next_skips_the_current_crasher(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    items = [_video("crasher"), _still("safe")]
    health = pairings.Store(path=tmp_path / "pairings.json")
    session = Session(
        config.Settings(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda _roots: Library(roots=(Path("/w"),), items=tuple(items)),
        pairing_store=health,
    )
    session.refresh()
    health.mark_borked(items[0], "renderer exited", "renderer-crash")

    with pytest.raises(ApplyError, match="marked Borked and cannot play"):
        session.apply_current()
    assert session.next().item == items[1]
    assert applied_paths == [items[1].path]


def test_all_borked_playlist_is_refused_without_replacing_the_manual_choice(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    safe, borked = _still("safe"), _video("crasher")
    health = pairings.Store(path=tmp_path / "pairings.json")
    lists = playlists.Store(path=tmp_path / "playlists.json")
    safe_list = lists.create("Safe")
    broken_list = lists.create("Broken")
    lists.add(safe_list.id, safe.path)
    lists.add(broken_list.id, borked.path)
    health.mark_borked(borked, "renderer exited", "renderer-crash")
    session = Session(
        config.Settings(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda _roots: Library(roots=(Path("/w"),), items=(safe, borked)),
        pairing_store=health,
        playlist_store=lists,
    )
    session.refresh()
    session.use_playlist(safe_list.id)

    with pytest.raises(ApplyError, match="every item is marked Borked"):
        session.use_playlist(broken_list.id)

    assert session.manual_playlist == safe_list.id
    assert session.cursor == safe
    assert applied_paths == []


def test_selecting_media_plays_through_a_visible_quick_choice_playlist(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    items = [_still("a"), _still("b")]
    store = playlists.Store(path=tmp_path / "playlists.json")
    library = Library(roots=(Path("/w"),), items=tuple(items))
    session = Session(
        config.Settings(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda _roots: library,
        playlist_store=store,
    )
    session.refresh()

    session.select(items[1].path)

    quick = store.get("quick-choice")
    assert quick is not None and quick.name == "Quick choice"
    assert [entry.path for entry in quick.entries] == [items[1].path]
    assert session.manual_playlist == quick.id
    assert session.current is not None and session.current.item.path == items[1].path


def test_choosing_media_authors_quick_choice_without_applying_it(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    items = [_still("a"), _still("b")]
    store = playlists.Store(path=tmp_path / "playlists.json")
    session = Session(
        config.Settings(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda _roots: Library(roots=(Path("/w"),), items=tuple(items)),
        playlist_store=store,
    )
    session.refresh()

    chosen = session.choose(items[1].path)

    assert chosen.id == "quick-choice"
    assert session.manual_playlist == chosen.id
    assert applied_paths == []


def test_a_manual_playlist_overrides_then_releases_the_schedule(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    items = [_still("day"), _still("night")]
    lists = playlists.Store(path=tmp_path / "playlists.json")
    day = lists.create("Day", entry_id="day")
    night = lists.create("Night", entry_id="night")
    lists.add(day.id, items[0].path)
    lists.add(night.id, items[1].path)
    calendar = schedules.Store(path=tmp_path / "schedules.json")
    calendar.add(night.id, rule_id="always")
    library = Library(roots=(Path("/w"),), items=tuple(items))
    session = Session(
        replace(config.Settings(), active_playlist=day.id),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda _roots: library,
        playlist_store=lists,
        schedule_store=calendar,
    )
    session.refresh()
    assert session.active_playlist() == night.id

    session.use_playlist(day.id)
    assert session.active_playlist() == day.id
    assert [item.path for item in session.playlist.items] == [items[0].path]

    session.resume_schedule()
    assert session.manual_playlist is None
    assert session.active_playlist() == night.id
    assert [item.path for item in session.playlist.items] == [items[1].path]


def test_an_empty_library_says_so_instead_of_crashing(applied_paths: list[Path]) -> None:
    session = _session([])
    session.refresh()
    with pytest.raises(ApplyError, match="empty"):
        session.next()


def test_dynamics_off_drops_unpaired_videos_from_the_rotation(
    applied_paths: list[Path],
) -> None:
    session = _session([_still("a"), _video("paired"), _video("lonely", paired=False)])
    session.refresh()
    assert len(session.playlist) == 3

    session.update_settings(replace(session.settings, dynamics_enabled=False))

    assert len(session.playlist) == 2
    assert Path("/w/lonely.mp4") not in [item.path for item in session.playlist.items]


def test_pausing_dynamics_on_an_unpaired_video_falls_back_to_a_still(
    applied_paths: list[Path],
) -> None:
    """Caught live: this used to raise, leaving dynamics=off with the video still up."""
    session = _session([_still("a"), _video("lonely", paired=False)])
    session.refresh()
    session.select(Path("/w/lonely.mp4"))
    current = session.current
    assert current is not None and current.animated

    session.update_settings(replace(session.settings, dynamics_enabled=False))

    current = session.current
    assert current is not None
    assert not current.animated
    assert current.path == Path("/w/a.png")


def test_pausing_dynamics_with_nothing_else_to_show_is_still_not_an_error(
    applied_paths: list[Path],
) -> None:
    session = _session([_video("lonely", paired=False)])
    session.refresh()
    session.apply_current()

    session.update_settings(replace(session.settings, dynamics_enabled=False))

    assert session.current is None
    assert len(session.playlist) == 0


def test_dynamics_back_on_restores_them(applied_paths: list[Path]) -> None:
    session = _session([_video("lonely", paired=False)], dynamics_enabled=False)
    session.refresh()
    assert len(session.playlist) == 0

    session.update_settings(replace(session.settings, dynamics_enabled=True))
    assert len(session.playlist) == 1


def test_pausing_dynamics_swaps_a_playing_video_for_its_still(
    applied_paths: list[Path],
) -> None:
    session = _session([_video("clip")])
    session.refresh()
    playing = session.apply_current()
    assert playing.animated

    session.update_settings(replace(session.settings, dynamics_enabled=False))

    current = session.current
    assert current is not None
    assert not current.animated
    assert current.path == Path("/w/clip-still.png")


def test_shuffle_setting_reaches_the_playlist(applied_paths: list[Path]) -> None:
    session = _session([_still(str(index)) for index in range(6)])
    session.refresh()
    assert not session.playlist.shuffle

    session.update_settings(replace(session.settings, shuffle=True))
    assert session.playlist.shuffle


def test_sync_survives_noctalia_being_absent(applied_paths: list[Path]) -> None:
    session = _session([_still("a")])
    session.refresh()
    assert session.sync_with_noctalia() is False


def test_sync_moves_the_cursor_onto_the_live_wallpaper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.set_wallpaper", lambda path, connector=None: None
    )
    monkeypatch.setattr("wall_in_one.theme.noctalia.current_wallpaper", lambda: Path("/w/c.png"))

    session = _session([_still("a"), _still("b"), _still("c")])
    session.refresh()

    assert session.sync_with_noctalia() is True
    assert session.playlist.current() is not None
    assert session.playlist.current().path == Path("/w/c.png")  # type: ignore[union-attr]


def test_the_cursor_is_set_before_anything_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the grid highlights at startup.

    Nothing has been applied through us yet, so `current` is empty -- but the
    wallpaper on screen is still the one the user expects to see marked.
    """
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.set_wallpaper", lambda path, connector=None: None
    )
    monkeypatch.setattr("wall_in_one.theme.noctalia.current_wallpaper", lambda: Path("/w/b.png"))

    session = _session([_still("a"), _still("b")])
    session.refresh()
    session.sync_with_noctalia()

    assert session.current is None
    cursor = session.cursor
    assert cursor is not None
    assert cursor.path == Path("/w/b.png")


def test_the_cursor_follows_navigation(applied_paths: list[Path]) -> None:
    session = _session([_still("a"), _still("b")])
    session.refresh()
    applied = session.next()

    cursor = session.cursor
    assert cursor is not None
    assert cursor.path == applied.item.path


def test_sync_recognises_a_videos_paired_still(monkeypatch: pytest.MonkeyPatch) -> None:
    """Noctalia reports the still we set underneath, not the video itself."""
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.set_wallpaper", lambda path, connector=None: None
    )
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.current_wallpaper", lambda: Path("/w/clip-still.png")
    )

    session = _session([_still("a"), _video("clip")])
    session.refresh()

    assert session.sync_with_noctalia() is True
    assert session.playlist.current().path == Path("/w/clip.mp4")  # type: ignore[union-attr]


def test_select_rejects_something_not_in_the_library(applied_paths: list[Path]) -> None:
    session = _session([_still("a")])
    session.refresh()
    with pytest.raises(ApplyError, match="not in the library"):
        session.select(Path("/w/nope.png"))


def test_describe_reports_the_real_state(applied_paths: list[Path]) -> None:
    session = _session([_still("a"), _still("b")], shuffle=True, cycle_enabled=True)
    session.refresh()
    session.apply_current()

    described = session.describe()

    assert "2 of 2 playable" in described
    assert "shuffle=on" in described
    assert "cycle=on" in described
    assert "dynamics=on" in described


def test_describe_before_anything_is_applied(applied_paths: list[Path]) -> None:
    session = _session([_still("a")])
    session.refresh()
    assert "nothing applied" in session.describe()


def test_a_renderer_failure_surfaces_as_an_apply_error(
    applied_paths: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(FakeRenderer):
        def start(self, video: Path) -> None:
            raise RendererError("mpvpaper died")

    settings = config.Settings()
    library = Library(roots=(Path("/w"),), items=(_video("clip"),))
    session = Session(
        settings,
        applier=Applier(Broken()),  # type: ignore[arg-type]
        scanner=lambda roots: library,
    )
    session.refresh()

    with pytest.raises(ApplyError, match="mpvpaper died"):
        session.apply_current()


def test_shutdown_stops_the_renderer(applied_paths: list[Path]) -> None:
    fake = FakeRenderer()
    library = Library(roots=(Path("/w"),), items=(_still("a"),))
    session = Session(
        config.Settings(),
        applier=Applier(fake),  # type: ignore[arg-type]
        scanner=lambda roots: library,
    )
    session.shutdown()
    assert fake.stops >= 1


# -- configured roots -----------------------------------------------------


def test_a_rescan_uses_the_configured_roots(applied_paths: list[Path]) -> None:
    """Resolved in `refresh` rather than at each call site, so that every path
    into a rescan honours the setting without having to remember to."""
    asked: list[object] = []

    def scanner(roots: Sequence[Path] | None) -> Library:
        asked.append(roots)
        return Library(roots=(), items=())

    session = Session(
        replace(config.Settings(), roots=(Path("/one"), Path("/two"))).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=scanner,
    )
    session.refresh()
    assert asked == [(Path("/one"), Path("/two"))]


def test_no_configured_roots_scans_nothing_until_the_user_chooses(
    applied_paths: list[Path],
) -> None:
    """Noctalia detection is a prompt suggestion, not implicit consent."""
    asked: list[object] = []

    def scanner(roots: Sequence[Path] | None) -> Library:
        asked.append(roots)
        return Library(roots=(), items=())

    session = Session(
        config.Settings().validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=scanner,
    )
    request = session.prepare_scan()
    session.adopt_library(request.run())
    assert asked == [()]
    assert request.include_workshop is False
    assert request.workshop_roots == ()


def test_an_explicit_root_still_wins_over_the_configured_ones(
    applied_paths: list[Path],
) -> None:
    asked: list[object] = []

    def scanner(roots: Sequence[Path] | None) -> Library:
        asked.append(roots)
        return Library(roots=(), items=())

    session = Session(
        replace(config.Settings(), roots=(Path("/one"),)).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=scanner,
    )
    session.refresh([Path("/elsewhere")])
    assert asked == [(Path("/elsewhere"),)]


def test_changing_the_roots_rescans_immediately(applied_paths: list[Path]) -> None:
    """Otherwise a folder the user just added stays invisible until relaunch."""
    scans = 0

    def scanner(_roots: Sequence[Path] | None) -> Library:
        nonlocal scans
        scans += 1
        return Library(roots=(), items=())

    session = Session(
        config.Settings().validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=scanner,
    )
    session.refresh()
    assert scans == 1
    session.update_settings(replace(session.settings, roots=(Path("/new"),)))
    assert scans == 2


def test_changing_workshop_inclusion_rescans_immediately(applied_paths: list[Path]) -> None:
    """Workshop is a library source just like a configured filesystem root."""
    scans = 0

    def scanner(_roots: Sequence[Path] | None) -> Library:
        nonlocal scans
        scans += 1
        return Library(roots=(), items=())

    session = Session(
        replace(config.Settings(), scan_workshop=False).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=scanner,
    )
    session.refresh()
    assert scans == 1
    session.update_settings(replace(session.settings, scan_workshop=True))
    assert scans == 2


def test_scan_request_snapshots_sources_before_worker_runs(
    applied_paths: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending GUI scan cannot observe roots changed after submission."""
    asked: list[Sequence[Path] | None] = []

    def scanner(roots: Sequence[Path] | None) -> Library:
        asked.append(roots)
        return Library(roots=tuple(roots or ()), items=())

    session = Session(
        replace(config.Settings(), roots=(Path("/first"),), scan_workshop=True).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=scanner,
    )
    monkeypatch.setattr(
        "wall_in_one.session.workshop.steam_roots",
        lambda: (Path("/steam-at-submit"),),
    )
    request = session.prepare_scan()
    session.update_settings(
        replace(session.settings, roots=(Path("/second"),), scan_workshop=False),
        rescan_library=False,
    )

    scanned = request.run()

    assert asked == [(Path("/first"),)]
    assert scanned.roots == (Path("/first"),)
    assert request.workshop_roots == (Path("/steam-at-submit"),)


def test_settings_that_do_not_touch_the_roots_do_not_rescan(
    applied_paths: list[Path],
) -> None:
    scans = 0

    def scanner(_roots: Sequence[Path] | None) -> Library:
        nonlocal scans
        scans += 1
        return Library(roots=(), items=())

    session = Session(
        config.Settings().validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=scanner,
    )
    session.refresh()
    session.update_settings(replace(session.settings, opacity=0.5))
    assert scans == 1


# -- video playback settings ----------------------------------------------


class AudioRenderer(FakeRenderer):
    """A FakeRenderer that remembers what it was retuned to."""

    def __init__(self) -> None:
        super().__init__()
        self.audio: list[tuple[bool, int]] = []
        self.when_hidden = "pause"
        self.hardware_decode = True
        self.interpolation = "off"

    def apply_audio(self, *, muted: bool, volume: int) -> None:
        self.audio.append((muted, volume))


def _audio_session(**overrides: object) -> tuple[Session, AudioRenderer]:
    fake = AudioRenderer()
    settings = replace(config.Settings(), **overrides)  # type: ignore[arg-type]
    session = Session(
        settings.validated(),
        applier=Applier(fake),  # type: ignore[arg-type]
        scanner=lambda _roots: Library(roots=(), items=()),
    )
    return session, fake


def test_unmuting_retunes_the_video_already_playing(applied_paths: list[Path]) -> None:
    """Over mpv's IPC. Restarting mpvpaper to change the volume would blink
    the wallpaper, which is a ludicrous price for a slider."""
    session, fake = _audio_session()
    session.update_settings(replace(session.settings, video_muted=False))
    assert fake.audio == [(False, 100)]


def test_changing_the_volume_retunes_it_too(applied_paths: list[Path]) -> None:
    session, fake = _audio_session(video_muted=False)
    session.update_settings(replace(session.settings, video_volume=30))
    assert fake.audio == [(False, 30)]


def test_settings_that_leave_the_audio_alone_do_not_retune(applied_paths: list[Path]) -> None:
    session, fake = _audio_session()
    session.update_settings(replace(session.settings, opacity=0.5))
    assert fake.audio == []


def test_the_hidden_policy_is_recorded_for_the_next_video(applied_paths: list[Path]) -> None:
    """It is an mpvpaper launch flag, not an mpv property, so it cannot be
    retuned live -- and restarting the wallpaper under someone to apply it
    would be worse than waiting."""
    session, fake = _audio_session()
    session.update_settings(replace(session.settings, video_when_hidden="stop"))
    assert fake.when_hidden == "stop"


def test_launch_time_video_options_are_recorded_for_the_next_video(
    applied_paths: list[Path],
) -> None:
    session, fake = _audio_session()
    session.update_settings(
        replace(
            session.settings,
            video_hardware_decode=False,
            video_interpolation="oversample",
        )
    )
    assert fake.hardware_decode is False
    assert fake.interpolation == "oversample"


def test_the_scene_frame_rate_is_recorded_for_the_scene_backend(
    applied_paths: list[Path],
) -> None:
    session, _fake = _audio_session()
    session.update_settings(replace(session.settings, scene_fps=75))
    assert session._applier.scenes.fps == 75


def test_a_session_that_builds_its_own_renderer_carries_the_settings() -> None:
    """`when_hidden` becomes a command-line flag, so it has to be right before
    the first video starts, not pushed afterwards."""
    settings = replace(
        config.Settings(),
        video_muted=False,
        video_volume=25,
        video_when_hidden="stop",
        video_hardware_decode=False,
        video_interpolation="linear",
        scene_fps=75,
    ).validated()
    built = Session(settings, scanner=lambda _roots: Library(roots=(), items=()))
    assert built._applier.renderer.muted is False
    assert built._applier.renderer.volume == 25
    assert built._applier.renderer.when_hidden == "stop"
    assert built._applier.renderer.hardware_decode is False
    assert built._applier.renderer.interpolation == "linear"
    assert built._applier.scenes.fps == 75
    built.shutdown()


def test_an_applier_handed_in_is_left_as_its_owner_configured_it() -> None:
    fake = AudioRenderer()
    fake.when_hidden = "play"
    settings = replace(config.Settings(), video_when_hidden="stop").validated()
    Session(settings, applier=Applier(fake), scanner=lambda _roots: Library(roots=(), items=()))  # type: ignore[arg-type]
    assert fake.when_hidden == "play"


def test_confirmed_workshop_uninstall_forgets_authoring_and_generated_still(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    content = tmp_path / "steamapps" / "workshop" / "content" / "431960"
    scene_path = content / "42"
    scene_path.mkdir(parents=True)
    scene = MediaItem(
        path=scene_path,
        kind=Kind.SCENE,
        size=1,
        mtime=1,
        provider=scan.WORKSHOP_PROVIDER,
        scene="42",
    )
    pairing_store = pairings.Store(path=tmp_path / "pairings.json")
    favourite_store = favourites.Store(path=tmp_path / "favourites.json")
    playlist_store = playlists.Store(path=tmp_path / "playlists.json")
    session = Session(
        replace(config.Settings(), roots=(root,), scan_workshop=True).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=pairing_store,
        favourite_store=favourite_store,
        playlist_store=playlist_store,
    )
    session.adopt_library(Library(roots=(root,), items=(scene,)))
    custom = tmp_path / "my-scene-still.png"
    custom.write_bytes(b"custom")
    pairing_store.choose_still(scene, custom)
    pairing_store.mark_borked(scene, "engine crashed", "renderer-crash")
    favourite_store.add(scene.path)
    playlist = playlist_store.create("Scenes")
    playlist_store.add(playlist.id, scene.path)
    generated = pairing.still_directory(root) / "42.png"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"automatic")

    scene_path.rmdir()
    session.adopt_library(Library(roots=(root,), items=()))

    assert session.removed_workshop == (scene,)
    assert pairing_store.get(pairings.Identity.of(scene)) is None
    assert scene.path not in favourite_store.paths
    assert playlist_store.get(playlist.id) is not None
    assert playlist_store.get(playlist.id).entries == ()  # type: ignore[union-attr]
    assert not generated.exists()
    assert custom.is_file(), "a chosen representative is the user's file"

    # The stable Workshop id is no longer poisoned after reinstall.
    scene_path.mkdir()
    session.adopt_library(Library(roots=(root,), items=(scene,)))
    assert not pairing_store.resolve(scene, (root,)).health.is_borked
    session.shutdown()


@pytest.mark.parametrize("source_present", [True, False])
def test_workshop_omission_is_not_an_uninstall_without_both_safety_signals(
    tmp_path: Path,
    source_present: bool,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    content = tmp_path / "steamapps" / "workshop" / "content" / "431960"
    scene_path = content / "42"
    scene_path.mkdir(parents=True)
    scene = MediaItem(
        path=scene_path,
        kind=Kind.SCENE,
        size=1,
        mtime=1,
        provider=scan.WORKSHOP_PROVIDER,
        scene="42",
    )
    store = pairings.Store(path=tmp_path / "pairings.json")
    session = Session(
        replace(config.Settings(), roots=(root,), scan_workshop=True).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=store,
    )
    session.adopt_library(Library(roots=(root,), items=(scene,)))
    custom = tmp_path / "chosen.png"
    custom.write_bytes(b"custom")
    store.choose_still(scene, custom)
    store.choose_palette(scene, pairings.PalettePolicy("builtin", "Nord"))
    store.mark_borked(scene, "engine crashed", "renderer-crash")
    generated = pairing.still_directory(root) / "42.png"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"automatic")
    if not source_present:
        scene_path.rmdir()
        content.rmdir()

    # With the source present this models a transient malformed project.json;
    # with both source and content root absent it models an unavailable drive.
    session.adopt_library(Library(roots=(root,), items=()))

    assert len(session.removed_workshop) == 0
    record = store.get(pairings.Identity.of(scene))
    assert record is not None
    assert record.still == custom
    assert record.palette == pairings.PalettePolicy("builtin", "Nord")
    assert record.health.is_borked
    assert generated.is_file()
    session.shutdown()


def test_missing_workshop_video_entry_is_not_an_uninstall_while_item_directory_exists(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    content = tmp_path / "steamapps" / "workshop" / "content" / "431960"
    item_directory = content / "42"
    item_directory.mkdir(parents=True)
    entry = item_directory / "wallpaper.mp4"
    entry.write_bytes(b"video")
    video = MediaItem(
        path=entry,
        kind=Kind.VIDEO,
        size=5,
        mtime=1,
        provider=scan.WORKSHOP_PROVIDER,
    )
    store = pairings.Store(path=tmp_path / "pairings.json")
    session = Session(
        replace(config.Settings(), roots=(root,), scan_workshop=True).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=store,
    )
    session.adopt_library(Library(roots=(root,), items=(video,)))
    chosen = tmp_path / "chosen.png"
    chosen.write_bytes(b"custom")
    store.choose_still(video, chosen)
    store.mark_borked(video, "renderer crashed", "renderer-crash")

    # Steam may replace or rename the media while updating project.json. The
    # Workshop-id directory is the install lifecycle boundary, not this file.
    entry.unlink()
    session.adopt_library(Library(roots=(root,), items=()))

    record = store.get(pairings.Identity.of(video))
    assert record is not None
    assert record.still == chosen
    assert record.health.is_borked

    # The transient omission must not erase the last-known installation. A
    # later scan can still recognize the directory's actual removal.
    item_directory.rmdir()
    session.adopt_library(Library(roots=(root,), items=()))
    assert session.removed_workshop == (video,)
    assert store.get(pairings.Identity.of(video)) is None
    assert chosen.is_file()
    session.shutdown()


def test_confirmed_workshop_video_uninstall_uses_the_item_directory_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    content = tmp_path / "steamapps" / "workshop" / "content" / "431960"
    item_directory = content / "42"
    item_directory.mkdir(parents=True)
    entry = item_directory / "wallpaper.mp4"
    entry.write_bytes(b"video")
    video = MediaItem(
        path=entry,
        kind=Kind.VIDEO,
        size=5,
        mtime=1,
        provider=scan.WORKSHOP_PROVIDER,
    )
    store = pairings.Store(path=tmp_path / "pairings.json")
    session = Session(
        replace(config.Settings(), roots=(root,), scan_workshop=True).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=store,
    )
    session.adopt_library(Library(roots=(root,), items=(video,)))
    store.mark_borked(video, "renderer crashed", "renderer-crash")

    entry.unlink()
    item_directory.rmdir()
    session.adopt_library(Library(roots=(root,), items=()))

    assert session.removed_workshop == (video,)
    assert store.get(pairings.Identity.of(video)) is None
    session.shutdown()


def test_workshop_metadata_failure_stays_visible_and_retries_on_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    content = tmp_path / "steamapps" / "workshop" / "content" / "431960"
    scene_path = content / "42"
    scene_path.mkdir(parents=True)
    scene = MediaItem(
        path=scene_path,
        kind=Kind.SCENE,
        size=1,
        mtime=1,
        provider=scan.WORKSHOP_PROVIDER,
        scene="42",
    )
    store = pairings.Store(path=tmp_path / "pairings.json")
    session = Session(
        replace(config.Settings(), roots=(root,), scan_workshop=True).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=store,
    )
    session.adopt_library(Library(roots=(root,), items=(scene,)))
    store.mark_borked(scene, "renderer crashed", "renderer-crash")
    original = store.forget_item
    attempts = 0

    def fail_once(
        removed: MediaItem,
        *,
        removed_stills: tuple[Path, ...] = (),
    ) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise pairings.PairingError("local-io", "pairings.json is read-only")
        return original(removed, removed_stills=removed_stills)

    monkeypatch.setattr(store, "forget_item", fail_once)
    scene_path.rmdir()

    session.adopt_library(Library(roots=(root,), items=()))

    assert session.removed_workshop == (scene,)
    assert "pairings.json is read-only" in " ".join(session.workshop_cleanup_failures)
    assert store.get(pairings.Identity.of(scene)) is not None

    # The next refresh retries the remembered lifecycle cleanup even though
    # the removed item is no longer present in the previous Library snapshot.
    session.adopt_library(Library(roots=(root,), items=()))
    assert session.workshop_cleanup_failures == ()
    assert store.get(pairings.Identity.of(scene)) is None
    session.shutdown()


def test_workshop_journal_failure_stays_visible_until_durable_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    content = tmp_path / "steamapps" / "workshop" / "content" / "431960"
    scene_path = content / "42"
    scene_path.mkdir(parents=True)
    scene = MediaItem(
        path=scene_path,
        kind=Kind.SCENE,
        size=1,
        mtime=1,
        provider=scan.WORKSHOP_PROVIDER,
        scene="42",
    )
    pairing_store = pairings.Store(path=tmp_path / "pairings.json")
    journal = removals.Store.open(tmp_path / "pending-removals.json")
    session = Session(
        replace(config.Settings(), roots=(root,), scan_workshop=True).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=pairing_store,
        removal_store=journal,
    )
    session.adopt_library(Library(roots=(root,), items=(scene,)))
    pairing_store.mark_borked(scene, "renderer crashed", "renderer-crash")
    original = journal.record_external

    def refuse_record(_item: MediaItem, _roots: Sequence[Path]) -> removals.Intent:
        raise removals.RemovalJournalError("local-io", "state directory is read-only")

    monkeypatch.setattr(journal, "record_external", refuse_record)
    scene_path.rmdir()

    session.adopt_library(Library(roots=(root,), items=()))

    assert "state directory is read-only" in " ".join(session.workshop_cleanup_failures)
    assert "recorded durably" in " ".join(session.workshop_cleanup_failures)
    assert pairing_store.get(pairings.Identity.of(scene)) is None

    monkeypatch.setattr(journal, "record_external", original)
    session.adopt_library(Library(roots=(root,), items=()))

    assert session.workshop_cleanup_failures == ()
    assert removals.Store.open(tmp_path / "pending-removals.json").records == ()
    session.shutdown()


def test_crash_after_local_delete_replays_durable_metadata_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "clip.mp4"
    source.write_bytes(b"video")
    motion = MediaItem(source, Kind.VIDEO, 5, 1)
    generated = pairing.still_directory(root) / f"{pairing.automatic_still_stem(source)}.png"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"automatic")
    dependent_path = root / "other.mp4"
    dependent_path.write_bytes(b"other")
    dependent = MediaItem(dependent_path, Kind.VIDEO, 5, 1)
    favourite_path = tmp_path / "favourites.json"
    pairing_path = tmp_path / "pairings.json"
    playlist_path = tmp_path / "playlists.json"
    journal_path = tmp_path / "pending-removals.json"
    favourite_store = favourites.Store(path=favourite_path)
    pairing_store = pairings.Store(path=pairing_path)
    playlist_store = playlists.Store(path=playlist_path)
    journal = removals.Store.open(journal_path)
    favourite_store.add(source)
    pairing_store.mark_borked(motion, "renderer crashed", "renderer-crash")
    pairing_store.choose_still(dependent, generated)
    playlist = playlist_store.create("Saved")
    playlist_store.add(playlist.id, source)
    first = Session(
        replace(config.Settings(), roots=(root,)).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        favourite_store=favourite_store,
        pairing_store=pairing_store,
        playlist_store=playlist_store,
        removal_store=journal,
    )

    first.prepare_removal(motion, (root,))
    source.unlink()
    # Simulate process death here: no commit marker and no store cleanup.
    first.shutdown()

    reopened_pairings = pairings.Store.open(pairing_path)
    reopened_favourites = favourites.Store.open(favourite_path)
    reopened_playlists = playlists.Store.open(playlist_path)
    restarted = Session(
        replace(config.Settings(), roots=(root,)).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        favourite_store=reopened_favourites,
        pairing_store=reopened_pairings,
        playlist_store=reopened_playlists,
        removal_store=removals.Store.open(journal_path),
    )

    assert restarted.retry_removals() == ()
    assert reopened_favourites.is_favourite(source) is False
    assert reopened_pairings.get(pairings.Identity.of(motion)) is None
    survivor = reopened_pairings.get(pairings.Identity.of(dependent))
    assert survivor is not None and survivor.still is None
    assert reopened_playlists.get(playlist.id) is not None
    assert reopened_playlists.get(playlist.id).entries == ()  # type: ignore[union-attr]
    assert not generated.exists()
    assert removals.Store.open(journal_path).records == ()

    # Reinstalling the same path cannot inherit the deleted item's Borked bit.
    source.write_bytes(b"reinstalled")
    assert not reopened_pairings.resolve(motion, (root,)).health.is_borked
    restarted.shutdown()


def test_restart_cancels_prepared_intent_when_original_was_never_removed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "paper.png"
    source.write_bytes(b"image")
    picture = MediaItem(source, Kind.STILL, 5, 1)
    pairing_path = tmp_path / "pairings.json"
    journal_path = tmp_path / "pending-removals.json"
    pairing_store = pairings.Store(path=pairing_path)
    pairing_store.mark_borked(picture, "renderer crashed", "renderer-crash")
    prepared_store = removals.Store.open(journal_path)
    prepared_store.prepare(picture, (root,))
    prepared_store.close()
    restarted = Session(
        replace(config.Settings(), roots=(root,)).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=pairings.Store.open(pairing_path),
        removal_store=removals.Store.open(journal_path),
    )

    assert restarted.retry_removals() == ()
    assert source.is_file()
    assert restarted.pairings.health(pairings.Identity.of(picture)).is_borked
    assert removals.Store.open(journal_path).records == ()
    restarted.shutdown()


def test_live_prepared_removal_cannot_be_cancelled_by_another_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "paper.png"
    source.write_bytes(b"image")
    picture = MediaItem(source, Kind.STILL, 5, 1)
    journal_path = tmp_path / "pending-removals.json"
    owner = removals.Store.open(journal_path)
    intent = owner.prepare(picture, (root,))
    observer = Session(
        replace(config.Settings(), roots=(root,)).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        removal_store=removals.Store.open(journal_path),
    )
    try:
        failures = observer.retry_removals()

        assert "still active in another process" in " ".join(failures)
        assert removals.Store.open(journal_path).records == (intent,)
    finally:
        owner.discard(intent)
        observer.shutdown()


def test_uncommitted_missing_source_waits_for_the_same_library_filesystem(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "paper.png"
    source.write_bytes(b"image")
    picture = MediaItem(source, Kind.STILL, 5, 1)
    pairing_path = tmp_path / "pairings.json"
    journal_path = tmp_path / "pending-removals.json"
    pairing_store = pairings.Store(path=pairing_path)
    pairing_store.mark_borked(picture, "renderer crashed", "renderer-crash")
    owner = removals.Store.open(journal_path)
    owner.prepare(picture, (root,))
    owner.close()
    mounted_elsewhere = tmp_path / "disconnected-drive"
    root.rename(mounted_elsewhere)
    root.mkdir()
    restarted = Session(
        replace(config.Settings(), roots=(root,)).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=pairings.Store.open(pairing_path),
        removal_store=removals.Store.open(journal_path),
    )
    try:
        failures = restarted.retry_removals()

        assert "filesystem is unavailable or changed" in " ".join(failures)
        assert restarted.pairings.health(pairings.Identity.of(picture)).is_borked
        assert restarted.removal_journal.records
    finally:
        restarted.shutdown()


def test_explicit_committed_removal_replays_after_the_source_root_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "paper.png"
    source.write_bytes(b"image")
    picture = MediaItem(source, Kind.STILL, 5, 1)
    pairing_path = tmp_path / "pairings.json"
    journal_path = tmp_path / "pending-removals.json"
    pairing_store = pairings.Store(path=pairing_path)
    pairing_store.mark_borked(picture, "renderer crashed", "renderer-crash")
    owner = removals.Store.open(journal_path)
    intent = owner.prepare(picture, (root,))
    source.unlink()
    committed = owner.mark_committed(intent)
    owner.finish_operation(committed)
    root.rename(tmp_path / "disconnected-drive")
    root.mkdir()
    restarted = Session(
        replace(config.Settings(), roots=(root,)).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=pairings.Store.open(pairing_path),
        removal_store=removals.Store.open(journal_path),
    )
    try:
        assert restarted.retry_removals() == ()
        assert not restarted.pairings.health(pairings.Identity.of(picture)).is_borked
        assert restarted.removal_journal.records == ()
    finally:
        restarted.shutdown()


def test_stale_commit_token_cannot_clean_a_newer_removal_lifecycle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    source = root / "paper.png"
    source.write_bytes(b"image")
    picture = MediaItem(source, Kind.STILL, 5, 1)
    pairing_store = pairings.Store(path=tmp_path / "pairings.json")
    pairing_store.mark_borked(picture, "renderer crashed", "renderer-crash")
    journal = removals.Store.open(tmp_path / "pending-removals.json")
    stale = journal.prepare(picture, (root,))
    journal.discard(stale)
    current = journal.prepare(picture, (root,))
    session = Session(
        replace(config.Settings(), roots=(root,)).validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        pairing_store=pairing_store,
        removal_store=journal,
    )
    try:
        failures = session.commit_removal(stale)

        assert "different pending removal" in " ".join(failures)
        assert pairing_store.health(pairings.Identity.of(picture)).is_borked
        assert journal.records == (current,)
    finally:
        journal.discard(current)
        session.shutdown()


# -- the rotation and the favourites --------------------------------------


def _with_favourites(
    items: Sequence[MediaItem], starred: Sequence[Path], tmp_path: Path, **overrides: object
) -> Session:
    store = favourites.Store(path=tmp_path / "favourites.json")
    for path in starred:
        store.add(path)
    settings = replace(config.Settings(), **overrides)  # type: ignore[arg-type]
    library = Library(roots=(Path("/w"),), items=tuple(items))
    session = Session(
        settings.validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda _roots: library,
        favourite_store=store,
    )
    session.refresh()
    return session


def test_the_rotation_is_the_whole_library_by_default(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    items = [_still("a"), _still("b"), _still("c")]
    session = _with_favourites(items, [items[0].path], tmp_path)
    assert len(session.playlist) == 3


def test_favourites_only_narrows_the_rotation(applied_paths: list[Path], tmp_path: Path) -> None:
    items = [_still("a"), _still("b"), _still("c")]
    session = _with_favourites(
        items, [items[0].path, items[2].path], tmp_path, cycle_favourites_only=True
    )
    assert [item.path for item in session.playlist.items] == [items[0].path, items[2].path]


def test_favourites_only_is_ignored_when_nothing_is_starred(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    """A manager that stops changing the wallpaper is a worse answer to "you
    have no favourites right now" than one that falls back and keeps working."""
    items = [_still("a"), _still("b")]
    session = _with_favourites(items, [], tmp_path, cycle_favourites_only=True)
    assert len(session.playlist) == 2


def test_favourites_only_is_ignored_when_none_of_them_are_here(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    """The drive they live on is not mounted; the rotation must not empty."""
    items = [_still("a"), _still("b")]
    session = _with_favourites(
        items, [Path("/elsewhere/gone.png")], tmp_path, cycle_favourites_only=True
    )
    assert len(session.playlist) == 2


def test_turning_the_setting_on_renarrows_immediately(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    items = [_still("a"), _still("b"), _still("c")]
    session = _with_favourites(items, [items[1].path], tmp_path)
    assert len(session.playlist) == 3
    session.update_settings(replace(session.settings, cycle_favourites_only=True))
    assert [item.path for item in session.playlist.items] == [items[1].path]


def test_starring_something_renarrows_the_rotation(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    """The grid writes to the store the session owns, then says so."""
    items = [_still("a"), _still("b")]
    session = _with_favourites(items, [items[0].path], tmp_path, cycle_favourites_only=True)
    assert len(session.playlist) == 1
    session.favourites.add(items[1].path)
    session.favourites_changed()
    assert len(session.playlist) == 2


def test_unstarring_the_last_one_falls_back_rather_than_emptying(
    applied_paths: list[Path], tmp_path: Path
) -> None:
    items = [_still("a"), _still("b")]
    session = _with_favourites(items, [items[0].path], tmp_path, cycle_favourites_only=True)
    session.favourites.discard(items[0].path)
    session.favourites_changed()
    assert len(session.playlist) == 2
