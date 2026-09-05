"""Opt-in A/B package regressions; nix/packaged-upgrade-test.nix supplies artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.test_deployed_upgrade_incident import (
    _file_snapshot,
    _idempotency_snapshot,
    _incident_profile,
    _tree_snapshot,
)
from wall_in_one import config, deployed_upgrade_transaction, paths

pytestmark = pytest.mark.skipif(
    not all(
        os.environ.get(name)
        for name in (
            "WALL_IN_ONE_OLD_PACKAGE",
            "WALL_IN_ONE_NEW_PACKAGE",
            "WALL_IN_ONE_PACKAGED_PYTHON",
        )
    ),
    reason="requires explicit old/new built packages; run nix/packaged-upgrade-test.nix",
)


@pytest.mark.parametrize(
    "changed_root", [False, True], ids=["same-root-resume", "changed-root-refusal"]
)
def test_old_package_preparation_new_package_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_root: bool
) -> None:
    old = Path(os.environ["WALL_IN_ONE_OLD_PACKAGE"])
    new = Path(os.environ["WALL_IN_ONE_NEW_PACKAGE"])
    python = Path(os.environ["WALL_IN_ONE_PACKAGED_PYTHON"])
    assert old != new and old.is_absolute() and new.is_absolute()
    old_sites = tuple((old / "lib").glob("python*/site-packages"))
    assert len(old_sites) == 1, old_sites
    assert (old_sites[0] / "wall_in_one").is_dir()

    # Shared sanitized fixture data only. Preparation/resume below happen in
    # fresh processes importing the actual old/candidate installed packages.
    profile = _incident_profile(tmp_path, monkeypatch)
    captures = tuple(path for _, path in (*profile.video_captures, *profile.scene_captures))
    captures_before = _file_snapshot(captures)
    motion_before = _tree_snapshot(profile.motionbgs)
    workshop_before = _tree_snapshot(profile.workshop_content)
    marker_before = profile.marker.read_bytes()
    old_runtime = paths.runtime_config_path().read_bytes()

    environment = {
        name: os.environ[name]
        for name in (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR",
        )
    }
    environment.update(
        PATH="/nonexistent",
        LANG="C.UTF-8",
        PYTHONNOUSERSITE="1",
        DBUS_SESSION_BUS_ADDRESS=f"unix:path={tmp_path}/no-session-bus",
        DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={tmp_path}/no-system-bus",
    )
    preparation_environment = {**environment, "PYTHONPATH": str(old_sites[0])}
    preparation = subprocess.run(
        (
            str(python),
            "-P",
            "-c",
            "from wall_in_one import deployed_upgrade_transaction as transaction; "
            f"assert transaction.__file__.startswith({str(old)!r} + '/'); "
            "result = transaction.ensure(cutover=False); "
            "assert result.status == 'prepared', result; print(result.status)",
        ),
        cwd=tmp_path,
        env=preparation_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert preparation.returncode == 0, (preparation.stdout, preparation.stderr)
    assert paths.runtime_config_path().read_bytes() == old_runtime

    chosen_root = profile.root if not changed_root else tmp_path / "different-library"
    replacement = (
        '[theme]\nsource = "wallpaper"\nwallpaper_scheme = "m3-fruit-salad"\n'
        f"[wallpaper]\ndirectory = {json.dumps(str(chosen_root))}\nenabled = true\n"
        "[wallpaper.default]\n"
        f"path = {json.dumps(str(profile.video_captures[1][1]))}\n"
        '[unrelated]\nkeep_this_setting = "changed after preparation"\n'
    ).encode()
    noctalia = paths.noctalia_settings_path()
    temporary = noctalia.with_name(".settings.toml.test-next")
    temporary.write_bytes(replacement)
    os.replace(temporary, noctalia)
    before_status = _idempotency_snapshot(profile)

    def candidate(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (str(new / "bin" / "wall-in-one"), *arguments),
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    status = candidate("--deployed-upgrade-status")
    assert _idempotency_snapshot(profile) == before_status
    resumed = candidate("--write-config")
    if changed_root:
        assert resumed.returncode != 0, (resumed.stdout, resumed.stderr)
        assert status.stdout.startswith("conflict: ")
        assert "wallpaper directory changed" in resumed.stderr
        assert str(profile.root) in resumed.stderr and str(chosen_root) in resumed.stderr
        assert _idempotency_snapshot(profile) == before_status
        assert paths.runtime_config_path().read_bytes() == old_runtime
    else:
        assert status.returncode == 0, (status.stdout, status.stderr)
        assert status.stdout.startswith("prepared: ") and "videos/captures: 48" in status.stdout
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        assert "migrated the deployed profile in place using original root" in resumed.stdout
        assert config.load_strict().roots == (profile.root,)
        assert tomllib.loads(paths.runtime_config_path().read_text())["schema_version"] == 4
        assert deployed_upgrade_transaction.probe().status == "complete"
        completed = _idempotency_snapshot(profile)
        repeated = candidate("--write-config")
        assert repeated.returncode == 0, (repeated.stdout, repeated.stderr)
        assert _idempotency_snapshot(profile) == completed

    assert noctalia.read_bytes() == replacement
    assert _file_snapshot(captures) == captures_before
    assert _tree_snapshot(profile.motionbgs) == motion_before
    assert _tree_snapshot(profile.workshop_content) == workshop_before
    assert profile.marker.read_bytes() == marker_before
    assert not paths.runtime_socket_path().exists()
    assert not paths.socket_path().exists()
