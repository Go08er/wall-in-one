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
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.test_scenes import _QuietRenderer
from tests.web_previews import Fixture
from wall_in_one import thumbnails
from wall_in_one.library import scan, stills
from wall_in_one.library.model import MediaItem
from wall_in_one.wallpaper import scenes
from wall_in_one.wallpaper.applier import Applier, ApplyError


def _item(fixture: Fixture, state: str, monkeypatch: pytest.MonkeyPatch) -> MediaItem:
    old = fixture.scene
    assert not old.holds_web and not old.still_from_preview
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
