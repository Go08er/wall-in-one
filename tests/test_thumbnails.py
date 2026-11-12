from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from wall_in_one import decoding, thumbnails
from wall_in_one.library import scan, workshop
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
    command = thumbnails._command(
        _item(Path("/w/a.mp4"), Kind.VIDEO), Path("/proc/1/fd/3"), Path("/out.png")
    )
    assert "-ss" in command
    assert command.index("-ss") < command.index("-i")


def test_stills_do_not_seek(cache_home: Path) -> None:
    command = thumbnails._command(_item(Path("/w/a.png")), Path("/proc/1/fd/3"), Path("/out.png"))
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

    assert thumbnails._thumbnail_source(scene) == still


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

    assert thumbnails._thumbnail_source(scene) == preview


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


@needs_ffmpeg
def test_legacy_predictable_thumbnail_temporary_cannot_redirect_ffmpeg(
    cache_home: Path, tmp_path: Path
) -> None:
    source = _make_png(tmp_path / "source.png")
    status = source.stat()
    item = _item(source, size=status.st_size, mtime=int(status.st_mtime))
    destination = thumbnails.cached_path(item)
    destination.parent.mkdir(parents=True)
    sentinel = tmp_path / "sentinel.png"
    sentinel.write_bytes(b"keep")
    leftover = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp{destination.suffix}")
    leftover.symlink_to(sentinel)

    assert thumbnails.generate(item) == destination

    assert sentinel.read_bytes() == b"keep"
    assert leftover.is_symlink()
    assert destination.is_file()


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


# -- ffmpeg is handed the file, never its name (R42-10) --------------------


def _made(path: Path, colour: str, *encoding: str, seconds: int = 0) -> Path:
    """A real picture of one ``colour`` (or ``seconds`` of animation), skipped if not made."""
    source = f"color={colour}:size=64x36:rate=5" + (f":duration={seconds}" if seconds else "")
    frames = () if seconds else ("-frames:v", "1")
    made = path.with_name(f"made{path.suffix}")  # ffmpeg's own output would read a % too
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source, *frames, *encoding, str(made)],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0 or not made.is_file():
        pytest.skip(f"this ffmpeg cannot make {path.name}")
    return made.rename(path)


def _first_pixel(thumbnail: Path) -> tuple[int, ...]:
    """The first pixel of a thumbnail the app made (a cache name: no ``%`` in it)."""
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", *decoding.input_options(), "-i", str(thumbnail),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True, check=True, timeout=10,
    ).stdout  # fmt: skip
    return tuple(decoded[:3])


def _red(pixel: tuple[int, ...]) -> bool:
    return len(pixel) == 3 and pixel[0] > 200 and pixel[1] < 60 and pixel[2] < 60


def _scene_showing(still: Path) -> MediaItem:
    """A scene whose thumbnail is drawn from ``still``, its paired still."""
    directory = still.parent / "123"
    directory.mkdir()
    return MediaItem(
        path=directory, kind=Kind.SCENE, size=1, mtime=1, scene="123", paired_still=still
    )


@needs_ffmpeg
@pytest.mark.parametrize("scene", [False, True], ids=["still", "scene-still"])
@pytest.mark.parametrize(
    ("name", "neighbour"),
    [("50%.png", None), ("frame%02d.png", "frame00.png"), ("shot%d.jpg", "shot0.jpg")],
)
def test_a_picture_with_a_percent_in_its_name_is_thumbnailed_from_itself(
    cache_home: Path, tmp_path: Path, name: str, neighbour: str | None, scene: bool
) -> None:
    """Your own file may have ``%`` in its name. ffmpeg's image2 read ``frame%02d.png`` as
    a numbered sequence and decoded ``frame00.png`` beside it (on 534a9da)."""
    source = _made(tmp_path / name, "red")
    if neighbour is not None:
        _made(tmp_path / neighbour, "blue")
    item = _scene_showing(source) if scene else _item(source, size=source.stat().st_size)

    assert _red(_first_pixel(thumbnails.generate(item)))


@needs_ffmpeg
def test_a_video_named_like_a_sequence_is_thumbnailed_from_itself(
    cache_home: Path, tmp_path: Path
) -> None:
    """The video route, its seek before the input, from a descriptor. image2 never took
    a video's name (an animated ``.gif`` is one), so this held on 534a9da too."""
    source = _made(tmp_path / "loop%d.gif", "red", seconds=3)
    _made(tmp_path / "loop0.gif", "blue", seconds=3)
    item = _item(source, Kind.VIDEO, size=source.stat().st_size)

    assert _red(_first_pixel(thumbnails.generate(item)))


@needs_ffmpeg
def test_a_cache_folder_with_a_percent_in_its_name_still_gets_thumbnails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f6 N4: the output is a name to ffmpeg too, and image2's muxer read ``pct%dir`` as
    ``pct1ir``, which isn't there: every thumbnail failed (on 86840ed)."""
    cache = tmp_path / "pct%dir"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    source = _made(tmp_path / "red.png", "red")
    item = _item(source, size=source.stat().st_size)

    thumbnail = thumbnails.generate(item)

    assert cache in thumbnail.parents
    with decoding.opened(thumbnail) as opened:
        assert _red(_first_pixel(opened))
    assert not (tmp_path / "pct1ir").exists()


@needs_ffmpeg
@pytest.mark.parametrize(
    ("name", "kind", "encoding", "seconds"),
    [("wallpaper.png", Kind.STILL, (), 0), ("clip.mp4", Kind.VIDEO, ("-pix_fmt", "yuv420p"), 3)],
)
def test_a_wallpaper_that_is_a_link_now_is_not_followed(
    cache_home: Path,
    tmp_path: Path,
    name: str,
    kind: Kind,
    encoding: tuple[str, ...],
    seconds: int,
) -> None:
    """The library's walk lists no link: one at the name since then is refused, unread."""
    elsewhere = _made(
        tmp_path / f"elsewhere{Path(name).suffix}", "blue", *encoding, seconds=seconds
    )
    link = tmp_path / name
    link.symlink_to(elsewhere)
    item = _item(link, kind, size=1)

    with pytest.raises(thumbnails.ThumbnailError, match="cannot open"):
        thumbnails.generate(item)
    assert not thumbnails.cached_path(item).exists()


@needs_ffmpeg
def test_a_scenes_paired_still_that_is_a_link_is_thumbnailed_and_its_preview_is_not(
    cache_home: Path, tmp_path: Path
) -> None:
    """f6 N3: ``Automatic Stills/<id>.png -> ~/Pictures/mine.png`` is paired and played,
    the link followed, but the thumbnailer refused it, a blank tile (on 86840ed). A scene's
    paired still is the app's own or one a person picked (f7 W1), so the thumbnail follows
    it too, still from a descriptor. A declared preview, the author's, is not followed."""
    real = _made(tmp_path / "real.png", "red")
    link = tmp_path / "still.png"
    link.symlink_to(real)

    assert _red(_first_pixel(thumbnails.generate(_scene_showing(link))))

    directory = tmp_path / "124"
    directory.mkdir()
    preview = directory / "preview.png"
    preview.symlink_to(real)
    scene = MediaItem(
        path=directory, kind=Kind.SCENE, size=1, mtime=1, scene="124", preview=preview
    )
    with pytest.raises(thumbnails.ThumbnailError, match="cannot open"):
        thumbnails.generate(scene)
    assert not thumbnails.cached_path(scene).exists()


@needs_ffmpeg
@pytest.mark.parametrize("folder", ["as-scanned", "a-link-now"])
def test_an_items_preview_is_read_in_its_own_folder_never_one_linked_in_its_place(
    cache_home: Path, tmp_path: Path, folder: str
) -> None:
    """f7 R42-18: only the preview's own name was opened without following a link, so a
    Workshop item's folder replaced by a link since the scan led the thumbnail to the picture
    of that name wherever the link pointed (on c8b2fddb). The folder it is in is opened without
    following one too; Steam's folders above it may still be reached through a link."""
    steam = tmp_path / "disk" / "steam"
    item_folder = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID / "200"
    item_folder.mkdir(parents=True)
    _made(item_folder / "preview.png", "red")
    project = json.dumps({"type": "scene", "file": "scene.json", "preview": "preview.png"})
    (item_folder / "project.json").write_text(project, encoding="utf-8")
    (item_folder / "scene.json").write_text("{}", encoding="utf-8")
    (tmp_path / "steam").symlink_to(steam, target_is_directory=True)
    library = tmp_path / "library"
    library.mkdir()
    found = scan.scan((library,), {}, include_workshop=True, workshop_roots=(tmp_path / "steam",))
    item = next(item for item in found.items if item.scene == "200")
    assert item.preview == tmp_path / "steam" / item_folder.relative_to(steam) / "preview.png"
    assert item.paired_still is None
    if folder == "as-scanned":
        assert _red(_first_pixel(thumbnails.generate(item)))
        return
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _made(elsewhere / "preview.png", "blue")
    (elsewhere / "project.json").write_text(project, encoding="utf-8")
    item_folder.rename(item_folder.with_name("old-200"))
    item_folder.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(thumbnails.ThumbnailError, match="cannot open"):
        thumbnails.generate(item)
    assert not thumbnails.cached_path(item).exists()


@needs_ffmpeg
def test_a_name_handed_to_ffmpeg_is_never_read_as_a_sequence(tmp_path: Path) -> None:
    """`decoding.FORMATS` leaves image2 out, so even a name handed over is no pattern."""
    source = _made(tmp_path / "preview%01d.png", "red")
    _made(tmp_path / "preview0.png", "blue")
    done = subprocess.run(
        ["ffmpeg", "-v", "error", *decoding.input_options(), "-i", str(source),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True, check=False, timeout=10,
    )  # fmt: skip
    assert done.returncode != 0 or _red(tuple(done.stdout[:3])), tuple(done.stdout[:3])


@needs_ffmpeg
@pytest.mark.parametrize(
    ("name", "kind", "encoding", "seconds"),
    [
        ("still.png", Kind.STILL, (), 0),
        ("still.jpg", Kind.STILL, (), 0),
        ("still.webp", Kind.STILL, ("-c:v", "libwebp"), 0),
        ("still.avif", Kind.STILL, ("-c:v", "libaom-av1"), 0),
        ("animated.webp", Kind.STILL, ("-c:v", "libwebp_anim", "-loop", "0"), 1),
        ("animated.png", Kind.STILL, ("-f", "apng", "-plays", "0"), 1),
        ("animated.jxl", Kind.STILL, ("-c:v", "libjxl_anim"), 1),
        ("animated.gif", Kind.VIDEO, (), 3),
        ("clip.mp4", Kind.VIDEO, ("-pix_fmt", "yuv420p"), 3),
        ("clip.mkv", Kind.VIDEO, ("-pix_fmt", "yuv420p"), 3),
        ("clip.webm", Kind.VIDEO, (), 3),
    ],
)
def test_every_kind_of_wallpaper_is_still_thumbnailed_from_its_descriptor(
    cache_home: Path,
    tmp_path: Path,
    name: str,
    kind: Kind,
    encoding: tuple[str, ...],
    seconds: int,
) -> None:
    """Handed a descriptor's path, with no extension, ffmpeg picks the demuxer by the bytes."""
    source = _made(tmp_path / name, "red", *encoding, seconds=seconds)
    item = _item(source, kind, size=source.stat().st_size)

    assert _red(_first_pixel(thumbnails.generate(item)))


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
