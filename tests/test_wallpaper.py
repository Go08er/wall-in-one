from __future__ import annotations

import subprocess
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


# -- video playback settings ----------------------------------------------
#
# The renderer always had the knobs -- mute, hardware decode, an auto-pause
# flag -- and nothing ever set them. These pin the wiring between a setting and
# the process that has to honour it.


def test_the_volume_reaches_mpv_even_while_muted() -> None:
    """Unmuting later must land at the chosen level, not at mpv's default."""
    options = renderer.Renderer(muted=True, volume=40)._mpv_options(None)
    assert "volume=40" in options
    assert "mute=yes" in options


@pytest.mark.parametrize(
    ("volume", "expected"),
    [(-10, "volume=0"), (0, "volume=0"), (100, "volume=100"), (500, "volume=100")],
)
def test_the_volume_is_clamped_to_mpvs_scale(volume: int, expected: str) -> None:
    assert expected in renderer.Renderer(volume=volume)._mpv_options(None)


@pytest.mark.parametrize(
    ("policy", "flag"),
    [("pause", "--auto-pause"), ("stop", "--auto-stop")],
)
def test_the_hidden_policy_becomes_an_mpvpaper_flag(
    policy: str, flag: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def capture(command: list[str], **_kwargs: object) -> object:
        seen.append(command)
        raise OSError("not really starting mpvpaper")

    monkeypatch.setattr(renderer, "is_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", capture)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")
    with pytest.raises(renderer.RendererError):
        renderer.Renderer(when_hidden=policy).start(video)
    assert flag in seen[0]


def test_keeping_it_playing_passes_neither_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """mpvpaper warns its auto options 'might not work as intended', so the
    escape hatch has to be reachable."""
    seen: list[list[str]] = []

    def capture(command: list[str], **_kwargs: object) -> object:
        seen.append(command)
        raise OSError("not really starting mpvpaper")

    monkeypatch.setattr(renderer, "is_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", capture)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")
    with pytest.raises(renderer.RendererError):
        renderer.Renderer(when_hidden="play").start(video)
    assert "--auto-pause" not in seen[0]
    assert "--auto-stop" not in seen[0]


def test_the_volume_is_remembered_even_when_ipc_is_unavailable() -> None:
    """ "The setting did not take" and "did not take yet" are different things."""
    instance = renderer.Renderer(volume=100)
    assert instance.set_volume(30) is False
    assert instance.volume == 30
    assert "volume=30" in instance._mpv_options(None)


def test_applying_audio_sets_the_volume_before_unmuting() -> None:
    """Unmuting at the old level first would put a moment of the wrong
    loudness through the speakers, which is the one mistake here you hear."""
    order: list[tuple[str, object]] = []

    class Recording(renderer.Renderer):
        def set_property(self, name: str, value: object) -> bool:
            order.append((name, value))
            return True

    Recording().apply_audio(muted=False, volume=55)
    assert order == [("volume", 55), ("mute", False)]
