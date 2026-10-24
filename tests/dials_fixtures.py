"""What the renderer-dial tests share: a library, the excluded options, an older service.

docs/renderer-settings.md.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config
from wall_in_one.dials import SceneProperty
from wall_in_one.library import pairings, workshop
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.library.scan import WORKSHOP_PROVIDER
from wall_in_one.session import Session

SERVICE = Path(__file__).resolve().parents[1] / "service" / "src"

#: mpv and engine options that run code or read or write a file of their
#: choosing (docs/renderer-settings.md, "Left out"). None may ever be produced.
EXCLUDED = (
    "script",
    "scripts",
    "load-scripts",
    "include",
    "config",
    "config-dir",
    "input-conf",
    "input-ipc-server",
    "sub-file",
    "audio-file",
    "external-files",
    "cover-art-files",
    "glsl-shader",
    "glsl-shaders",
    "vf",
    "af",
    "lavfi-complex",
    "demuxer-lavf-o",
    "stream-lavf-o",
    "ytdl",
    "ytdl-format",
    "script-opts",
    "log-file",
    "dump-stats",
    "stream-record",
    "screenshot-directory",
    "watch-later-directory",
    "playlist",
    "cookies-file",
    "profile",
    "assets-dir",
    "screenshot",
    "fork",
)

#: An injection attempt per shape a value can take, for every input.
INJECTIONS: tuple[object, ...] = (
    "fill script=/tmp/x.lua",
    "lanczos\ninclude=/etc/passwd",
    "1.5 script=/tmp/x.lua",
    '" ; --script=/tmp/x.lua',
    "--script=/tmp/x.lua",
    float("nan"),
    float("inf"),
    10**400,
    [1],
    {"script": "/tmp/x.lua"},
    None,
)


def bare_toml_values(text: str) -> list[str]:
    return re.findall(r"=\s*([^,{}]+?)\s*(?:,|}|$)", text)


def library(
    tmp_path: Path, *extra: MediaItem, store: pairings.Store | None = None
) -> tuple[config.Settings, Session, dict[str, MediaItem]]:
    """A video, a scene with properties to set, a preset of it and a web page."""
    root = tmp_path / "library"
    content = tmp_path / "steam" / "steamapps" / "workshop" / "content" / workshop.APP_ID
    automatic = root / "Wall-in-One" / "Automatic Stills"
    settable = (
        SceneProperty("bloom", "bool", "Bloom", "true"),
        SceneProperty("speed", "slider", "Speed", "1", minimum=0.0, maximum=4.0, step=0.1),
        SceneProperty("tint", "color", "Tint", "1.000000 0.000000 0.000000"),
        SceneProperty("style", "combo", "Style", "a", options=(("a", "Calm"), ("b", "Wild"))),
        SceneProperty("motto", "textinput", "Motto", "hello"),
    )

    def scene(workshop_id: str, title: str, kind: str, **fields: object) -> MediaItem:
        return replace(
            MediaItem(
                content / workshop_id,
                Kind.SCENE,
                7,
                1,
                provider=WORKSHOP_PROVIDER,
                scene=workshop_id,
                title=title,
                paired_still=automatic / f"{workshop_id}.png",
                workshop_type=kind,
            ),
            **fields,  # type: ignore[arg-type]
        )

    items = {
        "video": MediaItem(root / "clip.mp4", Kind.VIDEO, 7, 1, paired_still=root / "clip.png"),
        "scene": scene("1647046763", "Toothless", "scene", scene_settings=settable),
        "preset": scene(
            "2946165131",
            "Toothless at night",
            "preset",
            scene_base="1647046763",
            scene_properties=(("bloom", "false"),),
            scene_settings=settable,
        ),
        "web": scene("2777242023", "Electric Eye", "web", web_page=True),
    }
    for item in extra:
        items[item.title] = item
    settings = config.Settings(roots=(root,), allow_web_wallpapers=True)
    library = Library(roots=(root,), items=tuple(items.values()))
    session = Session(settings, scanner=lambda _roots: library, pairing_store=store)
    session.refresh()
    return settings, session, items


def entry(document: str, key: str, value: str) -> dict[str, object]:
    decoded = tomllib.loads(document)
    return next(
        found
        for playlist in decoded["playlists"]
        for found in playlist["entries"]
        if found.get(key) == value
    )


def renderer_lines(document: str) -> list[str]:
    lines = document.split("\n")
    start = lines.index("[renderer]")
    end = lines.index("", start)
    return lines[start + 1 : end]


def live_service(monkeypatch: pytest.MonkeyPatch, schemas: list[int]) -> dict[str, object]:
    from wall_in_one.control import client
    from wall_in_one.control.protocol import Response

    status: dict[str, object] = {"status_version": 2, "supported_config_schemas": schemas}

    def send(verb: str, *, timeout: float) -> Response:
        assert verb == "status"
        return Response(ok=True, message=json.dumps(status))

    monkeypatch.setattr(client, "send_runtime", send)
    return status
