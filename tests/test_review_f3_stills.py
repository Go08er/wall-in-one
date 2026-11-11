"""The f3 review of 0.4.2's stills round (S3-n): sidecars in Move to Trash, formats, stdin."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.test_manage import _trash, item_for
from tests.test_stills import make_video
from wall_in_one import decoding, thumbnails
from wall_in_one.library import pairing, workshop
from wall_in_one.library.model import Kind

# -- S3-5: Move to Trash leaves every sidecar the readers ignore ---------------------------------

KEPT = {
    "not-json": lambda root: b"this is not json",
    "empty": lambda root: b"",
    "no-still-path": lambda root: b'{"other": 1}',
    "still-path-a-number": lambda root: b'{"still_path": 7}',
    "oversize": lambda root: b" " * (pairing.MAX_SIDECAR_BYTES + 1),
    "outside-every-root": lambda root: json.dumps({"still_path": "/nowhere"}).encode(),
    "dotdot-escape": lambda root: b'{"still_path": "../outside.png"}',
}
TAKEN = {
    "absolute-inside": lambda root: json.dumps(
        {"still_path": str(root / "clip-still.png")}
    ).encode(),
    "relative-inside": lambda root: b'{"still_path": "clip-still.png"}',
}


@pytest.mark.parametrize("shape", [*KEPT, *TAKEN])
def test_move_to_trash_takes_only_a_sidecar_this_app_could_have_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private-data"))
    root = tmp_path / "library"
    root.mkdir()
    (tmp_path / "outside.png").write_bytes(b"x")
    (root / "clip-still.png").write_bytes(b"x")
    video = root / "clip.mp4"
    video.write_bytes(b"video" * 4)
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    make: Callable[[Path], bytes] = {**KEPT, **TAKEN}[shape]
    contents = make(root)
    sidecar.write_bytes(contents)

    _trash(item_for(video, Kind.VIDEO), (root,))

    assert not video.exists()
    if shape in KEPT:
        assert sidecar.read_bytes() == contents, "an ignored sidecar is left byte for byte"
    else:
        assert not sidecar.exists(), "the app's own selector is ordinary cleanup"
    assert (root / "clip-still.png").is_file()


# -- S3-4: the format allowlist ------------------------------------------------------------------


def _animated_webp(path: Path) -> bool:
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=64x64:d=1:r=5", str(path)],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0 and path.is_file()


def test_animated_webp_is_decoded_through_the_hardened_options(tmp_path: Path) -> None:
    animated = tmp_path / "anim.webp"
    if not _animated_webp(animated):
        pytest.skip("this ffmpeg cannot make an animated WebP")
    from wall_in_one.library.model import MediaItem

    thumb = tmp_path / "thumb.png"
    item = MediaItem(path=animated, kind=Kind.STILL, size=1, mtime=0)
    done = subprocess.run(thumbnails._command(item, thumb), capture_output=True, check=False)
    assert done.returncode == 0 and thumb.is_file(), done.stderr.decode()[-200:]
    # and over stdin, as a downloaded preview arrives
    assert thumbnails.to_displayable(animated.read_bytes()) != b""


def test_the_allowlist_names_the_formats_ffmpeg_reports_for_real_files(tmp_path: Path) -> None:
    """Each real file's demuxer is on the list, so none is refused for its container."""
    clip = make_video(tmp_path / "clip.mp4")
    names = set(decoding.FORMATS.split(","))
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name",
            "-of",
            "csv=p=0",
            str(clip),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("no ffprobe")
    assert set(probe.stdout.strip().split(",")) & names


# -- S3-N1: the stdin decode carries the options -------------------------------------------------


def test_the_download_decode_over_stdin_carries_the_hardening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 1, b"", b"")

    monkeypatch.setattr(thumbnails, "is_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", run)
    assert thumbnails.to_displayable(b"not natively decodable") == b""
    (command,) = seen
    before_input = command[: command.index("-i")]
    assert before_input[-4:] == decoding.input_options(pipe=True)
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"


# -- S3-N2: read_sidecar's own Workshop guard ----------------------------------------------------


def test_read_sidecar_itself_refuses_a_trusted_looking_sidecar_beside_workshop_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    folder = root / "Games/steam/steamapps/workshop/content/431960/9"
    folder.mkdir(parents=True)
    picture = root / "mine.png"
    picture.write_bytes(b"x")
    video = folder / "clip.mp4"
    video.write_bytes(b"x")
    video.with_name(video.name + pairing.SIDECAR_SUFFIX).write_text(
        json.dumps({"still_path": str(picture)}), encoding="utf-8"
    )
    assert workshop.in_content_tree(video)
    assert pairing.read_sidecar(video, (root,)) is None
    # The control: the same sidecar beside a video of the person's own is read.
    own = root / "own.mp4"
    own.write_bytes(b"x")
    own.with_name(own.name + pairing.SIDECAR_SUFFIX).write_text(
        json.dumps({"still_path": str(picture)}), encoding="utf-8"
    )
    assert pairing.read_sidecar(own, (root,)) == picture
