"""The app's own engine starts read an item's type every time, web wallpapers on or off.

Review f1-sonnet-b3 (B1): the service reads a Workshop item's ``project.json``
before every engine start, but the still maker and the legacy applier skipped
the read while web was on. An ``application`` item must never be handed to
linux-wallpaperengine, by any path. The same rules as the service's
(service/src/workshop.rs `refusal`): ``scene`` and ``video`` always, ``web``
only while web wallpapers are allowed, anything else refused.

No engine is started: its launch points are replaced with recorders.
"""

from __future__ import annotations

import json

import pytest

from tests.test_scenes import _QuietRenderer
from tests.web_previews import Fixture
from wall_in_one.library import stills, workshop
from wall_in_one.library.model import MediaItem
from wall_in_one.wallpaper import scenes
from wall_in_one.wallpaper.applier import Applier, ApplyError


def _become(fixture: Fixture, kind: str, monkeypatch: pytest.MonkeyPatch) -> MediaItem:
    """The scanned scene, with its ``project.json`` rewritten to ``kind`` where the engine looks."""
    old = fixture.scene
    home = fixture.root.parent / "engine-home"
    (home / ".local" / "share").mkdir(parents=True)
    (home / ".local" / "share" / "Steam").symlink_to(old.path.parents[4], target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    (old.path / "project.json").write_text(
        json.dumps({"type": kind, "title": "Updated", "file": "run.exe"}), encoding="utf-8"
    )
    return old


#: (type, web allowed, may the engine start)
CASES = [
    ("application", True, False),
    ("application", False, False),
    ("web", True, True),
    ("web", False, False),
    ("scene", True, True),
    ("scene", False, True),
    ("video", True, True),
    ("video", False, True),
]
IDS = [f"{kind}-web-{'on' if web else 'off'}" for kind, web, _ in CASES]


@pytest.mark.parametrize(("kind", "allow_web", "starts"), CASES, ids=IDS)
def test_engine_refusal_reads_the_type_every_time(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str, allow_web: bool, starts: bool
) -> None:
    item = _become(web_folder, kind, monkeypatch)
    refusal = workshop.engine_refusal(item.scene, allow_web=allow_web)
    assert (refusal == "") is starts, refusal
    if refusal:
        # Only a web page's reason may name web wallpapers, and only while they are off.
        assert ("web wallpapers" in refusal) is False
        assert ("web page" in refusal) is (kind == "web")


@pytest.mark.parametrize(("kind", "allow_web", "starts"), CASES, ids=IDS)
def test_still_capture_dispatches_the_engine_only_for_what_may_start(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str, allow_web: bool, starts: bool
) -> None:
    item = _become(web_folder, kind, monkeypatch)
    dispatched: list[str] = []

    def screenshot(scene: str, *_args: object, **_kwargs: object) -> None:
        dispatched.append(scene)
        raise scenes.SceneError("intercepted: the engine was not started")

    monkeypatch.setattr(scenes, "screenshot", screenshot)
    with pytest.raises(stills.StillError):
        stills.capture_scene(item, web_folder.root, force=True, size=(64, 40), allow_web=allow_web)
    assert dispatched == ([item.scene] if starts else [])


@pytest.mark.parametrize(("kind", "allow_web", "starts"), CASES, ids=IDS)
def test_legacy_applier_starts_the_engine_only_for_what_may_start(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str, allow_web: bool, starts: bool
) -> None:
    item = _become(web_folder, kind, monkeypatch)
    monkeypatch.setattr(scenes, "running_elsewhere", lambda _output: ())
    started: list[str] = []

    class Engine:
        def start(self, scene: str, properties: object = ()) -> None:
            started.append(scene)

        def stop(self) -> None: ...

    applier = Applier(_QuietRenderer(), scene_renderer=Engine(), own_scene_renderer=True)  # type: ignore[arg-type]
    if starts:
        applier.apply(item, dynamics_enabled=True, allow_web=allow_web)
    else:
        with pytest.raises(ApplyError) as raised:
            applier.apply(item, dynamics_enabled=True, allow_web=allow_web)
        assert ("web wallpapers" in str(raised.value)) is False
    assert started == ([item.scene] if starts else [])
