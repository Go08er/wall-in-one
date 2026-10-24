"""Where the renderer dials are kept: Settings, a wallpaper's pairing, a scene's declarations.

docs/renderer-settings.md. Each dial is one typed, bounded value; a global
default is written to settings.toml only off its default; a wallpaper's own
rides on its pairing record, through every other edit, and a newer build's
dials ride through this one's saves.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from tests.dials_fixtures import INJECTIONS, SERVICE, library
from wall_in_one import config, dials, paths
from wall_in_one.dials import SceneProperty
from wall_in_one.library import pairings, workshop


def test_every_dial_is_typed_bounded_and_matches_the_service() -> None:
    rust_dials = (SERVICE / "dials.rs").read_text(encoding="utf-8")
    rust_config = (SERVICE / "config.rs").read_text(encoding="utf-8")

    def rust_fields(struct: str) -> set[str]:
        body = rust_dials.split(f"pub struct {struct} {{", 1)[1].split("\n}", 1)[0]
        return set(re.findall(r"pub (\w+):", body))

    assert {d.key for d in dials.PER_WALLPAPER["video"]} == rust_fields("VideoOverrides")
    assert {d.key for d in dials.PER_WALLPAPER["scene"]} == rust_fields("SceneOverrides")
    for dial in dials.DIALS:
        assert dial.kind in ("choice", "int", "float", "bool")
        assert dial.valid(dial.default), dial
        if dial.kind == "choice":
            assert dial.default in [value for value, _label in dial.choices]
            for value, _label in dial.choices:
                # One token a config line can't be split at, whatever mpv does with it.
                assert re.fullmatch(r"[A-Za-z0-9_]*", str(value)), value
        if dial.kind in ("int", "float"):
            assert math.isfinite(dial.minimum) and dial.minimum < dial.maximum
            assert not dial.valid(dial.minimum - 1) and not dial.valid(dial.maximum + 1)
        if dial.setting:
            assert getattr(config.Settings(), dial.setting) == dial.default, dial.setting
            assert re.search(rf"pub {dial.setting}:", rust_config), dial.setting
        for attempt in INJECTIONS:
            assert not dial.valid(attempt), (dial.key, attempt)
            assert dial.coerce(attempt) is None, (dial.key, attempt)


def test_settings_write_each_dial_only_off_its_default_and_read_it_back(tmp_path: Path) -> None:
    target = tmp_path / "settings.toml"
    chosen = config.Settings(
        video_fit="fit",
        video_speed=1.55,
        video_brightness=-20,
        video_rotate=270,
        video_deband=True,
        video_scale="ewa_lanczos",
        video_hidden_trigger="max",
        scene_muted=False,
        scene_volume=35,
        scene_particles=False,
        scene_pause_only_active=True,
        scene_automute=False,
    )
    config.save(chosen, target)
    text = target.read_text(encoding="utf-8")

    assert "video_speed = 1.55" in text and 'video_fit = "fit"' in text
    assert "video_contrast" not in text and "scene_mouse" not in text
    assert config.load_strict(target) == chosen
    # Forgiving: a number out of range is brought into it, a choice that isn't one goes default.
    target.write_text(
        'video_speed = 9\nvideo_gamma = -500\nvideo_rotate = 45\nvideo_fit = "zoom"\n',
        encoding="utf-8",
    )
    repaired = config.load(target)
    assert (repaired.video_speed, repaired.video_gamma) == (4.0, -100)
    assert (repaired.video_rotate, repaired.video_fit) == (0, "fill")
    with pytest.raises(config.ConfigError, match='video_fit must be one of "fill", "fit"'):
        config.load_strict(target)
    target.write_text("video_speed = 9\n", encoding="utf-8")
    with pytest.raises(
        config.ConfigError, match=r"video_speed must be a number between 0\.25 and 4"
    ):
        config.load_strict(target)


def test_reset_to_defaults_keeps_a_wallpapers_own_dials_and_every_edit_carries_them(
    tmp_path: Path,
) -> None:
    _settings, session, items = library(tmp_path)
    video = items["video"]
    store = session.pairings
    store.set_dial(video, "video", "rotate", 90)
    store.choose_palette(video, pairings.PalettePolicy())
    store.choose_still(video, None)
    store.choose_mode(video, pairings.Mode.DARK)
    store.mark_borked(video, "it crashed", "mpvpaper")
    store.clear_borked(video)
    assert store.reset(video)

    reopened = pairings.Store.open(paths.app_state_dir() / pairings.STATE_FILENAME)
    record = reopened.get(pairings.Identity.of(video))
    assert record is not None and not record.customized
    assert record.renderer.dial("video", "rotate") == 90
    store.reset_renderer(video)
    assert (
        pairings.Store.open(paths.app_state_dir() / pairings.STATE_FILENAME).get(
            pairings.Identity.of(video)
        )
        is None
    )


def test_a_newer_builds_dials_ride_through_this_ones_saves(tmp_path: Path) -> None:
    _settings, _session, items = library(tmp_path)
    state = paths.app_state_dir() / pairings.STATE_FILENAME
    state.parent.mkdir(parents=True, exist_ok=True)
    video_key = pairings.Identity.of(items["video"]).key
    state.write_text(
        json.dumps(
            {
                "version": 2,
                "pairings": [
                    {
                        "identity": video_key,
                        "palette": "adaptive",
                        "renderer": {
                            "video": {"zoom": 2, "speed": 8.0},
                            "web": {"fps": 10},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = pairings.Store.open(state)

    store.choose_palette(items["video"], pairings.PalettePolicy())
    store.set_dial(items["video"], "video", "gamma", 5)

    saved = json.loads(state.read_text(encoding="utf-8"))["pairings"][0]["renderer"]
    assert saved == {"video": {"speed": 8.0, "gamma": 5, "zoom": 2}, "web": {"fps": 10}}
    # Its own reset goes back to what this build applies; what it can't, it keeps.
    store.reset_renderer(items["video"])
    saved = json.loads(state.read_text(encoding="utf-8"))["pairings"][0]["renderer"]
    assert saved == {"video": {"zoom": 2}, "web": {"fps": 10}}


def test_the_scan_offers_what_a_scene_declares_and_nothing_for_a_web_page(tmp_path: Path) -> None:
    content = tmp_path / "steamapps" / "workshop" / "content" / workshop.APP_ID
    general = {
        "properties": {
            "bloom": {"type": "bool", "text": "Bloom", "value": True, "order": 3},
            "speed": {
                "type": "slider",
                "text": "Speed",
                "value": 1,
                "min": 0,
                "max": 4,
                "step": 0.5,
                "order": 1,
            },
            "tint": {"type": "color", "text": "Tint", "value": "1 0 0", "order": 2},
            "style": {
                "type": "combo",
                "text": "Style",
                "value": "a",
                "order": 4,
                "options": [{"value": "a", "label": "Calm"}, {"value": "b", "label": "Wild"}],
            },
            "motto": {"type": "textinput", "text": "Motto", "value": "hi", "order": 5},
            "picture": {"type": "file", "text": "Picture", "value": "/etc/passwd"},
            "folder": {"type": "directory", "value": "/"},
            "texture": {"type": "scenetexture", "value": "x"},
            "broken": {"type": "slider", "value": 9, "min": 0, "max": 4},
        }
    }
    for workshop_id, project in (
        ("100", {"type": "scene", "title": "Scene", "file": "scene.json", "general": general}),
        ("200", {"type": "web", "title": "Page", "file": "index.html", "general": general}),
        ("300", {"title": "Night", "dependency": "100", "preset": {"bloom": False}}),
        ("400", {"title": "Page preset", "dependency": "200", "preset": {"bloom": False}}),
    ):
        (content / workshop_id).mkdir(parents=True)
        (content / workshop_id / "project.json").write_text(json.dumps(project))
    from wall_in_one.library import scan

    found = {item.scene: item for item in scan.workshop_items((tmp_path,))}

    offered = found["100"].scene_settings
    assert [setting.name for setting in offered] == ["speed", "tint", "bloom", "style", "motto"]
    assert offered[0] == SceneProperty("speed", "slider", "Speed", "1", 0.0, 4.0, 0.5)
    assert offered[1].default == "1.000000 0.000000 0.000000"
    assert offered[3].options == (("a", "Calm"), ("b", "Wild"))
    assert found["300"].scene_settings == offered, "a preset offers its base's"
    assert found["200"].scene_settings == () and found["400"].scene_settings == ()
