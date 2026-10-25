"""What the end of a quit reads of the Workshop items a ``runtime.toml`` names (review r13f, L2).

For a service with no check of an item's type of its own, the shutdown's last step judges the
file by the ids it names: read off the compiler's own lines when that is all it names
(`runtime_config.compiled_scenes`), otherwise parsed (`runtime_config.named_scenes`), then each
id's own ``project.json`` (`workshop.may_be_web`), never a scan. Every doubt counts as a web page.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wall_in_one import runtime_config
from wall_in_one.library import workshop

CONTENT = ".local/share/Steam/steamapps/workshop/content/431960"


def _item(identifier: str, project: object) -> Path:
    folder = Path(os.environ["HOME"]) / CONTENT / identifier
    folder.mkdir(parents=True)
    (folder / "project.json").write_text(json.dumps(project))
    return folder


SCENE = {"type": "scene", "title": "A scene", "file": "scene.json"}
WEB = {"type": "web", "title": "A page", "file": "index.html"}


def _entries(*lines: str) -> str:
    return 'schema_version = 4\n[[playlists]]\nid = "p"\n' + "".join(
        f"[[playlists.entries]]\n{line}\n" for line in lines
    )


# -- the ids a document names ---------------------------------------------------------------------


def test_the_compilers_lines_are_read_unparsed() -> None:
    text = _entries('scene_id = "101"', 'scene_id = "102"\nscene_base = "103"')
    assert runtime_config.compiled_scenes(text) == frozenset({"101", "102", "103"})
    assert runtime_config.named_scenes(text) == frozenset({"101", "102", "103"})


@pytest.mark.parametrize(
    "line",
    [
        'scene_id="101"',  # a hand edit's spacing
        '  scene_id = "101"',
        '"scene_id" = "101"',
        '"scene_i\\u0064" = "101"',
        'title = "my scene_id"',  # the name in a value: a parse tells
        "scene_id = '101'",
    ],
)
def test_anything_else_is_left_to_a_parse(line: str) -> None:
    assert runtime_config.compiled_scenes(_entries('scene_id = "102"', line)) is None


def test_a_document_that_isnt_toml_1_0_names_what_cant_be_told() -> None:
    """`named_scenes`: None, never "none" (the service may read ids in it)."""
    text = _entries('scene_id="101"', 'name = "\\x41"')
    assert runtime_config.compiled_scenes(text) is None
    assert runtime_config.named_scenes(text) is None


def test_control_a_compiled_document_that_names_nothing() -> None:
    assert runtime_config.compiled_scenes(_entries('kind = "still"')) == frozenset()


# -- each id's own project.json -------------------------------------------------------------------


def test_a_web_page_and_a_preset_of_one_are_found() -> None:
    _item("101", SCENE)
    _item("104", WEB)
    _item("105", {"title": "Preset", "preset": {"a": 1}, "dependency": "104"})
    assert workshop.may_be_web(["101", "104"])
    assert workshop.may_be_web(["101", "105"])


def test_control_scenes_and_a_preset_of_one_are_not() -> None:
    _item("101", SCENE)
    _item("105", {"title": "Preset", "preset": {"a": 1}, "dependency": "101"})
    assert not workshop.may_be_web(["101", "105"])
    assert not workshop.may_be_web([])


@pytest.mark.parametrize(
    "doubt",
    [
        "not-a-workshop-id",
        "not-installed",
        "project-not-json",
        "project-too-large",
        "project-a-link",
        "preset-of-nothing-installed",
        "preset-of-a-preset",
        "given-up",
    ],
)
def test_every_doubt_counts_as_a_web_page(doubt: str) -> None:
    _item("101", SCENE)
    named = ["101"]
    give_up = False
    if doubt == "not-a-workshop-id":
        named = ["../101"]
    elif doubt == "not-installed":
        named = ["109"]
    elif doubt == "project-not-json":
        (_item("102", SCENE) / "project.json").write_text("{")
        named = ["102"]
    elif doubt == "project-too-large":
        (_item("102", SCENE) / "project.json").write_text(
            json.dumps({**SCENE, "pad": "x" * workshop.MAX_PROJECT_BYTES})
        )
        named = ["102"]
    elif doubt == "project-a-link":
        folder = _item("102", SCENE)
        (folder / "project.json").unlink()
        (folder / "project.json").symlink_to(folder.parent / "101" / "project.json")
        named = ["102"]
    elif doubt == "preset-of-nothing-installed":
        _item("105", {"title": "Preset", "preset": {"a": 1}, "dependency": "109"})
        named = ["105"]
    elif doubt == "preset-of-a-preset":
        _item("105", {"title": "Preset", "preset": {"a": 1}, "dependency": "106"})
        _item("106", {"title": "Preset", "preset": {"a": 1}, "dependency": "101"})
        named = ["105"]
    else:
        give_up = True
    assert workshop.may_be_web(named, lambda: give_up), doubt
