from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from wall_in_one import file_io, paths
from wall_in_one.library import pairings
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper import outputs, renderer, scenes
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


@pytest.fixture
def applied_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str | None]]:
    """Every wallpaper handed to Noctalia, with the output it was aimed at."""
    calls: list[tuple[Path, str | None]] = []

    def record(path: Path, connector: str | None = None) -> None:
        calls.append((Path(path), connector))

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


@pytest.fixture
def short_runtime_dir() -> Iterator[Path]:
    """Keep AF_UNIX test addresses below Linux's 108-byte ceiling."""
    with tempfile.TemporaryDirectory(prefix="wio-renderer-", dir="/tmp") as directory:
        yield Path(directory)


def test_renderer_refuses_a_missing_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer, "is_available", lambda: True)
    with pytest.raises(renderer.RendererError, match="no such video"):
        renderer.Renderer().start(tmp_path / "gone.mp4")


def test_renderer_reports_mpvpaper_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer, "is_available", lambda: False)
    with pytest.raises(renderer.MpvpaperUnavailableError):
        renderer.Renderer().start(tmp_path / "clip.mp4")


def test_a_failed_start_leaves_its_empty_private_socket_namespace_inert(
    tmp_path: Path,
    short_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("not really starting mpvpaper")

    monkeypatch.setattr(renderer, "is_available", lambda: True)
    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    monkeypatch.setattr(subprocess, "Popen", fail)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")

    with pytest.raises(renderer.RendererError):
        renderer.Renderer().start(video)

    (namespace,) = tuple(short_runtime_dir.glob(f".{paths.APP_ID}-mpv-*"))
    assert namespace.is_dir()
    assert tuple(namespace.iterdir()) == ()
    assert not (short_runtime_dir / file_io.RETAINED_ENTRY_DIRECTORY).exists()


def test_start_setup_failure_leaves_its_private_socket_namespace_inert(
    tmp_path: Path,
    short_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_discovery() -> tuple[()]:
        raise RuntimeError("injected output discovery failure")

    monkeypatch.setattr(renderer, "is_available", lambda: True)
    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    monkeypatch.setattr(outputs, "discover", fail_discovery)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")

    with pytest.raises(RuntimeError, match="output discovery"):
        renderer.Renderer(interpolation="oversample").start(video)

    (namespace,) = tuple(short_runtime_dir.glob(f".{paths.APP_ID}-mpv-*"))
    assert namespace.is_dir()
    assert tuple(namespace.iterdir()) == ()
    assert not (short_runtime_dir / file_io.RETAINED_ENTRY_DIRECTORY).exists()


def test_start_retains_the_session_leader_pid_as_the_process_group_identity(
    tmp_path: Path,
    short_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StartedProcess:
        pid = 987653
        exited = False

        def poll(self) -> int | None:
            return 0 if self.exited else None

        def wait(self, *, timeout: float) -> int:
            del timeout
            return 0

    process = StartedProcess()
    launch_keywords: dict[str, object] = {}

    owned_process = cast(subprocess.Popen[bytes], process)

    def launch(_command: list[str], **keywords: object) -> subprocess.Popen[bytes]:
        launch_keywords.update(keywords)
        return owned_process

    def process_group(pgid: int, requested: int) -> None:
        assert pgid == process.pid
        if requested == signal.SIGTERM:
            process.exited = True
        if requested == 0 and process.exited:
            raise ProcessLookupError("group exited")

    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    monkeypatch.setattr(renderer, "is_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "killpg", process_group)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")
    instance = renderer.Renderer()

    instance.start(video)

    assert launch_keywords["start_new_session"] is True
    assert instance._process is owned_process
    assert instance._pgid == process.pid

    instance.stop()


def test_stopping_an_idle_renderer_is_harmless() -> None:
    renderer.Renderer().stop()


def test_renderer_never_reuses_or_removes_the_legacy_fixed_socket_name(
    short_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user entry at the old public path is neither stale nor ours."""
    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    legacy_path = short_runtime_dir / f"{paths.APP_ID}-mpv.sock"
    legacy_path.write_text("keep me", encoding="utf-8")

    instance = renderer.Renderer()
    namespace = instance._allocate_socket_path()

    assert namespace is not None
    ipc_path = namespace.logical_socket
    assert ipc_path != legacy_path
    assert ipc_path.parent.parent == short_runtime_dir
    assert stat.S_IMODE(ipc_path.parent.stat().st_mode) == 0o700
    assert legacy_path.read_text(encoding="utf-8") == "keep me"
    instance._cleanup_socket_path(namespace)
    assert ipc_path.parent.is_dir()


def test_stopping_leaves_the_exact_socket_namespace_inert(
    short_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    instance = renderer.Renderer()
    namespace = instance._allocate_socket_path()
    assert namespace is not None
    ipc_path = namespace.logical_socket
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(namespace.access_socket))
    listener.close()
    instance._socket = namespace

    instance.stop()

    assert ipc_path.is_socket()
    assert ipc_path.parent.is_dir()
    assert not (short_runtime_dir / file_io.RETAINED_ENTRY_DIRECTORY).exists()


def test_stop_process_exit_race_still_checks_the_retained_process_group(
    short_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        pid = 987654

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def terminate() -> None:
            raise ProcessLookupError("already exited")

        @staticmethod
        def wait(*, timeout: float) -> int:
            del timeout
            return 0

    signals: list[tuple[int, int]] = []

    def absent_group(pgid: int, requested: int) -> None:
        signals.append((pgid, requested))
        raise ProcessLookupError("already exited")

    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    monkeypatch.setattr(os, "killpg", absent_group)
    instance = renderer.Renderer()
    namespace = instance._allocate_socket_path()
    assert namespace is not None
    instance._socket = namespace
    instance._process = ExitedProcess()  # type: ignore[assignment]
    instance._pgid = ExitedProcess.pid

    instance.stop()

    assert signals[0] == (ExitedProcess.pid, signal.SIGTERM)
    assert (ExitedProcess.pid, 0) in signals
    assert namespace.directory.is_dir()
    assert instance._process is None
    assert instance._pgid is None


def test_stop_kills_retained_group_children_after_the_leader_has_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedLeader:
        pid = 987655

        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def wait(*, timeout: float) -> int:
            del timeout
            return 0

    group_exists = True
    signals: list[int] = []

    def process_group(pgid: int, requested: int) -> None:
        nonlocal group_exists
        assert pgid == ExitedLeader.pid
        if requested == 0:
            if group_exists:
                return
            raise ProcessLookupError("group exited")
        signals.append(requested)
        if requested == signal.SIGKILL:
            group_exists = False

    monkeypatch.setattr(os, "killpg", process_group)
    monkeypatch.setattr(renderer, "TERMINATE_TIMEOUT", 0.0)
    instance = renderer.Renderer()
    instance._process = ExitedLeader()  # type: ignore[assignment]
    instance._pgid = ExitedLeader.pid

    instance.stop()

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert instance._process is None
    assert instance._pgid is None


def test_persistent_process_group_retains_all_state_and_blocks_a_second_start(
    tmp_path: Path,
    short_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PersistentProcess:
        pid = 987656
        stuck = True

        def poll(self) -> int | None:
            return None if self.stuck else 0

        def wait(self, *, timeout: float) -> int:
            if self.stuck:
                raise subprocess.TimeoutExpired("mpvpaper", timeout)
            return 0

    process = PersistentProcess()
    owned_process = cast(subprocess.Popen[bytes], process)

    def process_group(pgid: int, requested: int) -> None:
        assert pgid == process.pid
        if not process.stuck:
            raise ProcessLookupError("group exited")

    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    monkeypatch.setattr(os, "killpg", process_group)
    monkeypatch.setattr(renderer, "TERMINATE_TIMEOUT", 0.001)
    monkeypatch.setattr(renderer, "PROCESS_GROUP_POLL_SECONDS", 0.001)
    monkeypatch.setattr(renderer, "is_available", lambda: True)
    launches: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: launches.append(object()))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")
    instance = renderer.Renderer()
    namespace = instance._allocate_socket_path()
    assert namespace is not None
    instance._process = owned_process
    instance._pgid = process.pid
    instance._video = video
    instance._socket = namespace

    with pytest.raises(renderer.RendererError, match=r"process group .* did not exit"):
        instance.stop()

    assert instance._process is owned_process
    assert instance._pgid == process.pid
    assert instance._video == video
    assert instance._socket is namespace
    assert namespace.pin.status().st_ino

    with pytest.raises(renderer.RendererError, match=r"process group .* did not exit"):
        instance.start(video)
    assert launches == []
    assert instance._process is owned_process
    assert instance._socket is namespace

    process.stuck = False
    instance.stop()
    assert not instance.is_running
    assert instance.ipc_socket is None
    assert namespace.directory.is_dir()


def test_a_wait_failure_retains_state_even_when_kill_makes_the_group_disappear(
    tmp_path: Path,
    short_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenWaitProcess:
        pid = 987657
        killed = False
        wait_failed = False

        def poll(self) -> int | None:
            return 0 if self.killed else None

        def wait(self, *, timeout: float) -> int:
            del timeout
            if not self.wait_failed:
                self.wait_failed = True
                raise OSError("injected wait failure")
            return 0

    process = BrokenWaitProcess()
    owned_process = cast(subprocess.Popen[bytes], process)

    def process_group(pgid: int, requested: int) -> None:
        assert pgid == process.pid
        if requested == signal.SIGKILL:
            process.killed = True
        if requested == 0 and process.killed:
            raise ProcessLookupError("group exited")

    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    monkeypatch.setattr(os, "killpg", process_group)
    instance = renderer.Renderer()
    namespace = instance._allocate_socket_path()
    assert namespace is not None
    instance._process = owned_process
    instance._pgid = process.pid
    instance._video = tmp_path / "clip.mp4"
    instance._socket = namespace

    with pytest.raises(renderer.RendererError, match="earlier wait failure"):
        instance.stop()

    assert instance._process is owned_process
    assert instance._pgid == process.pid
    assert instance._socket is namespace
    assert namespace.pin.status().st_ino

    instance.stop()
    assert not instance.is_running
    assert namespace.directory.is_dir()


def test_stopping_preserves_a_replacement_private_directory(
    short_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allocation-time pin anchors access but authorizes no directory cleanup."""
    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    instance = renderer.Renderer()
    namespace = instance._allocate_socket_path()
    assert namespace is not None
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(namespace.access_socket))
    listener.close()
    instance._socket = namespace
    moved = short_runtime_dir / "moved-original"
    namespace.directory.rename(moved)
    namespace.directory.mkdir(mode=0o700)
    replacement = namespace.directory / "keep"
    replacement.write_text("replacement", encoding="utf-8")

    instance.stop()

    assert replacement.read_text(encoding="utf-8") == "replacement"
    assert (moved / "ipc.sock").is_socket()


def test_socket_directory_creation_to_pin_replacement_is_never_moved_or_deleted(
    short_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A random mkdir name is not proof that the directory still belongs to us."""
    monkeypatch.setattr(paths, "runtime_dir", lambda: short_runtime_dir)
    real_lstat = Path.lstat
    original = short_runtime_dir / "original-allocation"
    replacement_payload: Path | None = None
    swapped = False

    def replace_before_first_inspection(path: Path) -> os.stat_result:
        nonlocal replacement_payload, swapped
        if (
            not swapped
            and path.parent == short_runtime_dir
            and path.name.startswith(f".{paths.APP_ID}-mpv-")
        ):
            swapped = True
            path.rename(original)
            path.mkdir(mode=0o700)
            replacement_payload = path / "keep"
            replacement_payload.write_text("unrelated replacement", encoding="utf-8")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", replace_before_first_inspection)
    instance = renderer.Renderer()
    namespace = instance._allocate_socket_path()
    assert namespace is not None
    instance._cleanup_socket_path(namespace)

    assert original.is_dir()
    assert tuple(original.iterdir()) == ()
    assert replacement_payload is not None
    assert replacement_payload.read_text(encoding="utf-8") == "unrelated replacement"
    assert namespace.directory == replacement_payload.parent
    assert not (short_runtime_dir / file_io.RETAINED_ENTRY_DIRECTORY).exists()


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


@pytest.mark.parametrize("mode", ("oversample", "linear"))
def test_interpolation_is_an_atomic_effective_option_set(mode: str) -> None:
    options = renderer.Renderer(interpolation=mode)._mpv_options(None, 165.004)
    for expected in (
        "video-sync=display-resample",
        "interpolation=yes",
        "display-fps-override=165.004",
        f"tscale={mode}",
    ):
        assert expected in options


def test_interpolation_without_a_known_refresh_changes_nothing() -> None:
    options = renderer.Renderer(interpolation="oversample")._mpv_options(None, None)
    assert "video-sync=" not in options
    assert "interpolation=" not in options
    assert "display-fps-override=" not in options
    assert "tscale=" not in options


def test_hardware_decoding_can_be_disabled_for_driver_diagnostics() -> None:
    assert "hwdec=no" in renderer.Renderer(hardware_decode=False)._mpv_options(None)


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


# -- which output ---------------------------------------------------------
#
# `noctalia msg wallpaper-set [connector] <path>` takes an optional connector
# and mpvpaper takes an output selector, so both halves can be aimed. Only one
# output is connected on the development machine, so what is pinned here is
# that the connector reaches the command -- not that two monitors end up
# showing different things.


def test_a_still_goes_to_every_output_by_default(
    applied_calls: list[tuple[Path, str | None]], tmp_path: Path
) -> None:
    Applier(FakeRenderer()).apply(_still(tmp_path), dynamics_enabled=True)  # type: ignore[arg-type]
    assert applied_calls[-1][1] is None


def test_a_still_can_be_aimed_at_one_output(
    applied_calls: list[tuple[Path, str | None]], tmp_path: Path
) -> None:
    applier = Applier(FakeRenderer(), output="DP-2")  # type: ignore[arg-type]
    applier.apply(_still(tmp_path), dynamics_enabled=True)
    assert applied_calls[-1][1] == "DP-2"


def test_a_videos_paired_still_is_aimed_the_same_way(
    applied_calls: list[tuple[Path, str | None]], tmp_path: Path
) -> None:
    """It goes underneath the video, so it has to land on the same screen."""
    applier = Applier(FakeRenderer(), output="DP-2")  # type: ignore[arg-type]
    applier.apply(_video(tmp_path), dynamics_enabled=True)
    assert applied_calls[-1][1] == "DP-2"


def test_the_renderer_defaults_to_every_output() -> None:
    assert renderer.Renderer().output == renderer.ALL_OUTPUTS


# -- the palette a wallpaper asks for -------------------------------------


@pytest.fixture
def noctalia_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every Noctalia call the applier makes, in order."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.set_wallpaper",
        lambda path, connector=None: calls.append(("wallpaper", Path(path).name)),
    )
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.set_mode", lambda mode: calls.append(("mode", str(mode)))
    )
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.set_scheme",
        lambda selection: calls.append(("scheme", f"{selection.source}/{selection.name}")),
    )
    return calls


def test_the_still_is_set_before_the_palette_is_asked_for(
    noctalia_calls: list[tuple[str, str]], tmp_path: Path
) -> None:
    """Noctalia derives adaptive colours from whatever wallpaper is set, so
    asking first would generate them from the previous picture."""
    applier = Applier(FakeRenderer())  # type: ignore[arg-type]
    applier.apply(
        _still(tmp_path),
        dynamics_enabled=True,
        palette=pairings.PalettePolicy(),
        generator="m3-fruit-salad",
    )
    assert noctalia_calls == [("wallpaper", "a.png"), ("scheme", "wallpaper/m3-fruit-salad")]


def test_the_mode_is_set_before_the_palette(
    noctalia_calls: list[tuple[str, str]], tmp_path: Path
) -> None:
    applier = Applier(FakeRenderer())  # type: ignore[arg-type]
    applier.apply(
        _still(tmp_path),
        dynamics_enabled=True,
        palette=pairings.PalettePolicy("builtin", "Nord", pairings.Mode.DARK),
    )
    assert noctalia_calls == [
        ("wallpaper", "a.png"),
        ("mode", "dark"),
        ("scheme", "builtin/Nord"),
    ]


def test_a_video_gets_its_palette_from_its_still_before_playing(
    noctalia_calls: list[tuple[str, str]], tmp_path: Path
) -> None:
    """The still goes underneath first, so adaptive colours come from what the
    video looks like rather than from the wallpaper before it."""
    fake = FakeRenderer()
    applier = Applier(fake)  # type: ignore[arg-type]
    applier.apply(_video(tmp_path), dynamics_enabled=True, palette=pairings.PalettePolicy())
    assert noctalia_calls[0] == ("wallpaper", "clip-still.png")
    assert noctalia_calls[1][0] == "scheme"
    assert fake.started, "the renderer starts last, after the colours are settled"


def test_keeping_the_palette_asks_noctalia_for_nothing(
    noctalia_calls: list[tuple[str, str]], tmp_path: Path
) -> None:
    applier = Applier(FakeRenderer())  # type: ignore[arg-type]
    applier.apply(
        _still(tmp_path),
        dynamics_enabled=True,
        palette=pairings.PalettePolicy(kind=pairings.KEEP),
    )
    assert noctalia_calls == [("wallpaper", "a.png")]


def test_no_policy_at_all_leaves_the_palette_alone(
    noctalia_calls: list[tuple[str, str]], tmp_path: Path
) -> None:
    """What every caller that predates policies does."""
    applier = Applier(FakeRenderer())  # type: ignore[arg-type]
    applier.apply(_still(tmp_path), dynamics_enabled=True)
    assert noctalia_calls == [("wallpaper", "a.png")]


def test_a_palette_that_will_not_apply_does_not_lose_the_wallpaper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The picture is what was asked for and is already on screen by then."""
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", lambda *a, **k: None)

    def refuse(_selection: object) -> None:
        raise noctalia.NoctaliaError("no shell")

    monkeypatch.setattr("wall_in_one.theme.noctalia.set_scheme", refuse)
    applier = Applier(FakeRenderer())  # type: ignore[arg-type]
    applied = applier.apply(
        _still(tmp_path), dynamics_enabled=True, palette=pairings.PalettePolicy()
    )
    assert applied.path.name == "a.png"


# -- switching between the two moving renderers --------------------------
#
# A playlist mixes stills, mpvpaper videos and Wallpaper Engine scenes, and
# every hop between them has to leave exactly one renderer running. The engine
# is single-instance per output, so a scene left behind is not a cosmetic
# leak: it is a second program drawing on the same screen as the video that
# replaced it.


class FakeScenes:
    """Stands in for `linux-wallpaperengine`."""

    def __init__(self, *, fail: bool = False) -> None:
        self.started: list[str] = []
        self.stops = 0
        self._fail = fail

    @property
    def is_running(self) -> bool:
        return bool(self.started)

    def start(self, scene: str) -> None:
        if self._fail:
            raise scenes.SceneError("boom")
        self.started.append(scene)

    def stop(self) -> None:
        self.stops += 1
        self.started.clear()


def _scene(tmp_path: Path) -> MediaItem:
    return MediaItem(
        path=tmp_path / "1647046763",
        kind=Kind.SCENE,
        size=1,
        mtime=0,
        scene="1647046763",
        paired_still=tmp_path / "1647046763-still.png",
    )


@pytest.fixture
def both(
    monkeypatch: pytest.MonkeyPatch, set_calls: list[Path]
) -> tuple[Applier, FakeRenderer, FakeScenes]:
    """An applier owning both renderers, with no foreign engine in the way."""
    monkeypatch.setattr("wall_in_one.wallpaper.scenes.running_elsewhere", lambda output="": ())
    video, scene = FakeRenderer(), FakeScenes()
    applier = Applier(video, scene_renderer=scene, own_scene_renderer=True)  # type: ignore[arg-type]
    return applier, video, scene


def test_switching_from_a_scene_to_a_video_stops_the_engine(
    tmp_path: Path, both: tuple[Applier, FakeRenderer, FakeScenes]
) -> None:
    """The transition that left two renderers drawing at once.

    mpvpaper's own `start` stops mpvpaper, so the video half was covered; the
    scene half had no such guarantee and the engine kept rendering underneath.
    """
    applier, video, scene = both
    applier.apply(_scene(tmp_path), dynamics_enabled=True)
    assert scene.started == ["1647046763"]

    applier.apply(_video(tmp_path), dynamics_enabled=True)
    assert scene.stops == 1
    assert scene.started == []
    assert video.started == [tmp_path / "clip.mp4"]


def test_switching_from_a_video_to_a_scene_stops_mpvpaper(
    tmp_path: Path, both: tuple[Applier, FakeRenderer, FakeScenes]
) -> None:
    applier, video, scene = both
    applier.apply(_video(tmp_path), dynamics_enabled=True)
    applier.apply(_scene(tmp_path), dynamics_enabled=True)
    assert video.stops >= 1
    assert video.video is None
    assert scene.started == ["1647046763"]


@pytest.mark.parametrize("first", ["video", "scene"])
def test_switching_to_a_still_stops_whichever_was_running(
    tmp_path: Path, first: str, both: tuple[Applier, FakeRenderer, FakeScenes]
) -> None:
    applier, video, scene = both
    applier.apply(_video(tmp_path) if first == "video" else _scene(tmp_path), dynamics_enabled=True)
    applier.apply(_still(tmp_path), dynamics_enabled=True)
    assert video.video is None
    assert not scene.is_running


def test_a_refused_scene_stops_the_video_it_was_replacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_calls: list[Path]
) -> None:
    """A playlist reaching a scene it may not start must not show the last video.

    By the time the refusal happens the scene's still is already on screen and
    its palette already applied -- so leaving mpvpaper running would show the
    *previous* video under the *next* wallpaper's colours. Stopping it is the
    coherent outcome: the right still, the right colours, no motion.
    """
    monkeypatch.setattr("wall_in_one.wallpaper.scenes.running_elsewhere", lambda output="": ())
    video, scene = FakeRenderer(), FakeScenes()
    applier = Applier(video, scene_renderer=scene, own_scene_renderer=False)  # type: ignore[arg-type]
    applier.apply(_video(tmp_path), dynamics_enabled=True)

    with pytest.raises(ApplyError, match="not set to drive"):
        applier.apply(_scene(tmp_path), dynamics_enabled=True)
    assert video.video is None
    assert set_calls[-1] == tmp_path / "1647046763-still.png"
