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

from tests.test_scenes import where_the_engine_looks
from wall_in_one import worker_processes
from wall_in_one.library import pairing, scan, stills
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
    assert pairing.find_still(clip, roots=(root,), labelled_workshop=False) is None


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


def test_a_pairing_record_that_cannot_be_read_does_not_stop_the_still_nor_get_touched(
    root: Path, rendered: list[Path]
) -> None:
    """F9: it is ignored like the readers ignore it, left byte for byte, and the still is made.

    Before, the writer refused ("Permission denied") and `ensure` swallowed it, so the
    video never got a still, silently, and the person had to find a hidden file.
    """
    clip = video(root)
    record = clip.with_name(clip.name + pairing.SIDECAR_SUFFIX)
    record.write_text("{}", encoding="utf-8")
    record.chmod(0)
    try:
        if os.access(record, os.R_OK):
            pytest.skip("permission bits don't bind root")
        made = stills.generate(clip, root)
    finally:
        record.chmod(0o600)
    assert made == stills.destination(clip, root) and made.is_file()
    assert record.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("shape", ["symlink", "oversize", "directory"])
def test_a_sidecar_the_writer_cannot_read_never_blocks_ensure(
    root: Path, rendered: list[Path], shape: str
) -> None:
    clip = video(root)
    record = clip.with_name(clip.name + pairing.SIDECAR_SUFFIX)
    if shape == "symlink":
        target = root / "elsewhere.json"
        target.write_text("{}", encoding="utf-8")
        record.symlink_to(target)
    elif shape == "oversize":
        record.write_bytes(b" " * (pairing.MAX_SIDECAR_BYTES + 1))
    else:
        record.mkdir()
    found = stills.ensure(item(clip, Kind.VIDEO), root)
    assert found == stills.destination(clip, root) and found.is_file()
    assert record.is_symlink() or record.exists(), "left in place"


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
    where_the_engine_looks(SCENE)
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


# -- f7 P1: a library folder reached through a link higher up its path ---------------------------


def _linked_library(tmp_path: Path, depth: str) -> Path:
    """``home/u/Videos/Wallpapers``, where ``Videos`` (or ``home``) links to another disk."""
    disk = tmp_path / "disk"
    if depth == "parent":
        (disk / "Videos" / "Wallpapers").mkdir(parents=True)
        (tmp_path / "home" / "u").mkdir(parents=True)
        (tmp_path / "home" / "u" / "Videos").symlink_to(disk / "Videos")
    else:
        (disk / "home" / "u" / "Videos" / "Wallpapers").mkdir(parents=True)
        (tmp_path / "home").symlink_to(disk / "home")
    return tmp_path / "home" / "u" / "Videos" / "Wallpapers"


@pytest.mark.parametrize("depth", ["parent", "higher-up"])
def test_a_library_folder_reached_through_a_link_gets_its_stills_and_keeps_them(
    tmp_path: Path, rendered: list[Path], depth: str
) -> None:
    """The capture's folder was walked from ``/`` without following a link, so a library
    folder reached through one (``~/Videos`` on another disk) failed every capture with
    "Not a directory", and nothing said so: its videos never got a still, and stayed out of
    playlists (on 86840ed, and since 0.1.0). The folders above it are resolved now; it and
    everything below it are still walked without links. A rescan pairs the still, and it is
    not taken again."""
    root = _linked_library(tmp_path, depth)
    clip = video(root)

    made = stills.generate(clip, root)

    assert made == stills.destination(clip, root) and made.is_file()
    found = scan.scan([root], {}).find(clip)
    assert found is not None and found.paired_still == made
    assert stills.ensure(found, root) == made
    assert len(rendered) == 1, "taken once"


def test_a_scene_still_is_captured_into_a_library_folder_reached_through_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scene route pins the same folder (`stills.capture_scene`)."""
    from tests.test_scene_preset_stills import _engine

    root = _linked_library(tmp_path, "parent")
    folder = tmp_path / "workshop" / SCENE
    folder.mkdir(parents=True)
    asked = _engine(monkeypatch)
    where_the_engine_looks(SCENE)

    made = stills.capture_scene(item(folder, Kind.SCENE, SCENE), root, force=True, size=(64, 64))

    assert made == pairing.still_directory(root) / f"{SCENE}.png" and made.is_file()
    assert len(asked) == 1


@pytest.mark.parametrize("linked", ["Wall-in-One", "Automatic Stills"])
def test_a_link_inside_the_library_folder_is_still_not_written_through_and_is_said(
    tmp_path: Path, rendered: list[Path], linked: str
) -> None:
    """Below the library folder nothing is followed, as before; and what was a plain
    `StillError`, which nothing said, is now the folder's refusal, which the still maker says."""
    root = tmp_path / "library"
    root.mkdir()
    clip = video(root)
    kept = root / "kept"
    if linked == "Wall-in-One":
        (kept / "Automatic Stills").mkdir(parents=True)
        (root / "Wall-in-One").symlink_to(kept)
    else:
        kept.mkdir()
        (root / "Wall-in-One").mkdir()
        (root / "Wall-in-One" / "Automatic Stills").symlink_to(kept)
    assert pairing.still_directory_trusted(root), "inside the library folder: W1 trusts it"
    heard: list[stills.StillFolderError] = []

    assert stills.ensure(item(clip, Kind.VIDEO), root, refused=heard.append) is None

    assert len(heard) == 1 and "symbolic link" in heard[0].reason, heard
    # The folder said is the one that holds the link (f7 H5), not Automatic Stills.
    assert heard[0].folder == (root if linked == "Wall-in-One" else root / "Wall-in-One")
    assert heard[0].reason.startswith(f"{linked} in it is a symbolic link"), heard[0].reason
    assert rendered == [], "refused before ffmpeg was run"
    assert list(kept.rglob("*.png")) == []


def test_a_refusal_nobody_asked_to_hear_is_logged_not_dropped(
    tmp_path: Path, rendered: list[Path], caplog: pytest.LogCaptureFixture
) -> None:
    """`stills.ensure` without ``refused`` (the paused video's still, `Session`) dropped the
    folder's refusal; it is logged now (f7 P1)."""
    root = tmp_path / "library"
    root.mkdir()
    clip = video(root)
    (root / "kept").mkdir()
    (root / "Wall-in-One").symlink_to(root / "kept")
    with caplog.at_level("WARNING", logger=stills.LOGGER.name):
        assert stills.ensure(item(clip, Kind.VIDEO), root) is None
    assert any("could not safely retain" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("gone", ["changed", "vanished"])
def test_a_still_folder_that_changed_or_went_since_the_scan_is_not_its_refusal(
    root: Path, rendered: list[Path], monkeypatch: pytest.MonkeyPatch, gone: str
) -> None:
    """f7 H5: P1 made every failure of the pin the folder's refusal, so a folder replaced or
    gone between the scan and the capture was toasted as one that "cannot be written". It is
    a plain `StillError` again, as before P1; a link or a permission stays the folder's."""
    from wall_in_one import file_io

    clip = video(root)

    def pin(*_arguments: object, **_keywords: object) -> object:
        if gone == "changed":
            raise file_io.PathChangedError(f"{root} is not the journaled filesystem root")
        raise FileNotFoundError(2, "No such file or directory", str(root))

    monkeypatch.setattr(file_io, "pin_directory_beneath", pin)
    heard: list[stills.StillFolderError] = []
    with pytest.raises(stills.StillError) as raised:
        stills.generate(clip, root)
    assert not isinstance(raised.value, stills.StillFolderError), raised.value
    assert stills.ensure(item(clip, Kind.VIDEO), root, refused=heard.append) is None
    assert heard == []
    assert rendered == []
