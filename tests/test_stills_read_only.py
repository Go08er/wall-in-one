"""A still its folder won't take fails as that: `stills.StillFolderError`, naming the folder.

`stills.ensure` forgives every failure, which is right for its callers (a
wallpaper with no still still plays) and left one failure indistinguishable
from the rest: a folder that can't be written looked like a video ffmpeg
can't read, and `tests.test_read_only_profile` found a read-only library that
never got its stills with nothing said. The reason is known where the write
is refused, so it is raised there as its own kind of `StillError`, carrying
the folder that refused and the kernel's word for why, and `ensure` hands it
to a caller that asks (``refused``).

Every folder here is read-only by its real permission bits; nothing injects
an error. ffmpeg is a stub, as in `tests.test_stills`.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from wall_in_one import worker_processes
from wall_in_one.library import pairing, stills
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.wallpaper import scenes

FRAME = b"\x89PNG\r\n\x1a\nrendered"
DISPLAY = (1920, 1080)
SCENE = "2149140853"


@contextlib.contextmanager
def read_only(*folders: Path) -> Iterator[None]:
    """Each of ``folders`` (itself, not what is below it) 0555 for the block."""
    if os.geteuid() == 0:
        pytest.skip("permission bits don't bind root")
    modes = [(folder, folder.stat().st_mode & 0o7777) for folder in folders]
    try:
        for folder in folders:
            folder.chmod(0o555)
        yield
    finally:
        for folder, mode in modes:
            folder.chmod(mode)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    made = tmp_path / "library"
    made.mkdir()
    return made


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """ffmpeg, installed and able to read every video: the sources it was run on."""
    ran: list[Path] = []

    def render(
        source: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: worker_processes.Cancellation | None = None,
    ) -> str:
        del processes
        ran.append(source)
        temporary.write_bytes(FRAME)
        return ""

    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(stills, "_run", render)
    return ran


def video(folder: Path, name: str = "clip.mp4") -> Path:
    path = folder / name
    path.write_bytes(b"a video")
    return path


def everything_under(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def refusal(make: Callable[[], object]) -> stills.StillFolderError:
    with pytest.raises(stills.StillFolderError) as raised:
        make()
    return raised.value


def test_a_library_that_cannot_take_the_stills_folder(root: Path, rendered: list[Path]) -> None:
    clip = video(root)
    before = everything_under(root)
    with read_only(root):
        error = refusal(lambda: stills.generate(clip, root))
    assert error.folder == root and error.reason == "Permission denied"
    assert str(error) == f"could not create {pairing.still_directory(root)}: Permission denied"
    assert rendered == [], "refused before ffmpeg was run"
    assert everything_under(root) == before


def test_the_stills_folder_read_only_on_its_own(root: Path, rendered: list[Path]) -> None:
    """The library takes writes; ``Wall-in-One/Automatic Stills`` alone doesn't."""
    clip = video(root)
    folder = pairing.still_directory(root)
    folder.mkdir(parents=True)
    before = everything_under(root)
    with read_only(folder):
        error = refusal(lambda: stills.generate(clip, root))
    assert error.folder == folder and error.reason == "Permission denied"
    assert str(error) == f"could not create a temporary still in {folder}: Permission denied"
    assert rendered == []
    assert everything_under(root) == before


def test_the_videos_own_folder_read_only_and_the_stills_folder_not(
    root: Path, rendered: list[Path]
) -> None:
    """``chmod a-w`` on the library alone: the frame is taken, its pairing record is refused.

    The record goes beside the video, and a still is published with its
    record or not at all, so the still is taken back and the folder that
    refused is the video's.
    """
    clip = video(root)
    folder = pairing.still_directory(root)
    folder.mkdir(parents=True)
    with read_only(root):
        error = refusal(lambda: stills.generate(clip, root))
    assert error.folder == root and error.reason == "Permission denied"
    assert "could not write" in str(error) and str(error).endswith(": Permission denied")
    assert len(rendered) == 1
    assert not stills.destination(clip, root).exists(), "no still without its record"
    assert pairing.find_still(clip, roots=(root,)) is None


def test_a_frame_that_cannot_be_captured_is_not_a_folder_that_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg can't read the video, in a library that takes writes: a plain `StillError`."""
    clip = video(root)
    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(
        stills, "_run", lambda *_a, **_k: "Invalid data found when processing input"
    )
    with pytest.raises(stills.StillError, match="ffmpeg could not take a still") as raised:
        stills.generate(clip, root)
    assert not isinstance(raised.value, stills.StillFolderError)
    # And where the folder is read-only too, it is the folder: nothing was captured from.
    with read_only(pairing.still_directory(root)):
        assert refusal(lambda: stills.generate(clip, root)).reason == "Permission denied"


def test_a_pairing_record_that_cannot_be_read_is_not_its_folder_refusing(
    root: Path, rendered: list[Path]
) -> None:
    """The same "Permission denied", from one file in a library that takes writes."""
    clip = video(root)
    record = clip.with_name(clip.name + pairing.SIDECAR_SUFFIX)
    record.write_text("{}", encoding="utf-8")
    record.chmod(0)
    try:
        if os.access(record, os.R_OK):
            pytest.skip("permission bits don't bind root")
        with pytest.raises(stills.StillError, match="Permission denied") as raised:
            stills.generate(clip, root)
    finally:
        record.chmod(0o600)
    assert not isinstance(raised.value, stills.StillFolderError)
    assert not stills.destination(clip, root).exists()


def item(path: Path, kind: Kind, scene: str = "") -> MediaItem:
    return MediaItem(path=path, kind=kind, size=1, mtime=0, scene=scene)


def test_ensure_hands_the_refusal_to_a_caller_that_asks_and_still_forgives(
    root: Path, rendered: list[Path]
) -> None:
    clip = item(video(root), Kind.VIDEO)
    heard: list[stills.StillFolderError] = []
    with read_only(root):
        assert stills.ensure(clip, root) is None, "as before, for a caller that doesn't ask"
        assert stills.ensure(clip, root, refused=heard.append) is None
    assert [(error.folder, error.reason) for error in heard] == [(root, "Permission denied")]
    # Writable: made, and nothing to hear.
    assert stills.ensure(clip, root, refused=heard.append) == stills.destination(clip.path, root)
    assert len(heard) == 1


def test_ensure_keeps_a_capture_failure_to_itself(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = item(video(root), Kind.VIDEO)
    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(stills, "_run", lambda *_a, **_k: "moov atom not found")
    heard: list[stills.StillFolderError] = []
    assert stills.ensure(clip, root, refused=heard.append) is None
    assert heard == []


@pytest.fixture
def scene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MediaItem:
    """A Workshop scene, the engine installed and a display measured; it is never run here."""
    folder = tmp_path / "workshop" / SCENE
    folder.mkdir(parents=True)
    monkeypatch.setattr(scenes, "is_available", lambda: True)
    monkeypatch.setattr(scenes, "measured_capture_size", lambda: DISPLAY)

    def engine(*_arguments: object, **_keywords: object) -> None:
        raise AssertionError("the engine was run for a still its folder can't take")

    monkeypatch.setattr(scenes, "screenshot", engine)
    return item(folder, Kind.SCENE, SCENE)


def test_a_scene_capture_in_a_library_that_cannot_take_the_stills_folder(
    root: Path, scene: MediaItem
) -> None:
    with read_only(root):
        error = refusal(lambda: stills.capture_scene(scene, root, force=True))
    assert error.folder == root and error.reason == "Permission denied"
    assert str(error) == f"could not create {pairing.still_directory(root)}: Permission denied"
    assert everything_under(root) == []


def test_a_scene_capture_with_the_stills_folder_read_only_on_its_own(
    root: Path, scene: MediaItem
) -> None:
    folder = pairing.still_directory(root)
    folder.mkdir(parents=True)
    heard: list[stills.StillFolderError] = []
    with read_only(folder):
        error = refusal(lambda: stills.capture_scene(scene, root, force=True))
        assert stills.ensure(scene, root, size=DISPLAY, refused=heard.append) is None
    assert error.folder == folder and error.reason == "Permission denied"
    assert str(error) == f"could not create a temporary still in {folder}: Permission denied"
    assert [heard_error.folder for heard_error in heard] == [folder]
    assert list(folder.iterdir()) == []
