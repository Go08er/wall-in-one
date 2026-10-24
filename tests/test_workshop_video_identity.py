"""A Workshop video keeps the identity 0.4.0 gave it, whatever else its project.json holds.

0.4.0 (``d159a20``) listed an item that says ``video`` and has its file as
``video:<path>``, even with a ``preset`` object and a ``dependency`` beside
it. Its pairings, favourites, playlists, stills and colours are keyed by that.
A real preset has no ``type`` at all, so a video-typed item is not one.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from wall_in_one.library import pairings, scan

LEGACY = "d159a20"
PRODUCER = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from wall_in_one.library import pairings, scan
items = scan.workshop_items(content_directories=[Path(sys.argv[2])])
print(json.dumps(sorted(pairings.Identity.of(item).key for item in items)))
"""

ITEMS = {
    "3000000106": ({"type": " Video ", "title": "Padded", "file": "clip.mp4"}, "clip.mp4"),
    "my-videos": ({"type": "video", "title": "Hand-made", "file": "clip.mp4"}, "clip.mp4"),
    "3000000107": (
        {
            "type": "video",
            "title": "With a preset object",
            "file": "clip.mp4",
            "preset": {"x": 1},
            "dependency": "3000000106",
        },
        "clip.mp4",
    ),
    "3000000111": (
        {
            "type": " Video ",
            "title": "Padded, with a preset object",
            "file": "clip.mp4",
            "preset": {"x": 1},
            "dependency": "3000000106",
        },
        "clip.mp4",
    ),
}


@pytest.fixture(scope="module")
def legacy_src(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    repository = Path(__file__).resolve().parents[1]
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available here")
    known = subprocess.run(
        [git, "-C", str(repository), "cat-file", "-e", f"{LEGACY}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    if known.returncode != 0:
        pytest.skip(f"this copy has no {LEGACY} to run")
    destination = tmp_path_factory.mktemp(f"src-{LEGACY}")
    archive = subprocess.run(
        [git, "-C", str(repository), "archive", "--format=tar", LEGACY, "src"],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as packed:
        packed.extractall(destination, filter="data")
    yield destination / "src"


def test_a_video_with_a_preset_object_and_a_dependency_keeps_0_4_0s_identity(
    tmp_path: Path, legacy_src: Path
) -> None:
    content = tmp_path / "content"
    for folder, (project, media) in ITEMS.items():
        directory = content / folder
        directory.mkdir(parents=True)
        (directory / "project.json").write_text(json.dumps(project))
        (directory / media).write_bytes(b"media")

    produced = subprocess.run(
        [sys.executable, "-I", "-c", PRODUCER, str(legacy_src), str(content)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    before = set(json.loads(produced))
    assert any(key.startswith("video:") for key in before) and len(before) == len(ITEMS)

    now = {
        pairings.Identity.of(item).key
        for item in scan.workshop_items(content_directories=[content])
    }

    assert before <= now, f"records keyed by {sorted(before - now)} are orphaned"
