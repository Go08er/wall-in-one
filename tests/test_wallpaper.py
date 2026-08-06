from __future__ import annotations

from pathlib import Path

import pytest

from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper import renderer
from wall_in_one.wallpaper.applier import Applier, ApplyError


class FakeRenderer:
    """Stands in for mpvpaper. Records what it was asked to do."""

    def __init__(self, *, fail: bool = False) -> None:
        self.started: list[Path] = []
        self.stops = 0
        self.video: Path | None = None
        self._fail = fail

    def start(self, video: Path) -> None:
        if self._fail:
            raise renderer.RendererError("boom")
        self.started.append(video)
        self.video = video

    def stop(self) -> None:
        self.stops += 1
        self.video = None


@pytest.fixture
def set_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def record(path: Path, connector: str | None = None) -> None:
        calls.append(Path(path))

    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", record)
    return calls


def _still(tmp_path: Path) -> MediaItem:
    return MediaItem(path=tmp_path / "a.png", kind=Kind.STILL, size=1, mtime=0)


def _video(tmp_path: Path, *, paired: bool = True) -> MediaItem:
    return MediaItem(
        path=tmp_path / "clip.mp4",
        kind=Kind.VIDEO,
        size=1,
        mtime=0,
        paired_still=(tmp_path / "clip-still.png") if paired else None,
    )


def test_applying_a_still_goes_through_noctalia(tmp_path: Path, set_calls: list[Path]) -> None:
    fake = FakeRenderer()
    applier = Applier(fake)  # type: ignore[arg-type]

    applied = applier.apply(_still(tmp_path), dynamics_enabled=True)

    assert set_calls == [tmp_path / "a.png"]
    assert not applied.animated
    assert applier.current == applied


def test_applying_a_still_stops_a_running_video(tmp_path: Path, set_calls: list[Path]) -> None:
    """A video renders over the still, so it has to go first."""
    fake = FakeRenderer()
    applier = Applier(fake)  # type: ignore[arg-type]

    applier.apply(_still(tmp_path), dynamics_enabled=True)

    assert fake.stops == 1


def test_applying_a_video_sets_its_still_underneath(tmp_path: Path, set_calls: list[Path]) -> None:
    fake = FakeRenderer()
    applier = Applier(fake)  # type: ignore[arg-type]

    applied = applier.apply(_video(tmp_path), dynamics_enabled=True)

    assert set_calls == [tmp_path / "clip-still.png"]
    assert fake.started == [tmp_path / "clip.mp4"]
    assert applied.animated


def test_a_still_that_noctalia_rejects_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(path: Path, connector: str | None = None) -> None:
        raise noctalia.NoctaliaError("nope")

    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", fail)
    applier = Applier(FakeRenderer())  # type: ignore[arg-type]

    with pytest.raises(ApplyError, match="nope"):
        applier.apply(_still(tmp_path), dynamics_enabled=True)


def test_a_video_survives_noctalia_refusing_the_still(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The video is what was asked for; the still is a nicety."""

    def fail(path: Path, connector: str | None = None) -> None:
        raise noctalia.NoctaliaError("nope")

    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", fail)
    fake = FakeRenderer()
    applier = Applier(fake)  # type: ignore[arg-type]

    applied = applier.apply(_video(tmp_path), dynamics_enabled=True)

    assert applied.animated
    assert fake.started == [tmp_path / "clip.mp4"]


def test_a_failing_renderer_is_an_error(tmp_path: Path, set_calls: list[Path]) -> None:
    applier = Applier(FakeRenderer(fail=True))  # type: ignore[arg-type]
    with pytest.raises(ApplyError, match="boom"):
        applier.apply(_video(tmp_path), dynamics_enabled=True)


def test_dynamics_off_applies_the_still_instead(tmp_path: Path, set_calls: list[Path]) -> None:
    fake = FakeRenderer()
    applier = Applier(fake)  # type: ignore[arg-type]

    applied = applier.apply(_video(tmp_path), dynamics_enabled=False)

    assert not applied.animated
    assert applied.path == tmp_path / "clip-still.png"
    assert fake.started == []


def test_dynamics_off_on_an_unpaired_video_says_so(tmp_path: Path, set_calls: list[Path]) -> None:
    applier = Applier(FakeRenderer())  # type: ignore[arg-type]
    with pytest.raises(ApplyError, match="no still"):
        applier.apply(_video(tmp_path, paired=False), dynamics_enabled=False)


def test_toggling_dynamics_reapplies_the_current_video(
    tmp_path: Path, set_calls: list[Path]
) -> None:
    fake = FakeRenderer()
    applier = Applier(fake)  # type: ignore[arg-type]
    applier.apply(_video(tmp_path), dynamics_enabled=True)

    paused = applier.set_dynamics(False)
    assert paused is not None and not paused.animated

    resumed = applier.set_dynamics(True)
    assert resumed is not None and resumed.animated
    assert fake.started == [tmp_path / "clip.mp4", tmp_path / "clip.mp4"]


def test_toggling_dynamics_leaves_a_still_alone(tmp_path: Path, set_calls: list[Path]) -> None:
    applier = Applier(FakeRenderer())  # type: ignore[arg-type]
    applier.apply(_still(tmp_path), dynamics_enabled=True)
    set_calls.clear()

    assert applier.set_dynamics(False) == applier.current
    assert set_calls == []


def test_shutdown_stops_the_renderer(tmp_path: Path, set_calls: list[Path]) -> None:
    fake = FakeRenderer()
    applier = Applier(fake)  # type: ignore[arg-type]
    applier.apply(_video(tmp_path), dynamics_enabled=True)

    applier.shutdown()

    assert fake.stops >= 1


# -- renderer ------------------------------------------------------------


def test_renderer_refuses_a_missing_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer, "is_available", lambda: True)
    with pytest.raises(renderer.RendererError, match="no such video"):
        renderer.Renderer().start(tmp_path / "gone.mp4")


def test_renderer_reports_mpvpaper_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer, "is_available", lambda: False)
    with pytest.raises(renderer.MpvpaperUnavailableError):
        renderer.Renderer().start(tmp_path / "clip.mp4")


def test_stopping_an_idle_renderer_is_harmless() -> None:
    renderer.Renderer().stop()


def test_ipc_is_absent_until_something_is_playing() -> None:
    instance = renderer.Renderer()
    assert instance.ipc_socket is None
    assert instance.command("get_property", "pause") is None


def test_mpv_options_keep_audio_loaded_when_muted() -> None:
    """`mute=yes` leaves the track available to unmute later; `no-audio` would not."""
    options = renderer.Renderer(muted=True)._mpv_options(None)
    assert "mute=yes" in options
    assert "no-audio" not in options
    assert "loop-file=inf" in options
