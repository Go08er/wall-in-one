"""Workshop content is untrusted: the root walk, and every decode of a file somebody wrote.

F1 of the 0.4.2 review: a library root that contains Steam's Workshop tree listed a
web wallpaper's `preview.gif` as local media (a video to the classifier), then
thumbnailed it and drew a still from it with ffmpeg, with web wallpapers off.
`workshop.folder_gate` decides, once per folder, by the place test every other
gate uses; and every ffmpeg that opens such a file is told what it may open
(`decoding.input_options`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_stills import make_video
from wall_in_one import decoding, thumbnails
from wall_in_one.library import scan, stills, workshop
from wall_in_one.library.model import Kind, MediaItem


def _listed(root: Path) -> set[str]:
    return {str(item.path.relative_to(root)) for item in scan.scan([root], records={}).items}


def _folder(root: Path, name: str, project: object | None, files: tuple[str, ...]) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    if project is not None:
        (folder / "project.json").write_text(
            project if isinstance(project, str) else json.dumps(project), encoding="utf-8"
        )
    for file in files:
        (folder / file).write_bytes(b"x")
    return folder


# -- the root walk ---------------------------------------------------------------------------


def test_steams_tree_inside_a_root_lists_no_web_wallpaper_file_as_local_media(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    item = "Games/steam/steamapps/workshop/content/431960/111"
    _folder(
        root,
        item,
        {"type": "web", "title": "W", "file": "index.html", "preview": "preview.gif"},
        ("index.html", "preview.gif", "extra.mp4", "preview.png"),
    )
    (root / "mine.png").write_bytes(b"x")
    assert _listed(root) == {"mine.png"}


def test_steams_tree_is_not_local_media_whatever_it_holds(tmp_path: Path) -> None:
    """Even a video item with no project.json at all: the Workshop scan lists the ones it lists."""
    root = tmp_path / "library"
    _folder(root, "steam/steamapps/workshop/content/431960/9", None, ("clip.mp4",))
    assert _listed(root) == set()


@pytest.mark.parametrize(
    "project",
    [
        {"type": "web", "title": "W", "file": "index.html", "preview": "preview.gif"},
        {"type": "application", "title": "A", "file": "run.exe", "preview": "preview.gif"},
        {"type": "mystery", "title": "M", "file": "clip.mp4", "preview": "preview.gif"},
        {"title": "no type at all", "file": "clip.mp4"},
        "{ not json",
        "[]",
    ],
)
def test_a_copied_in_item_folder_that_is_not_a_video_lists_nothing(
    tmp_path: Path, project: object
) -> None:
    root = tmp_path / "library"
    _folder(root, "copied", project, ("index.html", "preview.gif", "clip.mp4", "run.exe"))
    assert _listed(root) == set()


def test_a_copied_in_video_item_lists_its_declared_video_and_nothing_else(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _folder(
        root,
        "copied",
        {"type": "video", "title": "V", "file": "clip.mp4", "preview": "preview.gif"},
        ("clip.mp4", "preview.gif", "other.mp4", "preview.png"),
    )
    assert _listed(root) == {"copied/clip.mp4"}


def test_a_folder_without_a_project_json_is_listed_as_before(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _folder(root, "plain", None, ("a.mp4", "b.gif", "c.png"))
    assert _listed(root) == {"plain/a.mp4", "plain/b.gif", "plain/c.png"}


def test_the_gate_is_the_place_test_every_other_gate_uses(tmp_path: Path) -> None:
    inside = tmp_path / "x/steamapps/workshop/content/431960/1"
    inside.mkdir(parents=True)
    other = tmp_path / "x/steamapps/workshop/content/999/1"
    other.mkdir(parents=True)
    assert workshop.folder_gate(inside) == frozenset()
    assert workshop.folder_gate(other) is None
    assert workshop.in_content_tree(inside / "preview.gif")
    assert workshop.is_workshop_content(inside / "preview.gif", labelled=False)


# -- ffmpeg: a playlist disguised as a picture or a video ---------------------------------------


def _disguised_concat(directory: Path, name: str) -> tuple[Path, Path]:
    """A script ffmpeg would follow to another local file, named like a picture or a video."""
    other = make_video(directory / "other.mp4")
    evil = directory / name
    evil.write_text("ffconcat version 1.0\nfile other.mp4\n", encoding="utf-8")
    return evil, other


def _frames(command: list[str], cwd: Path) -> int:
    done = subprocess.run(command, cwd=cwd, capture_output=True, check=False)
    return done.returncode


@pytest.mark.parametrize("name", ["preview.gif", "clip.mp4"])
def test_a_disguised_playlist_is_not_followed_by_the_still_maker_or_the_thumbnailer(
    tmp_path: Path, name: str
) -> None:
    evil, _other = _disguised_concat(tmp_path, name)
    # The premise: plain ffmpeg reads the other local file through it.
    plain = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(evil),
        "-frames:v",
        "1",
        str(tmp_path / "p.png"),
    ]
    assert _frames(plain, tmp_path) == 0, "without the options the playlist is followed"

    target = tmp_path / "still.png"
    assert _frames(stills._command(evil, target, 0.0), tmp_path) != 0
    assert not target.exists()

    item = MediaItem(path=evil, kind=Kind.VIDEO, size=1, mtime=0)
    destination = tmp_path / "thumb.png"
    assert _frames(thumbnails._command(item, destination), tmp_path) != 0
    assert not destination.exists()


def test_the_hardened_options_still_decode_a_real_video_and_gif(tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4")
    gif = tmp_path / "a.gif"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=64x64:d=1:r=5", str(gif)],
        check=True,
    )
    for source in (video, gif):
        target = tmp_path / f"{source.name}.png"
        assert _frames(stills._command(source, target, 0.0), tmp_path) == 0, source
        assert target.is_file()
        thumb = tmp_path / f"{source.name}.thumb.png"
        item = MediaItem(path=source, kind=Kind.VIDEO, size=1, mtime=0)
        assert _frames(thumbnails._command(item, thumb), tmp_path) == 0, source


def test_a_downloaded_playlist_handed_over_stdin_is_not_decoded(tmp_path: Path) -> None:
    _disguised_concat(tmp_path, "unused.gif")
    data = b"ffconcat version 1.0\nfile /etc/hostname\n"
    assert thumbnails.to_displayable(data) == b""


def test_every_ffmpeg_command_in_the_app_carries_the_options() -> None:
    options = decoding.input_options()
    assert options[:2] == ["-protocol_whitelist", "file"] and options[2] == "-format_whitelist"
    assert decoding.input_options(pipe=True)[1] == "file,pipe"
    video = Path("/w/clip.mp4")
    for command in (
        stills._command(video, Path("/o.png"), 1.0),
        stills._preview_command(video, Path("/o.png"), "gif"),
        thumbnails._command(
            MediaItem(path=video, kind=Kind.VIDEO, size=1, mtime=0), Path("/o.png")
        ),
    ):
        before_input = command[: command.index("-i")]
        assert "-protocol_whitelist" in before_input and "-format_whitelist" in before_input
