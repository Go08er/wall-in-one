"""``tools/golden-profile-sanitize.py`` on a home shaped like a real one."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

from tests.golden import harness

SANITIZER: Final = Path(__file__).resolve().parents[2] / "tools" / "golden-profile-sanitize.py"


def test_the_sanitizer_round_trips_a_profile_without_its_secrets(
    tmp_path: Path, runtime_dir: Path
) -> None:
    real = harness.materialize(harness.FIXTURE, tmp_path / "real", runtime_dir)
    (real.app_config / "wallhaven-api-key").write_text("SEKRIT-KEY\n")
    before = harness.snapshot(real.home)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("XDG_", "PYTHON", "DBUS_"))
    }
    environment.update(real.environment())
    copy = tmp_path / "copy"
    completed = subprocess.run(
        [sys.executable, str(SANITIZER), str(copy)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert harness.diff(before, harness.snapshot(real.home)) == [], "the sanitizer wrote"

    home = str(real.home).encode()
    leaked = [
        path
        for path in copy.rglob("*")
        if path.is_file() and (b"SEKRIT" in path.read_bytes() or home in path.read_bytes())
    ]
    assert leaked == []
    assert not (copy / "config/wall-in-one/wallhaven-api-key").exists()

    again_runtime = Path(tempfile.mkdtemp(prefix="wio-run-"))
    try:
        again = harness.materialize(copy, tmp_path / "again", again_runtime)
    finally:
        shutil.rmtree(again_runtime, ignore_errors=True)
    # Back to the first sandbox's home, derived names included: every file the
    # app owns must come out byte-identical. The deployed markers are re-sealed
    # for each home (their ids hash the paths) and are checked elsewhere.
    known = {str(path) for path in again.home.rglob("*")}
    back = harness.Relocation(str(again.home), str(real.home), known)
    resealed = {"deployed-capture-adoption-v1.json", "deployed-upgrade-v1.json"}
    compared = 0
    for directory in (real.app_config, real.app_state):
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.name in resealed or path.name == "wallhaven-api-key":
                continue
            relative = path.relative_to(real.home).as_posix()
            twin = again.home / relative
            assert twin.is_file(), f"the copy lost {relative}"
            data = twin.read_bytes()
            text = harness._decoded_text(data)
            copied = back.text(text).encode() if text is not None else data
            assert copied == path.read_bytes(), relative
            compared += 1
    assert compared >= 20
    original_library = sorted(
        path.relative_to(real.home).as_posix() for path in (real.home / "Pictures").rglob("*")
    )
    copied_library = sorted(
        back.name(path.relative_to(again.home).as_posix())
        for path in (again.home / "Pictures").rglob("*")
    )
    assert copied_library == original_library
