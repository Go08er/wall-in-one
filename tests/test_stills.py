"""Making a still for a video, and pairing the two.

The generator shells out to ffmpeg, so these tests build real one-second clips
with it rather than stubbing it out: the failures worth catching here -- a seek
past the end of a short loop, a torn file left by a crash, a still the read
half cannot then find -- all live in the part a stub would replace.

Nothing here touches the user's library. Every path is under `tmp_path`.
"""

from __future__ import annotations

import random
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config
from wall_in_one.library import pairing, pairings, scan, stills
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.session import Session
from wall_in_one.theme import noctalia
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
    assert still == root / "Wall-in-One" / "Automatic Stills" / "clip.png"
    assert still.stat().st_size > 0


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
    assert not directory.exists() or list(directory.iterdir()) == []


# -- pairing the two -----------------------------------------------------


def test_generating_writes_a_sidecar_the_reader_understands(root: Path, tmp_path: Path) -> None:
    """The end-to-end claim: what the write half produces, the read half finds."""
    video = make_video(tmp_path / "clip.mp4")
    still = stills.generate(video, root)
    assert pairing.read_sidecar(video) == still


def test_the_still_is_found_by_the_managed_directory_alone(root: Path, tmp_path: Path) -> None:
    """Even with the sidecar gone, the convention still locates it."""
    video = make_video(tmp_path / "clip.mp4")
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
