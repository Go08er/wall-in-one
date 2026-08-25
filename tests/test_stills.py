"""Making a still for a video, and pairing the two.

The generator shells out to ffmpeg, so these tests build real one-second clips
with it rather than stubbing it out: the failures worth catching here -- a seek
past the end of a short loop, a torn file left by a crash, a still the read
half cannot then find -- all live in the part a stub would replace.

Nothing here touches the user's library. Every path is under `tmp_path`.
"""

from __future__ import annotations

import os
import random
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from wall_in_one import config, file_io, paths
from wall_in_one.library import pairing, pairings, scan, stills
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.session import Session
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper import scenes
from wall_in_one.wallpaper.applier import Applier

pytestmark = pytest.mark.skipif(not stills.is_available(), reason="ffmpeg is not installed")


def make_video(path: Path, *, seconds: float = 5.0, colour: str = "red") -> Path:
    """A real clip, because the point is what ffmpeg does with it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={colour}:s=320x180:d={seconds}:r=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def item(path: Path, kind: Kind, still: Path | None = None) -> MediaItem:
    """A `MediaItem` for a file that really exists, so size and mtime are real."""
    status = path.stat()
    return MediaItem(
        path=path,
        kind=kind,
        size=status.st_size,
        mtime=int(status.st_mtime),
        paired_still=still,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "wallpapers"


# -- taking the frame ----------------------------------------------------


def test_a_still_is_taken_into_the_managed_directory(root: Path, tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4")
    still = stills.generate(video, root)
    assert still == stills.destination(video, root)
    assert still.name.startswith("video-")
    assert still.stat().st_size > 0


def test_same_stem_videos_get_distinct_generated_stills(root: Path, tmp_path: Path) -> None:
    first = make_video(tmp_path / "one" / "intro.mp4", colour="red")
    second = make_video(tmp_path / "two" / "intro.mp4", colour="blue")

    first_still = stills.generate(first, root)
    second_still = stills.generate(second, root)

    assert first_still != second_still
    assert first_still.is_file()
    assert second_still.is_file()
    assert pairing.find_still(first, (root,)) == first_still
    assert pairing.find_still(second, (root,)) == second_still


def test_the_still_is_a_png_whatever_the_video_was(root: Path, tmp_path: Path) -> None:
    """Noctalia derives a palette from this file; JPEG would shift the colours."""
    video = make_video(tmp_path / "clip.mp4")
    still = stills.generate(video, root)
    assert still.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_a_clip_shorter_than_the_seek_still_yields_a_still(root: Path, tmp_path: Path) -> None:
    """Seeking three seconds into a one-second loop finds nothing; the first
    frame is the right answer for a clip that short."""
    video = make_video(tmp_path / "short.mp4", seconds=1.0)
    still = stills.generate(video, root)
    assert still.is_file()
    assert still.stat().st_size > 0


def test_the_still_is_taken_at_full_resolution(root: Path, tmp_path: Path) -> None:
    """This becomes a wallpaper, not a thumbnail: no scaling, no cropping."""
    video = make_video(tmp_path / "clip.mp4")
    still = stills.generate(video, root)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(still),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip().rstrip(",") == "320,180"


def test_an_existing_still_is_reused_rather_than_re_encoded(root: Path, tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4")
    first = stills.generate(video, root)
    stamp = first.stat().st_mtime_ns
    again = stills.generate(video, root)
    assert again == first
    assert again.stat().st_mtime_ns == stamp


def test_forcing_replaces_the_existing_still(root: Path, tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4")
    first = stills.generate(video, root)
    first.write_bytes(b"")
    again = stills.generate(video, root, force=True)
    assert again.stat().st_size > 0


def test_a_missing_video_is_refused_without_leaving_anything(root: Path, tmp_path: Path) -> None:
    with pytest.raises(stills.StillError):
        stills.generate(tmp_path / "absent.mp4", root)
    assert not root.exists()


def test_a_file_that_is_not_a_video_leaves_no_torn_still(root: Path, tmp_path: Path) -> None:
    """A half-written still is worse than none: `pairing` would find it and the
    user would get a torn frame as their wallpaper."""
    impostor = tmp_path / "clip.mp4"
    impostor.write_bytes(b"not a video at all")
    with pytest.raises(stills.StillError):
        stills.generate(impostor, root)
    directory = pairing.still_directory(root)
    assert not stills.destination(impostor, root).exists()
    assert not any(path.name.endswith(".tmp.png") for path in directory.iterdir())
    assert not any(path.name.startswith(".wall-in-one-capture-") for path in directory.iterdir())
    retained = tuple((directory / file_io.RETAINED_ENTRY_DIRECTORY).iterdir())
    tombstones = tuple(path for path in retained if path.is_file())
    assert len(tombstones) == 1 and tombstones[0].stat().st_size == 0
    directories = tuple(path for path in retained if path.is_dir())
    assert len(directories) == 1 and tuple(directories[0].iterdir()) == ()


def test_a_video_mutated_in_place_during_capture_cannot_publish(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"original source")

    def mutate_source(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\nrendered")
        video.write_bytes(b"different source bytes and size")
        return ""

    monkeypatch.setattr(stills, "_run", mutate_source)
    target = stills.destination(video, root)

    with pytest.raises(stills.StillError, match="changed while its still was being made"):
        stills.generate(video, root)

    assert not target.exists()
    assert not any(path.name.endswith(".tmp.png") for path in target.parent.iterdir())
    assert not any(
        path.name.startswith(".wall-in-one-capture-") for path in target.parent.iterdir()
    )
    retained = tuple((target.parent / file_io.RETAINED_ENTRY_DIRECTORY).iterdir())
    tombstones = tuple(path for path in retained if path.is_file())
    assert len(tombstones) == 1 and tombstones[0].stat().st_size == 0
    directories = tuple(path for path in retained if path.is_dir())
    assert len(directories) == 1 and tuple(directories[0].iterdir()) == ()


def test_a_late_nonlocking_still_target_wins_without_being_overwritten(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"source")
    target = stills.destination(video, root)
    late = b"late non-participating writer"

    def render_then_publish_late(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\ngenerated")
        target.write_bytes(late)
        return ""

    monkeypatch.setattr(stills, "_run", render_then_publish_late)

    with pytest.raises(stills.StillError):
        stills.generate(video, root)

    assert target.read_bytes() == late


def test_force_never_claims_a_preobserved_target_after_external_output_changes(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"source")
    target = stills.destination(video, root)
    target.parent.mkdir(parents=True)
    original = b"pre-observed app still"
    target.write_bytes(original)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"precious")

    def redirect_external_output(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.symlink_to(sentinel)
        return ""

    monkeypatch.setattr(stills, "_run", redirect_external_output)

    with pytest.raises(stills.StillError):
        stills.generate(video, root, force=True)

    assert target.read_bytes() == original
    assert sentinel.read_bytes() == b"precious"


def test_force_preserves_a_late_replacement_of_the_frozen_target(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"source")
    target = stills.destination(video, root)
    target.parent.mkdir(parents=True)
    original = b"pre-observed app still"
    target.write_bytes(original)
    saved = tmp_path / "saved-original-still"
    late = b"late target generation"

    def replace_target_after_render(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\ngenerated")
        target.rename(saved)
        target.write_bytes(late)
        return ""

    monkeypatch.setattr(stills, "_run", replace_target_after_render)

    with pytest.raises(stills.StillError):
        stills.generate(video, root, force=True)

    assert target.read_bytes() == late
    assert saved.read_bytes() == original


def test_force_preserves_an_in_place_mutation_of_the_frozen_target(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"source")
    target = stills.destination(video, root)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"pre-observed app still")
    mutated = b"changed through an open writer"

    def mutate_target_after_render(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\ngenerated")
        target.write_bytes(mutated)
        return ""

    monkeypatch.setattr(stills, "_run", mutate_target_after_render)

    with pytest.raises(stills.StillError):
        stills.generate(video, root, force=True)

    assert target.read_bytes() == mutated


def test_unrelated_still_directory_entries_are_preserved_during_capture_cleanup(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"source")

    def add_extra_then_change_source(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\ngenerated")
        (pairing.still_directory(root) / "unrecognised").write_bytes(b"keep")
        video.write_bytes(b"changed source")
        return ""

    monkeypatch.setattr(stills, "_run", add_extra_then_change_source)

    with pytest.raises(stills.StillError):
        stills.generate(video, root)

    still_directory = pairing.still_directory(root)
    unrecognised = still_directory / "unrecognised"
    assert unrecognised.read_bytes() == b"keep"
    retained = tuple((still_directory / file_io.RETAINED_ENTRY_DIRECTORY).iterdir())
    retained_directories = tuple(path for path in retained if path.is_dir())
    assert len(retained_directories) == 1
    assert tuple(retained_directories[0].iterdir()) == ()
    tombstones = tuple(path for path in retained if path.is_file())
    assert len(tombstones) == 1 and tombstones[0].stat().st_size == 0


def test_capture_cleanup_stays_in_the_pinned_target_directory_generation(
    root: Path,
) -> None:
    target = pairing.still_directory(root) / "capture.png"
    target.parent.mkdir(parents=True)
    context = stills._pin_target_directory(root, target)
    temporary = stills._private_image_temporary(target, context)
    output_name = temporary.path.name
    saved = target.parent.with_name("saved-automatic-stills")
    target.parent.rename(saved)
    target.parent.mkdir(mode=0o755)
    sentinel = target.parent / "keep"
    sentinel.write_bytes(b"replacement")

    try:
        temporary.close()
    finally:
        context.close()

    assert sentinel.read_bytes() == b"replacement"
    assert not (saved / output_name).exists()


def test_capture_output_close_failure_still_releases_the_output_descriptor(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pairing.still_directory(root) / "capture.png"
    target.parent.mkdir(parents=True)
    context = stills._pin_target_directory(root, target)
    temporary = stills._private_image_temporary(target, context)
    temporary.path.write_bytes(b"\x89PNG\r\n\x1a\nrendered")
    temporary.retain_rendered()
    temporary.mark_published()
    output_pin = temporary.pin
    assert output_pin is not None
    output_descriptor = output_pin.descriptor
    logical_path = temporary.logical_path
    real_close = file_io.PinnedPath.close
    injected = False

    def fail_after_output_close(pin: file_io.PinnedPath) -> None:
        nonlocal injected
        is_output = pin is output_pin
        real_close(pin)
        if is_output and not injected:
            injected = True
            raise OSError("injected output close failure after release")

    monkeypatch.setattr(file_io.PinnedPath, "close", fail_after_output_close)
    try:
        with pytest.raises(OSError, match="injected output close failure"):
            temporary.close()
    finally:
        context.close()

    assert injected
    with pytest.raises(OSError):
        os.fstat(output_descriptor)
    assert logical_path.read_bytes() == b"\x89PNG\r\n\x1a\nrendered"


def test_capture_mkstemp_handoff_replacement_is_never_treated_as_owned(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pairing.still_directory(root) / "capture.png"
    target.parent.mkdir(parents=True)
    context = stills._pin_target_directory(root, target)
    real_mkstemp = tempfile.mkstemp
    saved = target.parent / "creation-time-output.png"
    replacement: Path | None = None

    def replace_before_return(*args: Any, **kwargs: Any) -> tuple[int, str]:
        nonlocal replacement
        descriptor, name = real_mkstemp(*args, **kwargs)
        if kwargs.get("prefix") != ".capture.png.":
            return descriptor, name
        created = Path(name)
        created.rename(saved)
        created.write_bytes(b"unrelated replacement")
        replacement = target.parent / created.name
        return descriptor, name

    monkeypatch.setattr(tempfile, "mkstemp", replace_before_return)
    try:
        with pytest.raises(file_io.PathChangedError):
            stills._private_image_temporary(target, context)
    finally:
        context.close()

    assert replacement is not None
    assert replacement.read_bytes() == b"unrelated replacement"
    assert saved.is_file() and saved.stat().st_size == 0


def test_target_directory_replacement_cannot_redirect_video_publication(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = root / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"source")
    target = stills.destination(video, root)
    saved_directory = target.parent.with_name("saved-automatic-stills")
    sentinel = b"replacement target"

    def replace_target_directory(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\ngenerated")
        target.parent.rename(saved_directory)
        target.parent.mkdir(mode=0o755)
        target.write_bytes(sentinel)
        return ""

    monkeypatch.setattr(stills, "_run", replace_target_directory)

    with pytest.raises(stills.StillError):
        stills.generate(video, root)

    assert target.read_bytes() == sentinel
    assert not video.with_name(video.name + pairing.SIDECAR_SUFFIX).exists()
    assert (saved_directory / file_io.RETAINED_ENTRY_DIRECTORY).is_dir()


# -- pairing the two -----------------------------------------------------


def test_generating_writes_a_sidecar_the_reader_understands(root: Path) -> None:
    """The end-to-end claim: what the write half produces, the read half finds.

    The video is inside the root, which is where a scanned one always is.
    """
    video = make_video(root / "clip.mp4")
    still = stills.generate(video, root)
    assert pairing.read_sidecar(video) == still


def test_a_late_pairing_sidecar_wins_without_being_overwritten(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = make_video(root / "clip.mp4")
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    late = b'{"notes":"keep"}\n'
    real_move = file_io.atomic_move_no_replace

    def publish_late(
        source: Path,
        destination: Path,
        **keywords: object,
    ) -> None:
        if destination.name == sidecar.name:
            destination.write_bytes(late)
        real_move(source, destination, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "atomic_move_no_replace", publish_late)

    with pytest.raises(stills.StillError):
        stills.generate(video, root)

    assert sidecar.read_bytes() == late
    assert not stills.destination(video, root).exists()


def test_target_rebind_during_sidecar_publication_rolls_back_the_new_sidecar(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = root / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"source")
    target = stills.destination(video, root)
    saved_directory = target.parent.with_name("saved-sidecar-target")
    sentinel = b"replacement still"
    real_record = stills._record_beside

    def render(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\ngenerated")
        return ""

    def rebind_then_record(
        video_path: Path,
        still: Path,
        library_root: Path,
        source: stills._SourceSnapshot,
    ) -> object:
        target.parent.rename(saved_directory)
        target.parent.mkdir(mode=0o755)
        target.write_bytes(sentinel)
        return real_record(video_path, still, library_root, source)

    monkeypatch.setattr(stills, "_record_beside", rebind_then_record)
    monkeypatch.setattr(stills, "_run", render)

    with pytest.raises(stills.StillError):
        stills.generate(video, root)

    assert target.read_bytes() == sentinel
    assert not (saved_directory / target.name).exists()
    assert not video.with_name(video.name + pairing.SIDECAR_SUFFIX).exists()


@pytest.mark.parametrize("reuse_existing", (False, True))
def test_source_replacement_during_sidecar_publication_rolls_back_the_new_sidecar(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reuse_existing: bool,
) -> None:
    video = root / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"source generation A")
    target = stills.destination(video, root)
    if reuse_existing:
        target.parent.mkdir(parents=True)
        existing_target = b"\x89PNG\r\n\x1a\nexisting"
        target.write_bytes(existing_target)
    old_source = root / "source-generation-a.mp4"
    replacement = b"unrelated generation B"
    real_record = stills._record_beside
    swapped = False

    def render(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\ngenerated")
        return ""

    def replace_source_then_record(
        video_path: Path,
        still: Path,
        library_root: Path,
        source: stills._SourceSnapshot,
    ) -> object:
        nonlocal swapped
        if not swapped:
            swapped = True
            video_path.rename(old_source)
            video_path.write_bytes(replacement)
        return real_record(video_path, still, library_root, source)

    monkeypatch.setattr(stills, "_record_beside", replace_source_then_record)
    monkeypatch.setattr(stills, "_run", render)

    with pytest.raises(stills.StillError, match="changed while its still was being made"):
        stills.generate(video, root)

    assert swapped
    assert video.read_bytes() == replacement
    assert old_source.read_bytes() == b"source generation A"
    assert not video.with_name(video.name + pairing.SIDECAR_SUFFIX).exists()
    if reuse_existing:
        assert target.read_bytes() == existing_target
    else:
        assert not target.exists()


def test_video_parent_aba_renders_through_the_retained_source_descriptor(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_parent = tmp_path / "source-parent"
    source_parent.mkdir()
    video = source_parent / "clip.mp4"
    generation_a = b"source generation A"
    video.write_bytes(generation_a)
    saved_parent = tmp_path / "saved-source-parent"
    replacement_parent = tmp_path / "replacement-source-parent"
    rendered = b""

    def render_during_parent_aba(
        retained_video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        nonlocal rendered
        del processes
        source_parent.rename(saved_parent)
        source_parent.mkdir()
        video.write_bytes(b"unrelated generation B")
        rendered = retained_video.read_bytes()
        temporary.write_bytes(b"\x89PNG\r\n\x1a\n" + rendered)
        source_parent.rename(replacement_parent)
        saved_parent.rename(source_parent)
        return ""

    monkeypatch.setattr(stills, "_run", render_during_parent_aba)

    target = stills.generate(video, root)

    assert rendered == generation_a
    assert target.read_bytes().endswith(generation_a)
    assert (replacement_parent / video.name).read_bytes() == b"unrelated generation B"


def test_video_parent_aba_cannot_redirect_sidecar_publication(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_parent = root / "videos"
    source_parent.mkdir(parents=True)
    video = source_parent / "clip.mp4"
    video.write_bytes(b"source generation A")
    saved_parent = root / "saved-video-parent"
    replacement_parent = root / "replacement-video-parent"
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    real_move = file_io.atomic_move_no_replace
    raced = False

    def render(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\ngenerated")
        return ""

    def rebind_parent_around_sidecar_move(
        source: Path,
        destination: Path,
        **keywords: object,
    ) -> None:
        nonlocal raced
        if destination.name == sidecar.name and not raced:
            raced = True
            source_parent.rename(saved_parent)
            source_parent.mkdir()
            video.write_bytes(b"unrelated generation B")
            real_move(source, destination, **keywords)  # type: ignore[arg-type]
            source_parent.rename(replacement_parent)
            saved_parent.rename(source_parent)
            return
        real_move(source, destination, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "atomic_move_no_replace", rebind_parent_around_sidecar_move)
    monkeypatch.setattr(stills, "_run", render)

    target = stills.generate(video, root)

    assert raced
    assert pairing.read_sidecar(video) == target
    assert not (replacement_parent / sidecar.name).exists()


def test_sidecar_directory_sync_failure_rolls_back_sidecar_and_fresh_still(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = make_video(root / "clip.mp4")
    target = stills.destination(video, root)
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    real_sync = paths.fsync_directory
    failed = False

    def fail_sidecar_sync(directory: Path) -> None:
        nonlocal failed
        directory_status = directory.stat()
        source_status = video.parent.stat()
        same_directory = (directory_status.st_dev, directory_status.st_ino) == (
            source_status.st_dev,
            source_status.st_ino,
        )
        if sidecar.exists() and same_directory and not failed:
            failed = True
            raise OSError("injected sidecar directory fsync failure")
        real_sync(directory)

    monkeypatch.setattr(paths, "fsync_directory", fail_sidecar_sync)

    with pytest.raises(stills.StillError, match="fsync failure"):
        stills.generate(video, root)

    assert failed
    assert not sidecar.exists()
    assert not target.exists()


def test_sidecar_publication_never_acquires_rollback_authority_over_a_replacement(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = root / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    chosen = root / "chosen.png"
    chosen.write_bytes(b"image")
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    saved = root / "saved-published-sidecar"
    real_sync = paths.fsync_directory
    swapped = False
    expected = b""

    def replace_after_publish(directory: Path) -> None:
        nonlocal expected, swapped
        if directory == sidecar.parent and sidecar.exists() and not swapped:
            swapped = True
            expected = sidecar.read_bytes()
            sidecar.rename(saved)
            sidecar.write_bytes(expected)
        real_sync(directory)

    monkeypatch.setattr(paths, "fsync_directory", replace_after_publish)

    with pytest.raises(stills.StillError):
        stills._write_sidecar_publication(video, chosen)

    assert swapped
    assert sidecar.read_bytes() == expected
    assert saved.read_bytes() == expected


def test_sidecar_staging_close_failure_releases_the_unreturned_public_pin(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = root / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    chosen = root / "chosen.png"
    chosen.write_bytes(b"image")
    real_dup = os.dup
    real_close = file_io.PinnedPath.close
    public_descriptor: int | None = None
    injected = False

    def record_public_duplicate(descriptor: int) -> int:
        nonlocal public_descriptor
        duplicated = real_dup(descriptor)
        public_descriptor = duplicated
        return duplicated

    def fail_after_closing_staging(pin: file_io.PinnedPath) -> None:
        nonlocal injected
        staging = pin.path.name.startswith(f".{video.name}{pairing.SIDECAR_SUFFIX}.")
        real_close(pin)
        if staging and not injected:
            injected = True
            raise OSError("injected staging close failure after release")

    monkeypatch.setattr(os, "dup", record_public_duplicate)
    monkeypatch.setattr(file_io.PinnedPath, "close", fail_after_closing_staging)

    with pytest.raises(stills.StillError, match="injected staging close failure"):
        stills._write_sidecar_publication(video, chosen)

    assert injected
    assert public_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(public_descriptor)


def test_existing_target_never_writes_a_sidecar_after_source_removal(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reusing a frame is still a publication because it creates a sidecar."""
    video = make_video(root / "clip.mp4")
    target = stills.destination(video, root)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\nexisting")
    real_validate = stills._require_unchanged_source
    removed = False

    def remove_then_validate(
        source: Path,
        kind: Kind,
        expected: stills._SourceSnapshot,
    ) -> None:
        nonlocal removed
        if not removed:
            removed = True
            source.unlink()
        real_validate(source, kind, expected)

    monkeypatch.setattr(stills, "_require_unchanged_source", remove_then_validate)

    with pytest.raises(stills.StillError, match="removed while its still was being made"):
        stills.generate(video, root)

    assert target.is_file()
    assert not video.with_name(video.name + pairing.SIDECAR_SUFFIX).exists()


def test_the_still_is_found_by_the_managed_directory_alone(root: Path) -> None:
    """Even with the sidecar gone, the convention still locates it."""
    video = make_video(root / "clip.mp4")
    still = stills.generate(video, root)
    video.with_name(video.name + pairing.SIDECAR_SUFFIX).unlink()
    assert pairing.find_still(video, roots=(root,)) == still


def test_a_generated_still_does_not_become_a_wallpaper_of_its_own(
    root: Path, tmp_path: Path
) -> None:
    """Otherwise the same picture turns up twice in the rotation."""
    video = make_video(tmp_path / "clip.mp4")
    still = stills.generate(video, root)
    items = (item(video, Kind.VIDEO), item(still, Kind.STILL))
    paired = pairings.apply(items, roots=(root,))
    assert [entry.path for entry in paired] == [video]
    assert paired[0].paired_still == still


def test_the_sidecar_is_replaced_in_one_step(root: Path, tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4")
    stills.generate(video, root)
    stray = [
        entry
        for entry in video.parent.iterdir()
        if entry.name.startswith(".") and entry.name.endswith(".tmp")
    ]
    assert stray == []


def test_legacy_predictable_sidecar_temporary_cannot_redirect_a_write(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    chosen = tmp_path / "chosen.png"
    chosen.write_bytes(b"image")
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    legacy = sidecar.with_name(f".{sidecar.name}.{os.getpid()}.tmp")
    legacy.symlink_to(sentinel)

    stills.write_sidecar(video, chosen)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert legacy.is_symlink()
    assert pairing.read_sidecar(video) == chosen


def test_generated_still_publication_syncs_its_directory(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = make_video(tmp_path / "clip.mp4")
    target = stills.destination(video, root)
    synced: list[tuple[int, int]] = []
    original = paths.fsync_directory

    def record(directory: Path) -> None:
        status = directory.stat()
        synced.append((status.st_dev, status.st_ino))
        original(directory)

    monkeypatch.setattr(paths, "fsync_directory", record)

    generated = stills.generate(video, root)

    target_directory = target.parent.stat()
    assert generated == target
    assert synced == [(target_directory.st_dev, target_directory.st_ino)]


def test_legacy_predictable_image_temporary_cannot_redirect_ffmpeg(
    root: Path, tmp_path: Path
) -> None:
    video = make_video(tmp_path / "clip.mp4")
    target = stills.destination(video, root)
    target.parent.mkdir(parents=True)
    sentinel = tmp_path / "sentinel.png"
    sentinel.write_bytes(b"keep")
    legacy = target.with_name(f".{target.stem}.{os.getpid()}.tmp{target.suffix}")
    legacy.symlink_to(sentinel)

    assert stills.generate(video, root) == target

    assert sentinel.read_bytes() == b"keep"
    assert legacy.is_symlink()
    assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


# -- the forgiving entry point -------------------------------------------


def test_ensure_makes_a_still_for_a_video_that_has_none(root: Path, tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4")
    assert stills.ensure(item(video, Kind.VIDEO), root) == stills.destination(video, root)


def test_ensure_leaves_an_already_paired_video_alone(root: Path, tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4")
    chosen = tmp_path / "chosen.png"
    chosen.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert stills.ensure(item(video, Kind.VIDEO, chosen), root) == chosen
    assert not pairing.still_directory(root).exists()


def test_ensure_finds_the_users_own_convention_before_making_one(
    root: Path, tmp_path: Path
) -> None:
    """`foo.mp4` next to `foo-still.png` is what the real library already does."""
    video = make_video(tmp_path / "clip.mp4")
    sibling = tmp_path / "clip-still.png"
    sibling.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert stills.ensure(item(video, Kind.VIDEO), root) == sibling
    assert not pairing.still_directory(root).exists()


def test_ensure_says_nothing_about_a_still(root: Path, tmp_path: Path) -> None:
    picture = tmp_path / "picture.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert stills.ensure(item(picture, Kind.STILL), root) is None


def test_ensure_swallows_a_failure_rather_than_stopping_playback(
    root: Path, tmp_path: Path
) -> None:
    """A still that cannot be made is not a reason to refuse to play the video."""
    impostor = tmp_path / "clip.mp4"
    impostor.write_bytes(b"not a video at all")
    assert stills.ensure(item(impostor, Kind.VIDEO), root) is None


# -- scene captures -------------------------------------------------------


def _scene(
    workshop_id: str,
    paired: Path | None = None,
    *,
    directory: Path | None = None,
) -> MediaItem:
    return MediaItem(
        path=directory or Path("/steam/workshop/content/431960") / workshop_id,
        kind=Kind.SCENE,
        size=1,
        mtime=0,
        scene=workshop_id,
        paired_still=paired,
    )


def _png_header(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def test_an_old_portrait_scene_still_is_marked_for_recapture(root: Path) -> None:
    target = pairing.still_directory(root) / "1647046763.png"
    _png_header(target, 1270, 1537)
    scene = _scene("1647046763", target)

    assert stills.scene_capture_required(scene, root, size=(2560, 1600))


def test_a_large_correct_aspect_scene_still_is_reused(root: Path) -> None:
    target = pairing.still_directory(root) / "1647046763.png"
    _png_header(target, 3840, 2400)
    scene = _scene("1647046763", target)

    assert not stills.scene_capture_required(scene, root, size=(2560, 1600))


def test_a_custom_scene_still_is_never_replaced_by_maintenance(root: Path) -> None:
    custom = root / "portrait-i-chose.png"
    _png_header(custom, 800, 1200)

    assert not stills.scene_capture_required(_scene("1647046763", custom), root)


def test_scene_capture_replaces_the_managed_still_atomically(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    target = pairing.still_directory(root) / "1647046763.png"
    _png_header(target, 1270, 1537)
    installation = root.parent / "steam" / "workshop" / "content" / "431960" / "1647046763"
    installation.mkdir(parents=True)
    scene = _scene("1647046763", target, directory=installation)
    monkeypatch.setattr(scenes, "capture_size", lambda: (2560, 1600))

    def capture(
        _scene_id: str,
        destination: Path,
        *,
        size: tuple[int, int],
        prepared_output: bool,
    ) -> Path:
        assert size == (2560, 1600)
        assert prepared_output
        _png_header(destination, 3840, 2400)
        return destination

    monkeypatch.setattr(scenes, "screenshot", capture)

    assert stills.capture_scene(scene, root) == target
    assert stills._png_size(target) == (3840, 2400)
    assert not any(path.name.endswith(".tmp.png") for path in target.parent.iterdir())


def test_scene_capture_never_overwrites_a_late_target(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    target = pairing.still_directory(root) / "1647046763.png"
    installation = root.parent / "steam" / "workshop" / "content" / "431960" / "1647046763"
    installation.mkdir(parents=True)
    scene = _scene("1647046763", directory=installation)
    monkeypatch.setattr(scenes, "capture_size", lambda: (2560, 1600))
    late = b"late non-participating scene still"

    def capture(
        _scene_id: str,
        destination: Path,
        *,
        size: tuple[int, int],
        prepared_output: bool,
    ) -> Path:
        assert size == (2560, 1600)
        assert prepared_output
        _png_header(destination, 3840, 2400)
        target.write_bytes(late)
        return destination

    monkeypatch.setattr(scenes, "screenshot", capture)

    with pytest.raises(stills.StillError):
        stills.capture_scene(scene, root)

    assert target.read_bytes() == late


def test_scene_target_directory_replacement_cannot_redirect_publication(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    target = pairing.still_directory(root) / "1647046763.png"
    installation = root.parent / "steam" / "workshop" / "content" / "431960" / "1647046763"
    installation.mkdir(parents=True)
    scene = _scene("1647046763", directory=installation)
    monkeypatch.setattr(scenes, "capture_size", lambda: (2560, 1600))
    saved_directory = target.parent.with_name("saved-scene-stills")
    sentinel = b"replacement scene target"

    def capture(
        _scene_id: str,
        destination: Path,
        *,
        size: tuple[int, int],
        prepared_output: bool,
    ) -> Path:
        assert size == (2560, 1600)
        assert prepared_output
        _png_header(destination, 3840, 2400)
        target.parent.rename(saved_directory)
        target.parent.mkdir(mode=0o755)
        target.write_bytes(sentinel)
        return destination

    monkeypatch.setattr(scenes, "screenshot", capture)

    with pytest.raises(stills.StillError):
        stills.capture_scene(scene, root)

    assert target.read_bytes() == sentinel
    assert not (saved_directory / target.name).exists()


def test_scene_source_change_after_publication_restores_the_prior_still(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    target = pairing.still_directory(root) / "1647046763.png"
    _png_header(target, 1270, 1537)
    installation = root.parent / "steam" / "workshop" / "content" / "431960" / "1647046763"
    installation.mkdir(parents=True)
    scene = _scene("1647046763", target, directory=installation)
    monkeypatch.setattr(scenes, "capture_size", lambda: (2560, 1600))

    def capture(
        _scene_id: str,
        destination: Path,
        *,
        size: tuple[int, int],
        prepared_output: bool,
    ) -> Path:
        assert size == (2560, 1600)
        assert prepared_output
        _png_header(destination, 3840, 2400)
        return destination

    real_publish = stills._publish_image

    def change_source_after_publish(
        temporary: stills._ImageTemporary,
        published_target: Path,
        target_access: Path,
        target_context: file_io.PinnedDirectoryContext,
        existing: stills._ExistingTarget | None,
    ) -> stills._ImagePublication:
        publication = real_publish(
            temporary,
            published_target,
            target_access,
            target_context,
            existing,
        )
        (installation / "late-change").write_bytes(b"changed source directory")
        return publication

    monkeypatch.setattr(scenes, "screenshot", capture)
    monkeypatch.setattr(stills, "_publish_image", change_source_after_publish)

    with pytest.raises(stills.StillError, match="changed while its still was being made"):
        stills.capture_scene(scene, root)

    assert stills._png_size(target) == (1270, 1537)


def test_scene_capture_preserves_an_in_place_target_mutation(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    target = pairing.still_directory(root) / "1647046763.png"
    _png_header(target, 1270, 1537)
    installation = root.parent / "steam" / "workshop" / "content" / "431960" / "1647046763"
    installation.mkdir(parents=True)
    scene = _scene("1647046763", target, directory=installation)
    monkeypatch.setattr(scenes, "capture_size", lambda: (2560, 1600))
    mutated = b"changed through an open writer"

    def capture(
        _scene_id: str,
        destination: Path,
        *,
        size: tuple[int, int],
        prepared_output: bool,
    ) -> Path:
        assert size == (2560, 1600)
        assert prepared_output
        _png_header(destination, 3840, 2400)
        target.write_bytes(mutated)
        return destination

    monkeypatch.setattr(scenes, "screenshot", capture)

    with pytest.raises(stills.StillError):
        stills.capture_scene(scene, root)

    assert target.read_bytes() == mutated


# -- pausing a video that has no still ------------------------------------
#
# The whole reason this module exists. Before it, turning dynamics off while an
# unpaired video played left the session with nothing to show: the applier
# refused, the playlist jumped to an unrelated wallpaper, and Noctalia's palette
# went on being derived from whatever still was set last.


class FakeRenderer:
    def __init__(self) -> None:
        self.started: list[Path] = []
        self.stops = 0

    def start(self, video: Path) -> None:
        self.started.append(video)

    def stop(self) -> None:
        self.stops += 1


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every wallpaper the session hands to Noctalia, in order."""
    calls: list[Path] = []
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.set_wallpaper",
        lambda path, connector=None: calls.append(Path(path)),
    )
    monkeypatch.setattr(
        "wall_in_one.theme.noctalia.current_wallpaper",
        lambda: (_ for _ in ()).throw(noctalia.NoctaliaUnavailableError("no shell")),
    )
    # See `conftest`: applying settles the palette too, and the live calls are
    # refused, so a test that applies has to stand in for all of them.
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_scheme", lambda _selection: None)
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_mode", lambda _mode: None)
    return calls


def build_session(root: Path) -> Session:
    """A session over a real directory, scanned for real, with no compositor."""
    return Session(
        config.Settings().validated(),
        applier=Applier(FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda _roots: scan.scan((root,)),
        rng=random.Random(11),
    )


def test_pausing_an_unpaired_video_takes_a_still_from_it(applied: list[Path], root: Path) -> None:
    video = make_video(root / "lonely.mp4")
    session = build_session(root)
    session.refresh()
    session.apply_current()
    assert session.current is not None and session.current.animated

    session.update_settings(replace(session.settings, dynamics_enabled=False))

    still = stills.destination(video, root)
    assert still.is_file()
    assert applied[-1] == still


def test_the_video_stays_the_current_wallpaper_across_the_pause(
    applied: list[Path], root: Path
) -> None:
    """It must not jump to an unrelated wallpaper; that was the bug."""
    make_video(root / "lonely.mp4")
    other = root / "unrelated.png"
    other.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = build_session(root)
    session.refresh()
    session.select(root / "lonely.mp4")

    session.update_settings(replace(session.settings, dynamics_enabled=False))

    assert session.current is not None
    assert session.current.item.path == root / "lonely.mp4"
    assert not session.current.animated
    assert other not in applied


def test_the_generated_still_survives_dynamics_going_back_on(
    applied: list[Path], root: Path
) -> None:
    """Made once, then found by the scan from then on -- no second encode."""
    video = make_video(root / "lonely.mp4")
    session = build_session(root)
    session.refresh()
    session.apply_current()
    session.update_settings(replace(session.settings, dynamics_enabled=False))
    stamp = stills.destination(video, root).stat().st_mtime_ns

    session.update_settings(replace(session.settings, dynamics_enabled=True))
    session.update_settings(replace(session.settings, dynamics_enabled=False))

    assert stills.destination(video, root).stat().st_mtime_ns == stamp


def test_a_video_that_ffmpeg_cannot_read_still_falls_back(applied: list[Path], root: Path) -> None:
    """A still that cannot be made is not a reason to leave a dead screen."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "broken.mp4").write_bytes(b"not a video at all")
    fallback = root / "fallback.png"
    fallback.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = build_session(root)
    session.refresh()
    session.select(root / "broken.mp4")

    session.update_settings(replace(session.settings, dynamics_enabled=False))

    assert applied[-1] == fallback


# -- somebody else's directory --------------------------------------------


def test_no_sidecar_is_written_beside_a_video_outside_the_library(
    root: Path, tmp_path: Path
) -> None:
    """A Wallpaper Engine wallpaper lives in Steam's Workshop tree. Writing
    into it is not ours to do: Steam may replace the directory wholesale, and
    a foreign file in there is litter in somebody else's collection.

    Caught live, after a real run left a sidecar in the developer's Steam
    directory.
    """
    elsewhere = tmp_path / "steam" / "workshop"
    video = make_video(elsewhere / "clip.mp4")

    still = stills.generate(video, root)

    assert still.is_file(), "the still itself still gets made"
    assert list(elsewhere.iterdir()) == [video], "and nothing else appears beside the video"


def test_a_still_for_an_outside_video_is_still_found_afterwards(root: Path, tmp_path: Path) -> None:
    """Which is why skipping the sidecar costs nothing: the still lands in the
    managed directory and the convention finds it there by name."""
    video = make_video(tmp_path / "steam" / "clip.mp4")
    still = stills.generate(video, root)
    assert pairing.find_still(video, roots=(root,)) == still


def test_a_still_is_taken_at_the_wallpapers_own_resolution(root: Path) -> None:
    """Not the preview's. Wallpaper Engine ships previews between 192x192 and
    1080x1080, so taking the still from one would put a thumbnail on a 4K
    screen and hand Noctalia a thumbnail to derive 72 colour tokens from."""
    video = make_video(root / "clip.mp4")
    (root / "preview.gif").write_bytes(b"GIF89a")

    still = stills.generate(video, root)

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(still),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip().rstrip(",") == "320,180"
