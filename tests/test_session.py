from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config
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
