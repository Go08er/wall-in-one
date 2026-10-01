"""A damaged existing profile is repairable, never silently reset or bypassed."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from tests.test_deployed_upgrade import _profile
from wall_in_one import cli, config, deployed_upgrade, deployed_upgrade_transaction, paths
from wall_in_one.library import stills

DAMAGED = pytest.mark.parametrize(
    "damaged",
    [
        b"roots = [broken",
        b'roots = ["~wio_nonexistent_user_20260905/Pictures"]\n',
        b'roots = ["~bad\\u0000/Pictures"]\n',
        b"opacity = " + b"1" + b"0" * 400 + b"\n",
    ],
    ids=[
        "malformed-toml",
        "missing-home-account",
        "invalid-home-account",
        "oversized-opacity",
    ],
)

#: ``upgraded`` is a deployed install after its upgrade completed. Its marker
#: answers the upgrade probe without reading settings.toml, so it must not be
#: what decides whether a damaged document is refused.
PROFILES = pytest.mark.parametrize("profile", ["ordinary", "upgraded"])


def _existing_profile(
    profile: Literal["ordinary", "upgraded"],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Save a valid profile and return its library root."""
    if profile == "ordinary":
        root = tmp_path / "Original library"
        root.mkdir()
        config.save(config.Settings(roots=(root,), scan_workshop=False))
        runtime = paths.runtime_config_path()
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_bytes(b"schema_version = 2\n")
        return root

    def no_decoder(*_arguments: object, **_keywords: object) -> None:
        raise stills.StillError("no media decoder in this test")

    monkeypatch.setattr(stills, "generate", no_decoder)
    upgraded = _profile(tmp_path, monkeypatch)
    assert deployed_upgrade_transaction.ensure().status == "complete"
    assert deployed_upgrade.completion_path().is_file()
    return upgraded.root


@PROFILES
@DAMAGED
def test_graphical_recovery_retains_unreadable_settings_until_user_repairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: Literal["ordinary", "upgraded"],
    damaged: bytes,
) -> None:
    root = _existing_profile(profile, tmp_path, monkeypatch)
    original = paths.settings_path().read_bytes()
    paths.settings_path().write_bytes(damaged)
    runtime = paths.runtime_config_path()
    runtime_before = runtime.read_bytes()
    messages: list[str] = []

    with pytest.raises(config.ConfigError):
        config.load_strict()

    def cancel(message: str) -> bool:
        messages.append(message)
        assert paths.settings_path().read_bytes() == damaged
        assert runtime.read_bytes() == runtime_before
        return False

    assert cli._run_graphical_startup_upgrade(require_legacy_safe=False, retry=cancel) == 78
    assert messages and "settings" in messages[0]
    assert paths.settings_path().read_bytes() == damaged
    assert runtime.read_bytes() == runtime_before

    def repair(_message: str) -> bool:
        # A user edit through their editor, not an automatic default/reset.
        paths.settings_path().write_bytes(original)
        return True

    assert cli._run_graphical_startup_upgrade(require_legacy_safe=False, retry=repair) is None
    assert paths.settings_path().read_bytes() == original
    assert config.load_strict().roots == (root,)
    if profile == "ordinary":
        # The upgraded profile's capture housekeeping may legitimately
        # recompile its runtime once the settings are valid again.
        assert runtime.read_bytes() == runtime_before


@PROFILES
@DAMAGED
def test_headless_failure_does_not_open_recovery_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: Literal["ordinary", "upgraded"],
    damaged: bytes,
) -> None:
    _existing_profile(profile, tmp_path, monkeypatch)
    settings = paths.settings_path()
    settings.write_bytes(damaged)
    runtime = paths.runtime_config_path()
    runtime_before = runtime.read_bytes()

    def no_window(_message: str) -> bool:
        pytest.fail("Headless startup attempted an interactive repair window")

    assert cli._run_graphical_startup_upgrade(require_legacy_safe=True, retry=no_window) == 78
    # Not softened into keeping the last-known-good runtime: the service
    # unit treats 78 as a configuration error and does not restart.
    assert cli.main(["--service-startup-prepare"]) == 78
    assert cli.main(["--write-config"]) == 78
    assert settings.read_bytes() == damaged
    assert runtime.read_bytes() == runtime_before


@pytest.mark.parametrize(
    "runtime_bytes", [b"schema_version = 2\n", b"not valid TOML", b"schema_version = 999\n"]
)
def test_old_or_broken_runtime_does_not_prevent_graphical_settings_access(
    tmp_path: Path, runtime_bytes: bytes
) -> None:
    media = tmp_path / "Existing wallpapers"
    media.mkdir()
    config.save(config.Settings(roots=(media,), scan_workshop=False))
    runtime = paths.runtime_config_path()
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(runtime_bytes)
    assert cli._run_graphical_startup_upgrade(require_legacy_safe=False) is None
    # Ordinary settings authoring remains available without a running Rust
    # service; correcting a preference must not depend on that service starting.
    config.update({"cycle_interval": 37})
    assert config.load_strict().cycle_interval == 37
    assert config.load_strict().roots == (media,)
    assert runtime.read_bytes() == runtime_bytes
