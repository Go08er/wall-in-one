"""Codex review 0.4.2: an old preset preview after its root was removed (R42-4).

The record shape is the same already-supported 0.4.1 record tested by
test_scene_preset_stills. No rendering or live service is involved.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tests.test_scene_preset_stills import PNG, PRESET, Tree
from wall_in_one import config, runtime_config
from wall_in_one.library import pairings, scan
from wall_in_one.session import Session


@pytest.mark.parametrize("case", ["old-root-removed", "control-old-root-current", "control-manual"])
def test_an_old_preset_preview_is_not_revived_by_removing_its_root(
    tmp_path: Path, case: str
) -> None:
    tree = Tree(tmp_path)
    original = tree.scan()[PRESET]
    old = tree.automatic / f"{PRESET}.png"
    manual = tree.root / "hand-picked.png"
    manual.write_bytes(PNG)
    other = tmp_path / "other-library"
    other.mkdir()
    roots = (tree.root,) if case == "control-old-root-current" else (other,)
    chosen = manual if case == "control-manual" else old
    identity = pairings.Identity.of(original)
    record = pairings.Pairing(identity=identity, still=chosen, customized=True)
    records = {identity.key: record}
    settings = config.Settings(roots=roots, allow_web_wallpapers=False)
    session = Session(
        settings,
        scanner=lambda _roots: scan.scan(
            roots, records, include_workshop=True, workshop_roots=(tree.steam,), previews=False
        ),
        pairing_store=pairings.Store(records),
    )
    try:
        session.refresh()
        item = next(item for item in session.library.items if item.scene == PRESET)
        accepted = session.pairings.resolve_accepted(item, session.library)
        document = tomllib.loads(runtime_config.render(settings, session))
        rendered = {
            entry["scene_id"]: entry["still"]
            for playlist in document.get("playlists", [])
            for entry in playlist["entries"]
            if "scene_id" in entry
        }
        if case == "control-manual":
            assert accepted.still == manual
            assert rendered[PRESET] == str(manual)
        else:
            assert accepted.still is None, (accepted.still, rendered)
            assert PRESET not in rendered, rendered
        assert old.read_bytes() == PNG, "old files are not deleted"
    finally:
        session.shutdown()
