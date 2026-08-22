"""The Python client and Rust service must agree without XDG_RUNTIME_DIR."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from wall_in_one import paths


def _config() -> str:
    document = """schema_version = 3
default_playlist = "only"
[settings]
cycle_interval_seconds = 300
cycle_enabled = false
shuffle = false
dynamics_enabled = false
[renderer]
noctalia_program = "/bin/true"
niri_program = "/bin/false"
mpvpaper_program = "/bin/true"
linux_wallpaperengine_program = "/bin/true"
own_scene_renderer = false
layer = "background"
video_when_hidden = "pause"
video_hardware_decode = true
video_interpolation = "off"
video_muted = true
video_volume = 0
scene_fps = 30
scene_muted = true
scene_volume = 0
scene_pause_when_covered = true
scene_scaling = ""
scene_clamp = ""
[[playlists]]
id = "only"
name = "Only"
[[playlists.entries]]
id = "one"
kind = "still"
still = "/tmp/wall-in-one-socket-test.png"
palette = { kind = "keep", mode = "keep" }
"""
    return document.replace(
        '"/bin/true"', json.dumps(os.environ.get("WALL_IN_ONE_TEST_TRUE", "/bin/true"))
    ).replace('"/bin/false"', json.dumps(os.environ.get("WALL_IN_ONE_TEST_FALSE", "/bin/false")))


def test_runtime_socket_fallback_matches_the_rust_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "isolated-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    assert paths.service_runtime_dir() == state / "wall-in-one"
    assert paths.runtime_socket_path() == state / "wall-in-one" / "wall-in-one-runtime.sock"


def test_ctl_status_reaches_rust_without_a_runtime_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_binary = os.environ.get("WALL_IN_ONE_SERVICE_BINARY", "")
    if not configured_binary:
        pytest.skip("set WALL_IN_ONE_SERVICE_BINARY to run the cross-binary integration")
    binary = Path(configured_binary)
    assert binary.is_file(), binary

    root = Path(tempfile.mkdtemp(prefix="wio-socket-", dir="/tmp"))
    state = root / "state"
    app_state = state / "wall-in-one"
    app_state.mkdir(parents=True)
    (app_state / "runtime.toml").write_text(_config(), encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    environment = os.environ.copy()
    source = Path(__file__).resolve().parents[1] / "src"
    previous_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source}{os.pathsep}{previous_pythonpath}" if previous_pythonpath else str(source)
    )
    expected_socket = app_state / "wall-in-one-runtime.sock"
    service = subprocess.Popen(
        [str(binary)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    command = [sys.executable, "-m", "wall_in_one", "ctl"]
    try:
        deadline = time.monotonic() + 5
        while not expected_socket.exists():
            if service.poll() is not None:
                _stdout, stderr = service.communicate(timeout=1)
                raise AssertionError(f"Rust service exited before binding: {stderr}")
            if time.monotonic() >= deadline:
                raise AssertionError(f"Rust service did not bind {expected_socket}")
            time.sleep(0.02)

        result = subprocess.run(
            [*command, "status"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        snapshot = json.loads(result.stdout)
        assert snapshot["playlist_id"] == "only"
        assert snapshot["entry_id"] == "one"

        stopped = subprocess.run(
            [*command, "quit"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert stopped.returncode == 0, stopped.stderr
        assert stopped.stdout.strip() == "quitting"
        assert service.wait(timeout=5) == 0
    finally:
        if service.poll() is None:
            service.terminate()
            try:
                service.wait(timeout=2)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=2)
        shutil.rmtree(root, ignore_errors=True)
