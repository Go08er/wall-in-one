"""Findings F4-F7 and the test gaps of the 0.4.2 review (stills, pairing, settings)."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config
from wall_in_one.library import pairing, scan, stills, workshop
from wall_in_one.library.model import Library, MediaItem, Unplayable
from wall_in_one.session import Session

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (64).to_bytes(4, "big") * 2 + b"\x08\x02\0\0\0"


class _Tree:
    """A base scene (200), a preset of it (500) and a web page (300), under a library root."""

    def __init__(self, home: Path) -> None:
        self.steam = home / ".local/share/Steam"
        self.content = self.steam / "steamapps/workshop/content" / workshop.APP_ID
        self.root = home / "library"
        self.automatic = pairing.still_directory(self.root)
        self.automatic.mkdir(parents=True)
        self.put(
            "200",
            {
                "type": "scene",
                "title": "Base",
                "file": "scene.json",
                "general": {"properties": {"bloom": {"type": "bool", "value": False}}},
            },
        )
        self.put("500", {"title": "Preset", "dependency": "200", "preset": {"bloom": True}})
        self.put("300", {"type": "web", "title": "Web", "file": "index.html", "preview": "p.png"})

    def put(self, workshop_id: str, project: dict[str, object]) -> None:
        folder = self.content / workshop_id
        folder.mkdir(parents=True)
        (folder / "project.json").write_text(json.dumps(project))
        for name in ("scene.json", "scene.pkg", "index.html", "p.png"):
            (folder / name).write_bytes(b"x")

    def scan(self, *, previews: bool = False) -> Library:
        return scan.scan(
            [self.root], {}, include_workshop=True, workshop_roots=(self.steam,), previews=previews
        )


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Tree:
    monkeypatch.setenv("HOME", str(tmp_path))
    return _Tree(tmp_path)


def _names(library: Library, tree: _Tree) -> set[str]:
    return {item.path.name for item in library.items if item.path.parent == tree.automatic}


# -- F4 / N2: the capture is hidden by name, whatever the preset reads as -----------------------


def test_a_presets_capture_is_hidden_from_the_library(tree: _Tree) -> None:
    (tree.automatic / "500-capture.png").write_bytes(PNG)
    assert _names(tree.scan(), tree) == set()


def test_the_capture_stays_hidden_when_the_base_goes_missing(tree: _Tree) -> None:
    """The preset then fails closed to a web page's kind, which changed its derived names."""
    (tree.automatic / "500-capture.png").write_bytes(PNG)
    for child in (tree.content / "200").iterdir():
        child.unlink()
    (tree.content / "200").rmdir()
    library = tree.scan()
    preset = next(item for item in library.items if item.scene == "500")
    assert preset.preview_kind, "the premise: unclassifiable base, so it reads as a web page's"
    assert _names(library, tree) == set()


def test_capture_destination_is_by_name_alone(tree: _Tree) -> None:
    preset = next(item for item in tree.scan().items if item.scene == "500")
    assert stills.capture_destination(preset, tree.root) == tree.automatic / "500-capture.png"
    assert stills.capture_destination(replace(preset, scene_base=""), tree.root) is None


# -- F5 / N3: the previews setting is part of what a library is current for ----------------------


def test_the_previews_setting_is_a_library_source() -> None:
    a = config.Settings(allow_web_wallpapers=True, web_wallpaper_previews=True)
    b = replace(a, web_wallpaper_previews=False)
    assert a.library_sources != b.library_sources
    assert replace(a, allow_web_wallpapers=False).library_sources == (*b.library_sources[:2], False)


def test_moving_the_previews_setting_rescans_the_library(tree: _Tree) -> None:
    scans: list[bool] = []

    def scanner(_roots: object) -> Library:
        scans.append(True)
        return tree.scan()

    settings = config.Settings(roots=(tree.root,), allow_web_wallpapers=True)
    session = Session(settings, scanner=scanner)
    try:
        session.refresh()
        before = len(scans)
        session.update_settings(replace(settings, web_wallpaper_previews=True))
        assert len(scans) == before + 1, "the setting is resolved by the scan: a rescan"
        session.update_settings(replace(settings, web_wallpaper_previews=True, cycle_interval=99))
        assert len(scans) == before + 1, "an unrelated setting is not"
    finally:
        session.shutdown()


def test_a_library_scanned_under_the_other_value_is_not_current_for_a_saved_view(
    tree: _Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another program saved the setting off while the app holds a library scanned with it on."""
    settings = config.Settings(
        roots=(tree.root,), allow_web_wallpapers=True, web_wallpaper_previews=True
    )
    config.save(settings)  # no key written: previews read on, as in a 0.4.1 profile
    session = Session(settings)  # the real scanner: Steam is under the test's HOME
    try:
        session.refresh()
        assert session.library.previews
        config.save(replace(settings, web_wallpaper_previews=False))
        view = session.saved_view()()
        try:
            assert not view.settings.preview_stills
            assert view.library.previews is False, "scanned afresh, not the stale library"
        finally:
            view.shutdown()
    finally:
        session.shutdown()


# -- F6: the still queue asks again when the setting comes back --------------------------------


def test_a_web_page_queued_before_previews_went_off_is_asked_for_again(
    tree: _Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.ui.stills import StillMaker

    on = {"previews": True}
    maker = StillMaker(web_allowed=lambda: True, previews=lambda: on["previews"])
    web = next(item for item in tree.scan(previews=True).items if item.scene == "300")
    submitted: list[tuple[MediaItem, ...]] = []
    monkeypatch.setattr(
        maker, "_submit", lambda _work, items, *_rest, context: submitted.append(items)
    )
    monkeypatch.setattr(maker, "_local_displays", lambda: ())
    maker.request([web], tree.root, lambda made: None)
    assert submitted == [(web,)] and web.path in maker._attempted

    on["previews"] = False  # between the queueing and the worker reaching it
    ensured: list[MediaItem] = []
    monkeypatch.setattr(stills, "ensure", lambda item, *_a, **_k: ensured.append(item))
    monkeypatch.setattr("wall_in_one.wallpaper.scenes.measured_capture_size", lambda *_a: (64, 64))
    maker._run((web,), tree.root, lambda made: None)
    assert ensured == [] and web.path not in maker._attempted

    on["previews"] = True
    maker.request([web], tree.root, lambda made: None)
    assert submitted == [(web,), (web,)], "turning previews back on asks for it again"
    maker.shutdown()


# -- N4: turning web on never turns previews on, on every writer path --------------------------


def test_turning_web_on_in_a_new_profile_leaves_previews_off_on_every_writer(
    tmp_path: Path,
) -> None:
    target = tmp_path / "settings.toml"
    config.save(config.Settings(), target)
    updated = config.update({"allow_web_wallpapers": True}, target)
    assert updated.allow_web_wallpapers and not updated.web_wallpaper_previews
    document = tomllib.loads(target.read_text(encoding="utf-8"))
    assert document["allow_web_wallpapers"] is True
    assert document["web_wallpaper_previews"] is False
    assert not config.load(target).preview_stills
    # The classic page's write is the same call with every field: still off.
    again = config.update({"allow_web_wallpapers": True, "web_wallpaper_previews": False}, target)
    assert not again.preview_stills


def test_an_upgraded_profile_turning_web_off_keeps_the_saved_previews_value(
    tmp_path: Path,
) -> None:
    target = tmp_path / "settings.toml"
    target.write_text("roots = []\nallow_web_wallpapers = true\n", encoding="utf-8")
    assert config.load(target).preview_stills  # 0.4.1's profile: on
    config.update({"allow_web_wallpapers": False}, target)
    document = tomllib.loads(target.read_text(encoding="utf-8"))
    assert document["web_wallpaper_previews"] is True, "kept, though it does not apply"
    assert not config.load(target).preview_stills


# -- N1: the words follow the setting ------------------------------------------------------------


def test_the_still_note_follows_the_preview_setting(tree: _Tree) -> None:
    from wall_in_one.ui.next import real_state

    on = next(i for i in tree.scan(previews=True).items if i.scene == "300")
    (tree.automatic / "300.png").write_bytes(PNG)
    on = next(i for i in tree.scan(previews=True).items if i.scene == "300")
    off = next(i for i in tree.scan(previews=False).items if i.scene == "300")
    assert (
        real_state._still_note(on, None, False, True) == "Taken from its Wallpaper Engine preview"
    )
    note = real_state._still_note(off, None, False, False)
    assert "choose one by hand" in note and "preview" not in note
    # An item that can't play gets neither line: there is nothing to pick for.
    cant = replace(off, unplayable=Unplayable.RUNTIME)
    assert "choose one by hand" not in real_state._still_note(cant, None, False, False)
