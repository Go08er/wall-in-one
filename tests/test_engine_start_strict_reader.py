"""The engine-start guard reads strictly, as the service does (R42-3).

From the 0.4.2 review's diagnostic; never starts a renderer.

Desired policy: unknown types and malformed project JSON are refused at both
Python engine launch functions, matching the service. A dispatch recorder
raises before subprocess creation, ownership registration or any signal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_engine_start_type_web_on import _become
from tests.web_previews import Fixture
from wall_in_one.wallpaper import scenes


class InterceptedDispatchError(Exception):
    """The external engine boundary was reached; no subprocess was created."""


CASES = [
    (
        "padded-web-is-not-an-engine-type",
        b'{"type":" web ","title":"Current","file":"index.html"}',
        True,
        False,
    ),
    (
        "nonstandard-NaN-is-not-JSON",
        b'{"type":"scene","title":"Current","file":"scene.json","unused":NaN}',
        True,
        False,
    ),
    (
        "invalid-UTF8-is-not-a-project",
        b'{"type":"scene","title":"Current","file":"scene.json","unused":"\xff"}',
        True,
        False,
    ),
    (
        "control-valid-scene",
        b'{"type":"scene","title":"Current","file":"scene.json"}',
        True,
        True,
    ),
    (
        "control-enabled-valid-web",
        b'{"type":"web","title":"Current","file":"index.html"}',
        True,
        True,
    ),
    (
        "control-disabled-web",
        b'{"type":"web","title":"Current","file":"index.html"}',
        False,
        False,
    ),
    (
        "control-application-always-refused",
        b'{"type":"application","title":"Current","file":"run.exe"}',
        True,
        False,
    ),
]


@pytest.mark.parametrize("launch", ["play", "screenshot"])
@pytest.mark.parametrize(
    ("case", "project", "allow_web", "should_dispatch"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_launch_guard_uses_engine_strict_json_and_type(
    web_folder: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    launch: str,
    case: str,
    project: bytes,
    allow_web: bool,
    should_dispatch: bool,
) -> None:
    item = _become(web_folder, "scene", monkeypatch)
    (item.path / "project.json").write_bytes(project)
    dispatched: list[str] = []

    def intercept(argv: list[str], **_kwargs: object) -> None:
        dispatched.append(argv[-1])
        raise InterceptedDispatchError(f"{case}: engine dispatch for {argv[-1]}, intercepted")

    monkeypatch.setattr(scenes, "is_available", lambda: True)
    monkeypatch.setattr(
        scenes.SceneRenderer, "command", lambda _self, scene, **_kwargs: ["engine", scene]
    )
    monkeypatch.setattr(subprocess, "Popen", intercept)

    renderer = scenes.SceneRenderer()

    def invoke() -> None:
        if launch == "play":
            renderer.start(item.scene, allow_web=allow_web)
        else:
            scenes.screenshot(
                item.scene, tmp_path / "capture.png", size=(64, 40), allow_web=allow_web
            )

    if should_dispatch:
        with pytest.raises(InterceptedDispatchError):
            invoke()
        assert dispatched == [item.scene]
    else:
        with pytest.raises(scenes.SceneRefusedError):
            invoke()
        assert dispatched == []
