"""How the test instructions say to run the suite, and the wrapper they name.

tests/conftest.py's guard is a backstop: an audit hook sees only its own
Python process, and the tripwire notices a change after it happened. So no
instruction may present a bare ``pytest`` from a developer shell as safe, and
``tools/isolated.sh``, which they name instead, must really isolate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import conftest

REPOSITORY = Path(__file__).resolve().parents[1]
WRAPPER = REPOSITORY / "tools" / "isolated.sh"


def _development_commands() -> list[str]:
    readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Development", 1)[1].split("\n## ", 1)[0]
    return [
        line.removeprefix("$ ").strip()
        for block in re.findall(r"```console\n(.*?)```", section, re.DOTALL)
        for line in block.splitlines()
        if line.startswith("$ ")
    ]


def test_the_readme_runs_every_test_through_the_wrapper() -> None:
    commands = [command for command in _development_commands() if "pytest" in command]
    assert commands, "the Development section shows how to run the tests"
    for command in commands:
        assert command.startswith("tools/isolated.sh "), command
    readme = " ".join((REPOSITORY / "README.md").read_text(encoding="utf-8").split())
    assert "never as a bare `pytest`" in readme
    assert "That guard is a backstop, not a substitute" in readme


def test_the_guard_says_it_is_a_backstop() -> None:
    assert conftest.__doc__ is not None
    doc = " ".join(conftest.__doc__.split())
    assert "All of this is a backstop, not the way to run the suite." in doc
    assert "``tools/isolated.sh``" in doc and "never as a bare ``pytest``" in doc
    downgrade = (REPOSITORY / "tools" / "golden-downgrade.sh").read_text(encoding="utf-8")
    assert "tools/isolated.sh tools/golden-downgrade.sh" in downgrade
    assert "nix develop --command tools/golden-downgrade.sh" not in downgrade


def test_the_wrapper_runs_the_command_in_a_throwaway_profile(tmp_path: Path) -> None:
    """Started from a stand-in desktop shell, inside a Nix shell, with a stand-in
    Xvfb that only runs its command: the command sees none of that shell."""
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - every supported build has bash
        pytest.skip("no bash")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    xvfb_run = stubs / "xvfb-run"
    xvfb_run.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "arguments = sys.argv[1:]\n"
        "while arguments and arguments[0].startswith('-'):\n"
        "    arguments.pop(0)\n"
        "os.execvp(arguments[0], arguments)\n",
        encoding="utf-8",
    )
    xvfb_run.chmod(0o755)
    shell = tmp_path / "desktop"
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in (conftest.SESSION_ROOT_ENV, "WIO_TEST_SESSION_PID")
    }
    environment.update(
        HOME=str(shell / "home"),
        XDG_CONFIG_HOME=str(shell / "home" / ".config"),
        XDG_STATE_HOME=str(shell / "home" / ".local" / "state"),
        XDG_CACHE_HOME=str(shell / "home" / ".cache"),
        XDG_DATA_HOME=str(shell / "home" / ".local" / "share"),
        XDG_RUNTIME_DIR=str(shell / "run"),
        DBUS_SESSION_BUS_ADDRESS=f"unix:path={shell / 'run' / 'bus'}",
        DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={shell / 'system_bus_socket'}",
        WAYLAND_DISPLAY="wayland-1",
        NIRI_SOCKET=str(shell / "run" / "niri.sock"),
        DISPLAY=":0",
        LD_LIBRARY_PATH=str(shell / "lib"),
        IN_NIX_SHELL="impure",
        TMPDIR=str(temporary),
        PATH=f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
    )

    completed = subprocess.run(
        (
            bash,
            str(WRAPPER),
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    seen = json.loads(completed.stdout.splitlines()[-1])
    root = Path(seen["HOME"]).parent
    assert root.parent == temporary and root.name.startswith("wio-isolated."), root
    layout = {
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_STATE_HOME": "state",
        "XDG_CACHE_HOME": "cache",
        "XDG_DATA_HOME": "data",
        "XDG_RUNTIME_DIR": "run",
    }
    for variable, directory in layout.items():
        assert seen[variable] == str(root / directory), variable
    assert seen["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={root / 'run' / 'no-session-bus'}"
    assert seen["DBUS_SYSTEM_BUS_ADDRESS"] == f"unix:path={root / 'run' / 'no-system-bus'}"
    for variable in ("WAYLAND_DISPLAY", "NIRI_SOCKET", "DISPLAY", "LD_LIBRARY_PATH"):
        assert variable not in seen, variable
    assert conftest.SESSION_ROOT_ENV not in seen
    for variable, value in {
        "GDK_DEBUG": "no-portals",
        "GTK_USE_PORTAL": "0",
        "GSK_RENDERER": "cairo",
        "GDK_BACKEND": "x11",
    }.items():
        assert seen[variable] == value, variable
    assert not root.exists(), "the throwaway profile is removed afterwards"
    assert not shell.exists(), "nothing was written in the stand-in shell's directories"
