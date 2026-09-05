"""Battery policy settings remain opt-in and preserve old upgrade documents."""

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.session import Session
from wall_in_one.ui import runtime_truth


def test_battery_option_round_trip_preserves_manual_animation_preference(tmp_path: Path) -> None:
    settings = config.Settings(stop_animations_on_battery=True, dynamics_enabled=False)
    path = tmp_path / "settings.toml"
    config.save(settings, path)
    restored = config.load_strict(path)
    assert restored.stop_animations_on_battery is True
    assert restored.dynamics_enabled is False

    config.save(replace(restored, stop_animations_on_battery=False), path)
    assert config.load_strict(path).stop_animations_on_battery is False
    assert "stop_animations_on_battery" not in path.read_text()


@pytest.mark.parametrize("value", ['"false"', "1", "[]"])
def test_unattended_battery_option_refuses_non_boolean(tmp_path: Path, value: str) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(f"stop_animations_on_battery = {value}\n")
    with pytest.raises(config.ConfigError, match="stop_animations_on_battery must be a boolean"):
        config.load_strict(path)
    assert config.load(path).stop_animations_on_battery is False


def test_compiler_uses_new_schema_only_for_enabled_battery_policy(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "wallpaper.png").write_bytes(b"fixture")
    settings = config.Settings(roots=(root,), scan_workshop=False)
    session = Session(settings)
    original = runtime_config.render(settings, session)
    document = tomllib.loads(original)
    assert document["schema_version"] == 4
    assert "stop_animations_on_battery" not in document["settings"]
    assert "stop_animations_on_battery" not in settings.to_toml()

    enabled = replace(settings, stop_animations_on_battery=True)
    updated = tomllib.loads(runtime_config.render(enabled, session))
    assert updated["schema_version"] == 5
    assert updated["settings"]["stop_animations_on_battery"] is True
    assert updated["settings"]["dynamics_enabled"] is True
    assert updated["config_generation"] != document["config_generation"]
    assert (
        runtime_config.render(replace(enabled, stop_animations_on_battery=False), session)
        == original
    )


def _power_status(**changes: object) -> dict[str, object]:
    return {
        "playlist_id": "evening",
        "playlist": "Evening",
        "source": "manual",
        "playback_state": "stopped",
        "power_source": "battery",
        "power_available": True,
        "stop_animations_on_battery": True,
        "animations_inhibited": True,
        "animation_inhibition_reason": "battery",
        **changes,
    }


def test_battery_truth_does_not_replace_manual_playback_intent() -> None:
    status = _power_status()
    truth = runtime_truth.from_status(status)
    assert truth is not None and truth.power is not None
    assert truth.is_manual
    assert status["playback_state"] == "stopped"
    assert truth.power.message == "Animations stopped on battery"


def test_missing_power_provider_distinguishes_a_retained_restriction() -> None:
    held = runtime_truth.power_from_status(
        _power_status(
            power_source="unknown",
            power_available=False,
            animation_inhibition_reason="power-unavailable",
        )
    )
    assert held is not None and held.inhibited
    assert "battery restriction retained" in held.message
    fresh = runtime_truth.power_from_status(
        _power_status(
            power_source="unknown",
            power_available=False,
            animations_inhibited=False,
            animation_inhibition_reason="",
        )
    )
    assert fresh is not None and not fresh.inhibited
    assert fresh.message == "Power information unavailable"


@pytest.mark.parametrize(
    "changes",
    [
        {"power_source": "mains"},
        {"power_available": "true"},
        {"power_available": False},
        {"animations_inhibited": "false"},
        {"stop_animations_on_battery": False},
        {"power_source": "ac"},
        {"animation_inhibition_reason": ""},
        {"animation_inhibition_reason": "power-unavailable"},
        {"animations_inhibited": False, "animation_inhibition_reason": ""},
    ],
)
def test_inconsistent_power_truth_is_not_published(changes: dict[str, object]) -> None:
    assert runtime_truth.from_status(_power_status(**changes)) is None


def test_old_runtime_has_no_battery_claim() -> None:
    status = {"playlist_id": "old", "playlist": "Old", "source": "schedule"}
    truth = runtime_truth.from_status(status)
    assert truth is not None and truth.power is None
