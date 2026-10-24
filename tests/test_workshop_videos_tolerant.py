"""Workshop videos keep 38bc5ea's tolerant reading: the engine's strict rules aren't theirs.

mpvpaper plays a Workshop `video` by its file; linux-wallpaperengine is never
asked. So the type rule (ASCII lower-casing, nothing stripped), the file rule
and the ASCII-id folder rule, which protect what is handed to the engine, don't
apply to a video. A video that played then is still listed. Its pairing is
keyed by its file's path, so a folder not named by an id needs nothing more.
"""

from __future__ import annotations

import json
from pathlib import Path

from wall_in_one.library import pairings, scan, workshop
from wall_in_one.library.model import Kind, MediaItem, UnseenKind


def _install(content: Path, folder: str, project: dict[str, object], *files: str) -> Path:
    directory = content / folder
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(json.dumps(project), encoding="utf-8")
    for name in files:
        (directory / name).write_bytes(b"x")
    return directory


def _listed(tmp_path: Path) -> tuple[dict[str, MediaItem], list[tuple[str, UnseenKind]]]:
    said: list[tuple[str, UnseenKind]] = []
    items = scan.workshop_items(
        (tmp_path,), on_unseen=lambda place, kind, _why: said.append((place.name, kind))
    )
    return {item.path.parent.name: item for item in items}, said


def test_a_video_whose_type_is_padded_is_listed_and_plays_as_a_video(tmp_path: Path) -> None:
    """Before: ``" video"`` was a type the engine refuses, so it was listed as unplayable."""
    content = tmp_path / "steamapps" / "workshop" / "content" / workshop.APP_ID
    _install(content, "106", {"type": " video", "title": "Padded", "file": "v.mp4"}, "v.mp4")
    _install(content, "107", {"type": "VIDEO ", "title": "Padded too", "file": " v.mp4"}, "v.mp4")

    items, _said = _listed(tmp_path)

    for folder in ("106", "107"):
        assert items[folder].kind is Kind.VIDEO, folder
        assert items[folder].unplayable is None and items[folder].path.name == "v.mp4"


def test_a_padded_type_is_still_not_handed_to_the_engine(tmp_path: Path) -> None:
    content = tmp_path / "steamapps" / "workshop" / "content" / workshop.APP_ID
    _install(content, "103", {"type": " scene", "title": "S", "file": "scene.json"})

    (item,) = scan.workshop_items((tmp_path,))

    assert item.kind is Kind.SCENE and item.unplayable is not None


def test_a_video_in_a_folder_not_named_by_an_id_is_listed_with_a_stable_identity(
    tmp_path: Path,
) -> None:
    """Before: every folder not named by an ASCII Workshop id was dropped."""
    content = tmp_path / "steamapps" / "workshop" / "content" / workshop.APP_ID
    _install(content, "my-clip", {"type": "video", "title": "Clip", "file": "c.mp4"}, "c.mp4")
    _install(content, "my-scene", {"type": "scene", "title": "S", "file": "scene.json"})
    _install(content, "my-web", {"type": "web", "title": "W", "file": "index.html"}, "index.html")

    items, said = _listed(tmp_path)

    clip = items["my-clip"]
    assert clip.kind is Kind.VIDEO and clip.unplayable is None
    assert set(items) == {"my-clip"}, "only a video; the engine is never handed such a folder"
    assert sorted(said) == [
        ("my-scene", UnseenKind.NOT_A_WALLPAPER),
        ("my-web", UnseenKind.NOT_A_WALLPAPER),
    ]
    # Keyed by the file's path, as every video is: it doesn't need a Workshop id.
    assert pairings.Identity.of(clip).key == f"video:{clip.path}"
    again, _ = _listed(tmp_path)
    assert pairings.Identity.of(again["my-clip"]) == pairings.Identity.of(clip)


def test_a_video_whose_file_is_gone_is_still_not_listed(tmp_path: Path) -> None:
    content = tmp_path / "steamapps" / "workshop" / "content" / workshop.APP_ID
    _install(content, "108", {"type": " video", "title": "Gone", "file": "gone.mp4"})
    _install(content, "gone-clip", {"type": "video", "title": "Gone", "file": "gone.mp4"})

    items, _said = _listed(tmp_path)

    assert items == {}
