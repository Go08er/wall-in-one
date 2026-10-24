"""A synthetic Workshop web wallpaper, and a record of everything that touches its folder.

Shared by `test_web_previews_off` and `test_ui_web_previews_off`. See the first
for why every file but ``project.json`` is mode 000 and an audit hook watches.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wall_in_one import thumbnails
from wall_in_one.library import scan, stills, workshop
from wall_in_one.library.model import MediaItem
from wall_in_one.wallpaper import scenes

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 400 + b"\x00\x00\x00\x00IEND\xaeB`\x82"


class _Watch:
    """What touched the web folders, filled by one process-wide audit hook."""

    def __init__(self) -> None:
        self.folders: tuple[str, ...] = ()
        self.names: frozenset[str] = frozenset()
        self.opens: list[str] = []
        self.started: list[list[str]] = []
        self.on = False

    def clean(self) -> bool:
        return not self.opens and not self.started


WATCH = _Watch()
PROGRAMS = ("ffmpeg", "linux-wallpaperengine")


def _audit(event: str, arguments: tuple[Any, ...]) -> None:
    if not WATCH.on:
        return
    if event == "open":
        target = arguments[0]
        if isinstance(target, bytes):
            target = os.fsdecode(target)
        if not isinstance(target, str):
            try:
                target = os.fspath(target)
            except TypeError:
                return
        if target == "project.json" or target.endswith("/project.json"):
            return
        if target.startswith(WATCH.folders) or Path(target).name in WATCH.names:
            WATCH.opens.append(target)
    elif event == "subprocess.Popen":
        argv = [str(part) for part in arguments[1]] if arguments[1] else []
        if argv and Path(argv[0]).name in PROGRAMS:
            WATCH.started.append(argv)


sys.addaudithook(_audit)


@pytest.fixture
def watch() -> Iterator[_Watch]:
    WATCH.opens.clear()
    WATCH.started.clear()
    WATCH.on = True
    try:
        yield WATCH
    finally:
        WATCH.on = False


class Fixture(SimpleNamespace):
    web: MediaItem
    preset: MediaItem
    scene: MediaItem
    malformed: MediaItem
    orphan: MediaItem
    bad_scene: MediaItem
    root: Path
    cache: Path
    folders: tuple[Path, ...]


@pytest.fixture
def web_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fixture]:
    steam = tmp_path / "steam"
    content = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID
    projects = {
        "300": {
            "type": "web",
            "title": "Electric Eye",
            "file": "index.html",
            "preview": "preview.gif",
        },
        "400": {"title": "Y2K Matrix", "dependency": "300", "preset": {"a": True}},
        # Refused by the scan, so not `web_page`: a web item with no page to name,
        # a preset of an uninstalled wallpaper, and (the control) a malformed scene.
        "500": {"type": " Web ", "title": "No Page", "preview": "preview.gif"},
        "600": {"title": "Orphan", "dependency": "999", "preset": {"a": True}, "preview": "p.gif"},
        "700": {"type": "scene", "title": "Bad Scene", "file": "scene.txt", "preview": "p.gif"},
        "200": {"type": "scene", "title": "Toothless", "file": "scene.json", "preview": "p.gif"},
    }
    for workshop_id, project in projects.items():
        (content / workshop_id).mkdir(parents=True)
        (content / workshop_id / "project.json").write_text(json.dumps(project))
        for name in ("index.html", "preview.gif", "p.gif", "scene.pkg", "extra.js"):
            (content / workshop_id / name).write_bytes(b"GIF89a<html>")
    root = tmp_path / "library"
    (root / "Wall-in-One" / "Automatic Stills").mkdir(parents=True)
    library = scan.scan([root], include_workshop=True, workshop_roots=(steam,), records={})
    items = {item.scene: item for item in library.items}
    assert items["500"].unplayable is not None and not items["500"].web_page
    assert items["600"].unplayable is not None and not items["600"].web_page
    assert items["700"].unplayable is not None and not items["700"].holds_web
    assert items["300"].web_page and items["400"].web_page and not items["200"].web_page
    assert items["300"].preview is not None
    # Unreadable from here on: any attempt to read one is an error somewhere.
    folders = tuple(content / name for name in projects)
    for folder in folders:
        for entry in folder.iterdir():
            if entry.name != "project.json":
                entry.chmod(0o000)
    cache = tmp_path / "cache"
    monkeypatch.setattr(thumbnails, "cache_directory", lambda: cache)
    monkeypatch.setattr(thumbnails, "is_available", lambda: True)
    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(scenes, "is_available", lambda: True)
    WATCH.folders = tuple(str(folder) for folder in folders)
    WATCH.names = frozenset({"index.html", "preview.gif", "p.gif", "scene.pkg", "extra.js"})
    try:
        yield Fixture(
            web=items["300"],
            preset=items["400"],
            scene=items["200"],
            malformed=items["500"],
            orphan=items["600"],
            bad_scene=items["700"],
            root=root,
            cache=cache,
            folders=folders,
        )
    finally:
        for folder in folders:
            for entry in folder.iterdir():
                entry.chmod(0o600)


def both(web_folder: Fixture) -> tuple[MediaItem, ...]:
    """The web wallpaper and the preset of it."""
    return (web_folder.web, web_folder.preset)
