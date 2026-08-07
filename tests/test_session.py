from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config
from wall_in_one.library import favourites
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


def test_no_configured_roots_leaves_the_scanner_to_decide(applied_paths: list[Path]) -> None:
    """`None` is what makes `library.scan` fall back to asking Noctalia."""
    asked: list[object] = []

    def scanner(roots: Sequence[Path] | None) -> Library:
        asked.append(roots)
        return Library(roots=(), items=())

    session = Session(
        config.Settings().validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=scanner,
    )
    session.refresh()
    assert asked == [None]


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
    assert asked == [[Path("/elsewhere")]]


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


def test_a_session_that_builds_its_own_renderer_carries_the_settings() -> None:
    """`when_hidden` becomes a command-line flag, so it has to be right before
    the first video starts, not pushed afterwards."""
    settings = replace(
        config.Settings(), video_muted=False, video_volume=25, video_when_hidden="stop"
    ).validated()
    built = Session(settings, scanner=lambda _roots: Library(roots=(), items=()))
    assert built._applier.renderer.muted is False
    assert built._applier.renderer.volume == 25
    assert built._applier.renderer.when_hidden == "stop"
    built.shutdown()


def test_an_applier_handed_in_is_left_as_its_owner_configured_it() -> None:
    fake = AudioRenderer()
    fake.when_hidden = "play"
    settings = replace(config.Settings(), video_when_hidden="stop").validated()
    Session(settings, applier=Applier(fake), scanner=lambda _roots: Library(roots=(), items=()))  # type: ignore[arg-type]
    assert fake.when_hidden == "play"


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
