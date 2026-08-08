from __future__ import annotations

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


def test_a_scene_uses_its_generated_still_instead_of_its_directory(
    cache_home: Path, tmp_path: Path
) -> None:
    directory = tmp_path / "123"
    directory.mkdir()
    still = tmp_path / "123.png"
    still.write_bytes(b"still")
    scene = MediaItem(
        path=directory,
        kind=Kind.SCENE,
        size=directory.stat().st_size,
        mtime=int(directory.stat().st_mtime),
        scene="123",
        paired_still=still,
    )

    command = thumbnails._command(scene, Path("/out.png"))

    assert command[command.index("-i") + 1] == str(still)


def test_a_scene_falls_back_to_the_workshop_preview(cache_home: Path, tmp_path: Path) -> None:
    directory = tmp_path / "123"
    directory.mkdir()
    preview = directory / "preview.gif"
    preview.write_bytes(b"preview")
    scene = MediaItem(
        path=directory,
        kind=Kind.SCENE,
        size=directory.stat().st_size,
        mtime=int(directory.stat().st_mtime),
        scene="123",
        preview=preview,
    )

    command = thumbnails._command(scene, Path("/out.png"))

    assert command[command.index("-i") + 1] == str(preview)


def test_a_scene_cache_key_changes_when_its_still_changes(cache_home: Path, tmp_path: Path) -> None:
    directory = tmp_path / "123"
    directory.mkdir()
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"one")
    second.write_bytes(b"two two")
    base = MediaItem(
        path=directory,
        kind=Kind.SCENE,
        size=1,
        mtime=1,
        scene="123",
        paired_still=first,
    )

    changed = MediaItem(
        path=directory,
        kind=Kind.SCENE,
        size=1,
        mtime=1,
        scene="123",
        paired_still=second,
    )
    assert thumbnails.cache_key(base) != thumbnails.cache_key(changed)


# -- generation ----------------------------------------------------------


def test_a_missing_file_is_reported(cache_home: Path) -> None:
    with pytest.raises(thumbnails.ThumbnailError, match="no such file"):
        thumbnails.generate(_item(Path("/w/gone.png")))


def test_a_scene_with_no_still_or_preview_is_reported(cache_home: Path, tmp_path: Path) -> None:
    scene = MediaItem(
        path=tmp_path,
        kind=Kind.SCENE,
        size=1,
        mtime=1,
        scene="123",
    )
    with pytest.raises(thumbnails.ThumbnailError, match="no still or preview"):
        thumbnails.generate(scene)


@needs_ffmpeg
def test_generates_a_scene_thumbnail_from_its_still(cache_home: Path, tmp_path: Path) -> None:
    directory = tmp_path / "123"
    directory.mkdir()
    still = _make_png(tmp_path / "scene.png", width=1920, height=1080)
    scene = MediaItem(
        path=directory,
        kind=Kind.SCENE,
        size=directory.stat().st_size,
        mtime=int(directory.stat().st_mtime),
        scene="123",
        paired_still=still,
    )

    assert thumbnails.generate(scene).is_file()


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


# -- remote previews -----------------------------------------------------


def _make_webp(path: Path) -> bytes:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=64x36:rate=1",
            "-frames:v", "1", "-c:v", "libwebp", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path.read_bytes()


def test_png_and_jpeg_are_recognised_without_decoding() -> None:
    assert thumbnails.is_natively_decodable(b"\x89PNG\r\n\x1a\n rest")
    assert thumbnails.is_natively_decodable(b"\xff\xd8\xff\xe0 rest")
    assert not thumbnails.is_natively_decodable(b"RIFF____WEBP")


@needs_ffmpeg
def test_a_png_preview_is_passed_through_untouched(tmp_path: Path) -> None:
    """No ffmpeg process for the common case: GTK decodes PNG itself."""
    data = _make_png(tmp_path / "a.png").read_bytes()
    assert thumbnails.to_displayable(data) is data


@needs_ffmpeg
def test_a_webp_preview_is_transcoded_for_gtk(tmp_path: Path) -> None:
    # MotionBGS serves webp, and this closure's GdkPixbuf has no webp loader.
    converted = thumbnails.to_displayable(_make_webp(tmp_path / "a.webp"))
    assert thumbnails.is_natively_decodable(converted)


def test_an_empty_preview_stays_empty() -> None:
    assert thumbnails.to_displayable(b"") == b""


@needs_ffmpeg
def test_a_preview_that_is_not_an_image_yields_nothing() -> None:
    """An error page must not become a broken texture."""
    assert thumbnails.to_displayable(b"<html>not an image</html>") == b""


def test_a_preview_without_ffmpeg_is_dropped_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(thumbnails, "is_available", lambda: False)
    assert thumbnails.to_displayable(b"RIFF____WEBPsomething") == b""
