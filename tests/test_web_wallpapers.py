"""Web wallpapers are off by default, and off means never played (owner decision).

A web wallpaper runs its page's JavaScript in linux-wallpaperengine's own,
older Chromium with web security and site isolation off
(docs/wallpaper-engine.md). While ``allow_web_wallpapers`` is off, a web item
and a preset of one are listed but never compiled, applied or given a still;
executables stay refused whatever it says. Steam's folder is a fixture.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.library import scan, workshop
from wall_in_one.library.model import MediaItem
from wall_in_one.session import Session
from wall_in_one.wallpaper.applier import ApplyError


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    """A web wallpaper (300), a preset of it (400), an executable (100), a scene (200)."""
    steam = tmp_path / "steam"
    content = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID
    for workshop_id, project in (
        ("100", {"type": "application", "title": "Trojan", "file": "run.exe"}),
        ("200", {"type": "scene", "title": "Toothless", "file": "scene.json"}),
        ("300", {"type": "web", "title": "Electric Eye", "file": "index.html"}),
        ("400", {"title": "Y2K Matrix", "dependency": "300", "preset": {"a": True}}),
    ):
        (content / workshop_id).mkdir(parents=True)
        (content / workshop_id / "project.json").write_text(json.dumps(project))
    root = tmp_path / "library"
    automatic = root / "Wall-in-One" / "Automatic Stills"
    automatic.mkdir(parents=True)
    for workshop_id in ("100", "200", "300", "400"):
        # A still on record for each: what keeps one out is the setting alone.
        (automatic / f"{workshop_id}.png").write_bytes(b"\x89PNG")
    return steam, root


def _session(tmp_path: Path, *, allow: bool) -> tuple[config.Settings, Session]:
    steam, root = _fixture(tmp_path)
    settings = config.Settings(roots=(root,), allow_web_wallpapers=allow)
    session = Session(
        settings,
        scanner=lambda roots: scan.scan(
            roots, include_workshop=True, workshop_roots=(steam,), records={}
        ),
    )
    session.refresh()
    return settings, session


def _compiled_scenes(settings: config.Settings, session: Session) -> set[str]:
    document = tomllib.loads(runtime_config.render(settings, session))
    return {
        entry["scene_id"]
        for playlist in document["playlists"]
        for entry in playlist["entries"]
        if "scene_id" in entry
    }


def _items(session: Session) -> dict[str, MediaItem]:
    return {item.scene: item for item in session.library.items}


def test_the_setting_is_off_by_default_and_written_only_when_on(tmp_path: Path) -> None:
    assert config.Settings().allow_web_wallpapers is False
    assert "allow_web_wallpapers" not in config.Settings().to_toml()
    path = tmp_path / "settings.toml"
    config.save(config.Settings(allow_web_wallpapers=True), path)
    assert "allow_web_wallpapers = true" in path.read_text()
    assert config.load_strict(path).allow_web_wallpapers is True
    path.write_text('allow_web_wallpapers = "yes"\n')
    with pytest.raises(config.ConfigError, match="allow_web_wallpapers must be a boolean"):
        config.load_strict(path)


def test_while_off_web_pages_are_listed_but_never_compiled(tmp_path: Path) -> None:
    settings, session = _session(tmp_path, allow=False)
    items = _items(session)

    assert set(items) == {"100", "200", "300", "400"}, "all listed"
    assert items["300"].web_page and items["400"].web_page
    assert not items["200"].web_page
    assert _compiled_scenes(settings, session) == {"200"}
    for workshop_id in ("300", "400"):
        can = runtime_config.entry_playability(items[workshop_id], session)
        assert can == runtime_config.Playability(False, False, workshop.WEB_OFF_REASON)
    # Not counted as unsupported: off is a choice, not a lack.
    assert workshop.unsupported_titles(session.library.items) == ("Trojan",)
    assert workshop.web_off_titles(session.library.items, False) == ("Electric Eye", "Y2K Matrix")


def test_turned_on_they_play_and_executables_still_dont(tmp_path: Path) -> None:
    settings, session = _session(tmp_path, allow=True)

    assert _compiled_scenes(settings, session) == {"200", "300", "400"}
    document = runtime_config.render(settings, session)
    preset = next(
        entry
        for playlist in tomllib.loads(document)["playlists"]
        for entry in playlist["entries"]
        if entry.get("scene_id") == "400"
    )
    assert preset["scene_base"] == "300" and "properties" not in preset
    assert workshop.web_off_titles(session.library.items, True) == ()
    # Toggling is a settings change, compiled like any other: off again, gone again.
    assert _compiled_scenes(replace(settings, allow_web_wallpapers=False), session) == {"200"}


def test_while_off_the_legacy_applier_is_never_handed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    monkeypatch.setattr(
        "wall_in_one.wallpaper.scenes.SceneRenderer.start",
        lambda self, scene, properties=(): started.append(scene),
    )
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", lambda *a, **k: None)
    _settings, session = _session(tmp_path, allow=False)

    with pytest.raises(ApplyError, match="web wallpapers are off"):
        session.select(_items(session)["300"].path)
    assert started == []
