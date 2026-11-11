"""A preset of a true scene gets an engine still of its own making, whatever the toggle says.

The owner's rule: a still is automatic (Wall-in-One made it from the item itself)
or picked. The engine's screenshot of a preset's base with the preset's own
values is made from the item itself, so only a web page, and a preset of one,
depend on the preview setting. 0.4.1's preview-made `<id>.png` is never used
for a preset of a true scene and is never "already captured"; the capture goes
under `pairing.scene_still_stem`'s name and nothing is deleted.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from wall_in_one import config
from wall_in_one import runtime_config as rc
from wall_in_one.library import pairing, pairings, scan, stills, workshop
from wall_in_one.library.model import MediaItem
from wall_in_one.session import Session
from wall_in_one.wallpaper import scenes

BASE, PRESET, WEB, WEB_PRESET = "200", "500", "300", "400"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (64).to_bytes(4, "big") * 2 + b"\x08\x02\0\0\0"


class Tree:
    def __init__(self, home: Path) -> None:
        self.steam = home / ".local/share/Steam"
        self.content = self.steam / "steamapps/workshop/content" / workshop.APP_ID
        self.root = home / "library"
        self.automatic = pairing.still_directory(self.root)
        self.automatic.mkdir(parents=True)
        self.put(
            BASE,
            {
                "type": "scene",
                "title": "Base",
                "file": "scene.json",
                "general": {"properties": {"bloom": {"type": "bool", "value": False}}},
            },
        )
        self.put(PRESET, {"title": "Preset", "dependency": BASE, "preset": {"bloom": True}})
        self.put(WEB, {"type": "web", "title": "Web", "file": "index.html", "preview": "p.png"})
        self.put(WEB_PRESET, {"title": "Web preset", "dependency": WEB, "preset": {"a": 1}})
        # 0.4.1 drew a still from each preview, and left it named by the id.
        for workshop_id in (BASE, PRESET, WEB, WEB_PRESET):
            (self.automatic / f"{workshop_id}.png").write_bytes(PNG)

    def put(self, workshop_id: str, project: dict[str, object]) -> None:
        folder = self.content / workshop_id
        folder.mkdir(parents=True)
        (folder / "project.json").write_text(json.dumps(project))
        for name in ("scene.json", "scene.pkg", "index.html", "p.png"):
            (folder / name).write_bytes(b"x")

    def scan(self, *, previews: bool = False) -> dict[str, MediaItem]:
        library = scan.scan(
            [self.root],
            {},
            include_workshop=True,
            workshop_roots=(self.steam,),
            previews=previews,
        )
        return {item.scene: item for item in library.items if item.scene}


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tree:
    monkeypatch.setenv("HOME", str(tmp_path))
    return Tree(tmp_path)


def _engine(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """The engine stubbed at `scenes.screenshot`: what it was asked, and a PNG written."""
    asked: list[dict[str, object]] = []

    def screenshot(scene: str, destination: Path, **keywords: object) -> Path:
        asked.append({"scene": scene, **keywords})
        destination.write_bytes(PNG)
        return destination

    monkeypatch.setattr(scenes, "is_available", lambda: True)
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_a: (64, 64))
    monkeypatch.setattr(scenes, "screenshot", screenshot)
    return asked


@pytest.mark.parametrize("previews", [False, True])
def test_a_preset_of_a_true_scene_never_depends_on_the_preview_setting(
    tree: Tree, previews: bool
) -> None:
    items = tree.scan(previews=previews)
    assert not items[PRESET].preview_kind and items[PRESET].scene_base == BASE
    assert items[WEB].preview_kind and items[WEB_PRESET].preview_kind
    assert not workshop.no_automatic_still(items[PRESET], previews)
    assert not workshop.preview_still(items[PRESET], previews)
    # The old preview-made file is not its still, with the toggle either way.
    assert items[PRESET].paired_still is None


def test_web_off_a_preset_of_a_true_scene_gets_an_engine_still_and_stays_in_the_document(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _engine(monkeypatch)
    item = tree.scan()[PRESET]
    old = tree.automatic / f"{PRESET}.png"
    assert item.paired_still is None

    made = stills.ensure(item, tree.root, allow_web=False, previews=False)

    assert made == tree.automatic / f"{PRESET}-capture.png" and made.is_file()
    assert len(asked) == 1
    assert asked[0]["scene"] == BASE, "the base is rendered, as the service plays a preset"
    assert asked[0]["properties"] == (("bloom", "true"),)
    assert asked[0]["allow_web"] is False
    assert old.read_bytes() == PNG, "the old file is left as it is"

    again = tree.scan()[PRESET]
    assert again.paired_still == made
    assert not stills.scene_capture_required(again, tree.root, size=(64, 64), automatic=True)

    settings = config.Settings(roots=(tree.root,), allow_web_wallpapers=False)
    session = Session(
        settings,
        scanner=lambda _roots: scan.scan(
            [tree.root], {}, include_workshop=True, workshop_roots=(tree.steam,)
        ),
    )
    session.refresh()
    try:
        document = tomllib.loads(rc.render(settings, session))
    finally:
        session.shutdown()
    entries = [e for p in document["playlists"] for e in p["entries"] if "scene_id" in e]
    preset = [e for e in entries if e["scene_id"] == PRESET]
    assert preset and preset[0]["still"] == str(made) and preset[0]["scene_base"] == BASE


def test_the_old_preview_made_file_is_never_already_captured_whatever_its_size(
    tree: Tree,
) -> None:
    item = tree.scan()[PRESET]
    big = tree.automatic / f"{PRESET}.png"
    big.write_bytes(PNG.replace((64).to_bytes(4, "big"), (4000).to_bytes(4, "big")))
    assert stills.scene_capture_required(item, tree.root, size=(64, 64), automatic=True)
    # And it is still hidden from the library, as this app's own file.
    hidden = scan.scan([tree.root], {}, include_workshop=True, workshop_roots=(tree.steam,))
    assert big not in {i.path for i in hidden.items}


def test_a_record_naming_the_old_file_counts_as_no_choice_for_a_preset_of_a_scene(
    tree: Tree,
) -> None:
    item = tree.scan()[PRESET]
    identity = pairings.Identity.of(item)
    old = tree.automatic / f"{PRESET}.png"
    records = {identity.key: pairings.Pairing(identity=identity, still=old, customized=True)}
    for previews in (False, True):
        library = scan.scan(
            [tree.root],
            records,
            include_workshop=True,
            workshop_roots=(tree.steam,),
            previews=previews,
        )
        assert {i.scene: i for i in library.items if i.scene}[PRESET].paired_still is None


def test_a_preset_of_a_web_page_still_follows_the_toggle(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine(monkeypatch)
    off = tree.scan(previews=False)[WEB_PRESET]
    on = tree.scan(previews=True)[WEB_PRESET]
    assert off.paired_still is None and on.paired_still == tree.automatic / f"{WEB_PRESET}.png"
    assert stills.ensure(off, tree.root, allow_web=True, previews=False) is None


def _launch_recorder(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The engine's launch replaced by a recorder that refuses to start."""
    commands: list[list[str]] = []

    def popen(argv: list[str], **_kwargs: object) -> object:
        commands.append(list(argv))
        raise OSError("intercepted: not started")

    monkeypatch.setattr(scenes, "is_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", popen)
    return commands


def test_the_presets_values_reach_the_command_and_the_base_is_the_id(
    tree: Tree, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _launch_recorder(monkeypatch)
    with pytest.raises(scenes.SceneError):
        scenes.screenshot(
            BASE,
            tmp_path / "out.png",
            size=(64, 64),
            allow_web=False,
            properties=(("bloom", "true"),),
        )
    (argv,) = commands
    assert argv[argv.index("--set-property") + 1] == "bloom=true"
    assert argv[-1] == BASE or BASE in argv


@pytest.mark.parametrize(
    "properties",
    [
        (("-bloom", "true"),),
        (("a=b", "1"),),
        (("bloom", "x\n--screen-root"),),
        tuple((f"p{i}", "1") for i in range(workshop.MAX_PROPERTIES + 1)),
    ],
)
def test_invalid_values_are_refused_before_the_engine_starts(
    tree: Tree, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, properties: object
) -> None:
    commands = _launch_recorder(monkeypatch)
    with pytest.raises(scenes.SceneError):
        scenes.screenshot(
            BASE,
            tmp_path / "out.png",
            size=(64, 64),
            allow_web=False,
            properties=properties,  # type: ignore[arg-type]
        )
    assert commands == []


def _retype(tree: Tree, workshop_id: str, kind: str) -> None:
    (tree.content / workshop_id / "project.json").write_text(
        json.dumps({"type": kind, "title": "Changed", "file": "x"}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("base_kind", "allow_web", "refused"),
    [
        ("scene", False, False),
        ("application", False, True),
        ("web", False, True),
        ("web", True, False),
    ],
)
def test_the_type_checked_is_the_base_the_engine_is_given(
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    base_kind: str,
    allow_web: bool,
    refused: bool,
) -> None:
    item = tree.scan()[PRESET]
    _retype(tree, BASE, base_kind)  # since the scan: the preset itself is untouched
    # The preset's own folder says nothing the engine reads: only the base's type counts.
    assert os.path.isfile(tree.content / PRESET / "project.json")
    commands = _launch_recorder(monkeypatch)
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_a: (64, 64))
    with pytest.raises(stills.StillError) as raised:
        stills.capture_scene(item, tree.root, force=True, size=(64, 64), allow_web=allow_web)
    if refused:
        assert commands == [], str(raised.value)
        assert BASE in str(raised.value)
    else:
        assert len(commands) == 1  # the engine was reached (and the stub refused to start it)
        assert commands[0][-1] == BASE
