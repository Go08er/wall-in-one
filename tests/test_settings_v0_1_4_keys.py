"""settings.toml stays readable by 0.1.4, the only release anyone rolls back to.

There is no 0.1.5: people go from 0.1.4 straight to 0.2.0 and back. v0.1.4's
strict settings loader refuses a key it does not know, and its service then
cannot use the file at all (``tests/golden/test_downgrade.py`` pins that
against v0.1.4 itself). So no writer in this build may emit a key v0.1.4
lacks.

Every settings writer -- ``config.save``, ``config.mutate`` and
``config.update``, ``config.forget_playlist_default``, the deployed-upgrade
transaction and the legacy migration -- serializes through
``Settings.to_toml``, so pinning ``Settings`` and that serializer pins them
all. This needs no old build; it runs with every unit test.
"""

from __future__ import annotations

import tomllib
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Final

import pytest

from wall_in_one import config

#: Every key v0.1.4 writes and its strict loader accepts: the fields of
#: ``Settings`` at tag v0.1.4 (``dbfbaa0``), ``src/wall_in_one/config.py``
#: (``git show v0.1.4:src/wall_in_one/config.py``), which ``to_toml`` there
#: writes, ``stop_animations_on_battery`` only when it is on. Frozen with
#: that release: this set never changes.
V0_1_4_SETTINGS_KEYS: Final = frozenset(
    {
        "active_playlist",
        "cycle_enabled",
        "cycle_favourites_only",
        "cycle_interval",
        "display_mode",
        "dynamics_enabled",
        "follow_noctalia_palette",
        "opacity",
        "output",
        "own_scene_renderer",
        "preview_scheme",
        "roots",
        "scan_workshop",
        "scene_clamp",
        "scene_fps",
        "scene_scaling",
        "shuffle",
        "stop_animations_on_battery",
        "theme_source_connector",
        "video_hardware_decode",
        "video_interpolation",
        "video_muted",
        "video_volume",
        "video_when_hidden",
    }
)
#: The one key v0.1.4's writer leaves out at its default.
V0_1_4_OMITTED_AT_DEFAULT: Final = frozenset({"stop_animations_on_battery"})

#: A value away from the default for every setting.
AWAY_FROM_DEFAULT: Final[dict[str, Any]] = {
    "opacity": 0.45,
    "preview_scheme": "m3-content",
    "follow_noctalia_palette": False,
    "cycle_interval": 4321,
    "cycle_enabled": True,
    "shuffle": True,
    "dynamics_enabled": False,
    "stop_animations_on_battery": True,
    "video_muted": False,
    "video_volume": 37,
    "video_when_hidden": "stop",
    "video_interpolation": "linear",
    "video_hardware_decode": False,
    "scene_fps": 24,
    "scene_scaling": "fill",
    "scene_clamp": "border",
    "display_mode": config.DISPLAY_MODE_INDEPENDENT,
    "theme_source_connector": "DP-1",
    "output": "DP-1",
    "own_scene_renderer": False,
    "scan_workshop": False,
    "active_playlist": "Evenings",
    "cycle_favourites_only": True,
    "roots": (Path("/srv/wallpapers"), Path("/home/someone/Pictures")),
}


def _written_keys(settings: config.Settings) -> frozenset[str]:
    return frozenset(tomllib.loads(settings.validated().to_toml()))


def test_settings_has_exactly_the_v0_1_4_keys() -> None:
    """Adding a field to ``Settings`` would add a key 0.1.4 cannot read."""
    assert frozenset(field.name for field in fields(config.Settings)) == V0_1_4_SETTINGS_KEYS
    assert config.KNOWN_KEYS == V0_1_4_SETTINGS_KEYS


def test_every_setting_is_away_from_its_default_below() -> None:
    """The cases below change every field, each one valid as written."""
    defaults = config.Settings()
    assert set(AWAY_FROM_DEFAULT) == V0_1_4_SETTINGS_KEYS
    changed = replace(defaults, **AWAY_FROM_DEFAULT)
    assert changed.validated() == changed
    for field in fields(config.Settings):
        assert getattr(changed, field.name) != getattr(defaults, field.name), field.name


def _cases() -> list[Any]:
    defaults = config.Settings()
    cases = [
        pytest.param(defaults, id="defaults"),
        pytest.param(replace(defaults, **AWAY_FROM_DEFAULT), id="every-setting-changed"),
    ]
    for key, value in AWAY_FROM_DEFAULT.items():
        changes = {key: value}
        if key == "display_mode":
            changes["theme_source_connector"] = AWAY_FROM_DEFAULT["theme_source_connector"]
        cases.append(pytest.param(replace(defaults, **changes), id=f"only-{key}"))
    return cases


@pytest.mark.parametrize("settings", _cases())
def test_the_writer_emits_only_v0_1_4_keys(settings: config.Settings) -> None:
    """Whatever is saved, every key written is one v0.1.4 knows, and as v0.1.4
    does, every key is written but the battery option at its default."""
    written = _written_keys(settings)

    assert written <= V0_1_4_SETTINGS_KEYS, sorted(written - V0_1_4_SETTINGS_KEYS)
    expected = V0_1_4_SETTINGS_KEYS - (
        frozenset() if settings.stop_animations_on_battery else V0_1_4_OMITTED_AT_DEFAULT
    )
    assert written == expected


def test_a_saved_file_has_only_v0_1_4_keys(tmp_path: Path) -> None:
    """The same through ``config.save`` and ``config.update`` on disk."""
    target = tmp_path / "settings.toml"
    config.save(replace(config.Settings(), **AWAY_FROM_DEFAULT), target)
    assert frozenset(tomllib.loads(target.read_text())) == V0_1_4_SETTINGS_KEYS

    config.update({"stop_animations_on_battery": False, "opacity": 0.8}, target)
    assert frozenset(tomllib.loads(target.read_text())) == (
        V0_1_4_SETTINGS_KEYS - V0_1_4_OMITTED_AT_DEFAULT
    )
    with pytest.raises(config.ConfigError, match="unknown setting"):
        config.update({"ui_glass_frost": 0.3}, target)
