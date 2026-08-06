from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from wall_in_one import thumbnails
from wall_in_one.library.model import Kind, MediaItem

needs_ffmpeg = pytest.mark.skipif(not thumbnails.is_available(), reason="ffmpeg is not installed")


@pytest.fixture
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path


def _item(path: Path, kind: Kind = Kind.STILL, *, size: int = 1, mtime: int = 0) -> MediaItem:
    return MediaItem(path=path, kind=kind, size=size, mtime=mtime)


def _make_png(path: Path, *, width: int = 64, height: int = 64) -> Path:
    """A real image, made by ffmpeg so the test needs no image library."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=1",
            "-frames:v", "1", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def _make_video(path: Path, *, seconds: int = 3) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


# -- cache keys ----------------------------------------------------------


def test_the_key_changes_when_the_file_does(cache_home: Path) -> None:
    """Replacing a wallpaper in place must not keep serving the old thumbnail."""
    original = _item(Path("/w/a.png"), size=100, mtime=1)
    edited = _item(Path("/w/a.png"), size=200, mtime=2)
    assert thumbnails.cache_key(original) != thumbnails.cache_key(edited)


def test_different_files_do_not_collide(cache_home: Path) -> None:
    first = _item(Path("/w/a.png"))
    second = _item(Path("/w/b.png"))
    assert thumbnails.cache_key(first) != thumbnails.cache_key(second)


def test_the_key_is_stable_for_the_same_input(cache_home: Path) -> None:
    item = _item(Path("/w/a.png"))
    assert thumbnails.cache_key(item) == thumbnails.cache_key(item)


def test_cached_path_lives_under_the_cache_home(cache_home: Path) -> None:
    path = thumbnails.cached_path(_item(Path("/w/a.png")))
    assert cache_home in path.parents
    assert path.suffix == ".png"


def test_video_seeks_before_input(cache_home: Path) -> None:
    """`-ss` before `-i` is the fast seek; after it, ffmpeg decodes the lot."""
    command = thumbnails._command(_item(Path("/w/a.mp4"), Kind.VIDEO), Path("/out.png"))
    assert "-ss" in command
    assert command.index("-ss") < command.index("-i")


def test_stills_do_not_seek(cache_home: Path) -> None:
    command = thumbnails._command(_item(Path("/w/a.png")), Path("/out.png"))
    assert "-ss" not in command


# -- generation ----------------------------------------------------------


def test_a_missing_file_is_reported(cache_home: Path) -> None:
    with pytest.raises(thumbnails.ThumbnailError, match="no such file"):
        thumbnails.generate(_item(Path("/w/gone.png")))


def test_missing_ffmpeg_is_reported(cache_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(thumbnails, "is_available", lambda: False)
    with pytest.raises(thumbnails.ThumbnailError, match="ffmpeg is not installed"):
        thumbnails.generate(_item(Path("/w/a.png")))


@needs_ffmpeg
def test_generates_a_thumbnail_at_the_tile_size(cache_home: Path, tmp_path: Path) -> None:
    source = _make_png(tmp_path / "wallpaper.png", width=1920, height=1080)
    info = source.stat()
    item = _item(source, size=info.st_size, mtime=int(info.st_mtime))

    produced = thumbnails.generate(item)

    assert produced.is_file()
    assert produced == thumbnails.cached_path(item)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(produced)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert probe.stdout.strip() == f"{thumbnails.THUMBNAIL_WIDTH},{thumbnails.THUMBNAIL_HEIGHT}"


@needs_ffmpeg
def test_a_tall_image_is_cropped_not_squashed(cache_home: Path, tmp_path: Path) -> None:
    source = _make_png(tmp_path / "portrait.png", width=400, height=1200)
    item = _item(source, size=source.stat().st_size)

    produced = thumbnails.generate(item)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(produced)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert probe.stdout.strip() == f"{thumbnails.THUMBNAIL_WIDTH},{thumbnails.THUMBNAIL_HEIGHT}"


@needs_ffmpeg
def test_generates_a_frame_from_a_video(cache_home: Path, tmp_path: Path) -> None:
    source = _make_video(tmp_path / "clip.mp4")
    item = _item(source, Kind.VIDEO, size=source.stat().st_size)

    produced = thumbnails.generate(item)

    assert produced.is_file() and produced.stat().st_size > 0


@needs_ffmpeg
def test_the_second_call_reuses_the_cache(cache_home: Path, tmp_path: Path) -> None:
    source = _make_png(tmp_path / "wallpaper.png")
    item = _item(source, size=source.stat().st_size)

    first = thumbnails.generate(item)
    stamp = first.stat().st_mtime_ns
    second = thumbnails.generate(item)

    assert second == first
    assert second.stat().st_mtime_ns == stamp


@needs_ffmpeg
def test_a_file_that_is_not_an_image_fails_cleanly(cache_home: Path, tmp_path: Path) -> None:
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"this is not a png")
    item = _item(junk, size=junk.stat().st_size)

    with pytest.raises(thumbnails.ThumbnailError, match="ffmpeg failed"):
        thumbnails.generate(item)

    # Nothing half-written may survive a failure.
    assert not thumbnails.cached_path(item).exists()
    leftovers = list(thumbnails.cache_directory().glob("*.tmp.png"))
    assert leftovers == []


# -- pruning -------------------------------------------------------------


def test_prune_removes_the_oldest_past_the_limit(cache_home: Path) -> None:
    directory = thumbnails.cache_directory()
    directory.mkdir(parents=True)
    for index in range(6):
        entry = directory / f"{index:032x}.png"
        entry.write_bytes(b"x")
        os.utime(entry, (index, index))

    removed = thumbnails.prune(limit=2)

    assert removed == 4
    survivors = sorted(path.name for path in directory.glob("*.png"))
    assert survivors == [f"{4:032x}.png", f"{5:032x}.png"]


def test_prune_under_the_limit_does_nothing(cache_home: Path) -> None:
    directory = thumbnails.cache_directory()
    directory.mkdir(parents=True)
    (directory / "a.png").write_bytes(b"x")
    assert thumbnails.prune(limit=10) == 0


def test_prune_on_a_missing_cache_is_harmless(cache_home: Path) -> None:
    assert thumbnails.prune() == 0
