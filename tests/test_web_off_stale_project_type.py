"""A scanned scene that Steam has since made a web page gets no still or thumbnail while web is off.

Review 0.4.1 (R41-1): the scan's word (`MediaItem.holds_web`) is as old as the
scan, and an update can rewrite a scene's ``project.json`` to ``web`` without
a rescan. So the engine and ffmpeg are asked only after the type is read
again, where each of them would look: the engine under ``$HOME``, ffmpeg in
the item's own folder. The legacy Python applier, which only ``--service``'s
fallback drives, starts the engine the same way and is held to the same.

No engine or decoder is started. The private Workshop fixture is scanned as
a scene, and only ``project.json`` is changed before the request; the
screenshot and ffmpeg boundaries are replaced with recorders that raise.

The second half is the R14 review's probes: what the fresh read takes in
besides an item's own type. A preset's base (L1), a Workshop video's still
beside it (P2), and the still maker asking again for a scene it was refused
once the rescan says web (N1).
"""

from __future__ import annotations

import json
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REAL_HOME
from tests.test_scenes import _QuietRenderer
from tests.web_previews import Fixture
from wall_in_one import thumbnails
from wall_in_one.library import scan, stills, workshop
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.wallpaper import scenes
from wall_in_one.wallpaper.applier import Applier, ApplyError


def _item(fixture: Fixture, state: str, monkeypatch: pytest.MonkeyPatch) -> MediaItem:
    old = fixture.scene
    assert not old.holds_web and not old.no_automatic_still
    # The screenshot command names a Workshop id, not item.path. The fixture
    # is put at the engine's first usual place under HOME, so this is also
    # the folder an actual engine would find.
    home = fixture.root.parent / "engine-home"
    (home / ".local" / "share").mkdir(parents=True)
    (home / ".local" / "share" / "Steam").symlink_to(old.path.parents[4], target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    if state != "scene-control":
        (old.path / "project.json").write_text(
            json.dumps(
                {"type": "web", "title": "Updated web", "file": "index.html", "preview": "p.gif"}
            ),
            encoding="utf-8",
        )
    if state == "fresh-web-control":
        steam = old.path.parents[4]
        library = scan.scan(
            [fixture.root], include_workshop=True, workshop_roots=(steam,), records={}
        )
        fresh = next(item for item in library.items if item.scene == old.scene)
        assert fresh.holds_web
        return fresh
    return old


@pytest.mark.parametrize("state", ["scene-control", "stale-web", "fresh-web-control"])
def test_capture_reads_the_type_again_before_the_engine_is_asked(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    item = _item(web_folder, state, monkeypatch)
    dispatched: list[str] = []

    def screenshot(scene: str, *_args: object, **_kwargs: object) -> None:
        dispatched.append(scene)
        raise scenes.SceneError("intercepted: the engine was not started")

    monkeypatch.setattr(scenes, "screenshot", screenshot)
    with pytest.raises(stills.StillError):
        stills.capture_scene(item, web_folder.root, force=True, size=(64, 40), allow_web=False)
    if state == "scene-control":
        assert dispatched == [item.scene]
    else:
        assert not dispatched, "web-off capture dispatched the engine on the scan's old type"


@pytest.mark.parametrize("state", ["scene-control", "stale-web", "fresh-web-control"])
def test_a_thumbnail_reads_the_type_again_before_ffmpeg_is_asked(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    item = _item(web_folder, state, monkeypatch)
    dispatched: list[list[str]] = []

    def decode(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        dispatched.append(argv)
        raise OSError("intercepted: the decoder was not started")

    monkeypatch.setattr(subprocess, "run", decode)
    with pytest.raises(thumbnails.ThumbnailError):
        thumbnails.generate(item, force=True, allow_web=False)
    if state == "scene-control":
        assert len(dispatched) == 1
    else:
        assert not dispatched, "web-off thumbnail dispatched ffmpeg on the scan's old type"


@pytest.mark.parametrize("state", ["scene-control", "stale-web"])
def test_a_legacy_scene_start_reads_the_type_again_before_the_engine_is_started(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    item = _item(web_folder, state, monkeypatch)
    monkeypatch.setattr(scenes, "running_elsewhere", lambda _output: ())
    started: list[str] = []

    class Engine:
        def start(self, scene: str, properties: object = ()) -> None:
            started.append(scene)

        def stop(self) -> None: ...

    applier = Applier(_QuietRenderer(), scene_renderer=Engine(), own_scene_renderer=True)  # type: ignore[arg-type]
    if state == "scene-control":
        applier.apply(item, dynamics_enabled=True)
        assert started == [item.scene]
    else:
        with pytest.raises(ApplyError):
            applier.apply(item, dynamics_enabled=True)
        assert not started, "web-off scene start dispatched the engine on the scan's old type"


# -- R14: what else the fresh read takes in ------------------------------------------


def _png(width: int, height: int) -> bytes:
    """A real PNG: whatever reads its header reads one."""
    raw = b"".join(b"\0" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class _Tree:
    """A private Steam tree: a scene (200), a video with a still beside it (210),
    and a scene (230) with a preset of it (220). A second Steam library is
    scanned too, empty unless a test moves an item into it."""

    def __init__(self, tmp_path: Path) -> None:
        self.steam = tmp_path / "steam"
        self.other = tmp_path / "other-steam"
        self.contents = tuple(
            steam / "steamapps" / "workshop" / "content" / workshop.APP_ID
            for steam in (self.steam, self.other)
        )
        self.content = self.contents[0]
        self.root = tmp_path / "library"
        (self.root / "Wall-in-One" / "Automatic Stills").mkdir(parents=True)
        scene = {"type": "scene", "file": "scene.json"}
        self.put("200", {**scene, "title": "S", "preview": "p.png"})
        self.put("210", {"type": "video", "title": "V", "file": "clip.mp4", "preview": "p.png"})
        (self.content / "210" / "clip.png").write_bytes(_png(8, 8))  # the video's still
        self.put("230", {**scene, "title": "B", "preview": "bp.png"})
        self.put(
            "220", {"title": "P", "dependency": "230", "preset": {"a": 1}, "preview": "pp.png"}
        )

    def put(self, wid: str, project: dict[str, Any]) -> None:
        folder = self.content / wid
        folder.mkdir(parents=True)
        (folder / "project.json").write_text(json.dumps(project))
        for name in ("scene.json", "scene.pkg", "clip.mp4", "index.html"):
            (folder / name).write_bytes(b"x")
        for name in ("p.png", "preview.jpg", "bp.png", "pp.png"):
            (folder / name).write_bytes(_png(8, 8))

    def folder(self, wid: str) -> Path:
        return next(content / wid for content in self.contents if (content / wid).is_dir())

    def elsewhere(self, wid: str) -> None:
        """Move ``wid`` into the second Steam library."""
        self.contents[1].mkdir(parents=True, exist_ok=True)
        self.folder(wid).rename(self.contents[1] / wid)

    def make_web(self, wid: str) -> None:
        """What a Steam update can do to a scene's or a video's folder."""
        (self.folder(wid) / "project.json").write_text(
            json.dumps({"type": "web", "title": "W", "file": "index.html", "preview": "p.png"})
        )

    def scan(self) -> dict[str, MediaItem]:
        library = scan.scan(
            [self.root],
            include_workshop=True,
            workshop_roots=(self.steam, self.other),
            records={},
        )
        return {(item.scene or item.path.parent.name): item for item in library.items}

    def link_home(self) -> None:
        """Where the engine looks under HOME: Steam's usual home, linked to the tree."""
        if Path.home().resolve() == REAL_HOME.resolve():
            pytest.fail(f"refusing to link a Steam folder under the real {REAL_HOME}")
        share = Path.home() / ".local" / "share"
        share.mkdir(parents=True, exist_ok=True)
        (share / "Steam").symlink_to(self.steam, target_is_directory=True)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Tree:
    monkeypatch.setattr(thumbnails, "cache_directory", lambda: tmp_path / "cache")
    monkeypatch.setattr(thumbnails, "is_available", lambda: True)
    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(scenes, "is_available", lambda: True)
    return _Tree(tmp_path)


def _decoder(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every ffmpeg command line asked for, each refused."""
    asked: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        asked.append([str(part) for part in argv])
        raise OSError("intercepted: the decoder was not started")

    monkeypatch.setattr(subprocess, "run", run)
    return asked


@pytest.mark.parametrize("where", ["beside", "another-library"])
@pytest.mark.parametrize("state", ["scene-control", "stale", "fresh-control"])
def test_a_preset_whose_base_steam_made_web_gets_no_ffmpeg_while_web_is_off(
    tree: _Tree, monkeypatch: pytest.MonkeyPatch, where: str, state: str
) -> None:
    """R14 L1: a fresh scan calls a preset of a web base web (`workshop.holds_web`).

    So the read before ffmpeg reads the base too, where the scan found it: in
    another Steam library than the preset's, one that is still a scene is no
    reason to refuse.
    """
    if where == "another-library":
        tree.elsewhere("230")
    preset = tree.scan()["220"]
    assert not preset.holds_web
    if state != "scene-control":
        tree.make_web("230")
    if state == "fresh-control":
        preset = tree.scan()["220"]
        assert preset.holds_web
    asked = _decoder(monkeypatch)
    with pytest.raises((thumbnails.ThumbnailError, stills.StillError)):
        thumbnails.generate(preset, force=True)
    with pytest.raises(stills.StillError):
        stills.capture_scene(preset, tree.root, force=True)
    if state == "scene-control":
        assert asked, "a preset of a scene got no thumbnail or still"
    else:
        assert not asked, "web-off ffmpeg was asked for a preset of a now-web base"


def _video(tree: _Tree, state: str) -> MediaItem:
    """The Workshop video 210, with the author's ``clip.png`` beside it, which is not its still."""
    item = tree.scan()["210"]
    assert item.kind is Kind.VIDEO and item.paired_still is None
    assert (tree.content / "210" / "clip.png").is_file()
    if state == "stale-web":
        tree.make_web("210")
    return item


@pytest.mark.parametrize("state", ["video-control", "stale-web"])
def test_a_workshop_videos_still_is_made_from_the_video_and_not_from_a_file_beside_it(
    tree: _Tree, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """The scan's pairing is as old as the scan, and a picture beside the video is the author's.

    With web off and the folder now a web page's, nothing is opened at all.
    """
    item = _video(tree, state)
    asked = _decoder(monkeypatch)
    still = stills.ensure(item, tree.root)
    assert still is None, "the author's clip.png was used, or a still came from nowhere"
    if state == "video-control":
        assert asked, "ffmpeg is asked for a frame of the video itself"
        assert not any("clip.png" in part for command in asked for part in command)
    else:
        assert not asked


@pytest.mark.parametrize("state", ["scene-control", "stale-web"])
def test_with_web_on_a_scene_refused_as_web_is_asked_for_again_after_the_rescan(
    tree: _Tree, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """R14 N1: the still maker tells only a scan-known web item that web is on.

    So a scene Steam has made a web page is refused, whatever the setting; the
    rescan's web item must not then be kept out as already attempted. The
    control is a scene the engine fails on, which is not asked for again.
    """
    from wall_in_one.ui.stills import StillMaker

    tree.link_home()
    stale = tree.scan()["200"]
    if state == "stale-web":
        tree.make_web("200")
    engine: list[str] = []

    def screenshot(scene: str, *_args: object, **_kwargs: object) -> None:
        engine.append(scene)
        raise scenes.SceneError("intercepted: the engine was not started")

    monkeypatch.setattr(scenes, "screenshot", screenshot)
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_a: (64, 40))
    maker = StillMaker(web_allowed=lambda: True)
    submitted: list[tuple[MediaItem, ...]] = []
    monkeypatch.setattr(
        maker, "_submit", lambda _work, items, *_rest, context: submitted.append(items)
    )
    monkeypatch.setattr(maker, "_local_displays", lambda: ())
    maker.request([stale], tree.root, lambda made: None)
    assert submitted == [(stale,)]
    maker._run((stale,), tree.root, lambda made: None)
    rescanned = tree.scan()["200"]
    maker.request([rescanned], tree.root, lambda made: None)
    maker.shutdown()
    if state == "stale-web":
        assert engine == []
        assert rescanned.holds_web and rescanned.no_automatic_still
        # No still is ever made for a web page (the owner's rule), so there is
        # nothing to ask for again after the rescan.
        assert submitted == [(stale,)]
    else:
        assert engine == ["200"]
        assert submitted == [(stale,)]
