"""Self-check of vm_tool's Noctalia accounting, run when the VM test is built.

Snapshots a synthetic home holding the golden profile's Noctalia settings,
changes it the ways the VM can, and checks that what the test driver does with
two snapshots -- ``harness.diff`` then ``vm_tool.shell_writes`` -- lets the
shell's own field through and fails anything else. Usage:
``vm_tool_selfcheck.py PROFILE_DIR`` with harness and vm_tool importable.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import harness
import vm_tool


def snap(home: Path) -> dict[str, Any]:
    return vm_tool.nodes(json.loads(json.dumps(vm_tool.snapshot(home))))


def judge(
    home: Path, before: dict[str, Any], *, first_start: bool = False
) -> tuple[list[str], list[str]]:
    changes = harness.diff(before, snap(home))
    rest, problems = vm_tool.shell_writes(changes, first_start=first_start)
    return [change.describe() for change in rest], problems


def problem(home: Path, before: dict[str, Any], *, first_start: bool = False) -> str:
    """The one finding a change must produce."""
    rest, problems = judge(home, before, first_start=first_start)
    assert len(problems) == 1, f"expected one finding, got {problems} (and {rest})"
    return problems[0]


def main(profile: Path) -> None:
    original = (profile / "state/noctalia/settings.toml").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(scratch) / "home"
        settings = home / vm_tool.NOCTALIA_SETTINGS
        settings.parent.mkdir(parents=True)
        settings.write_text(original, encoding="utf-8")
        settings.chmod(0o600)
        state = settings.with_name("state.toml")
        state.write_text("x = 1\n", encoding="utf-8")
        state.chmod(0o600)
        before = snap(home)
        assert vm_tool.NOCTALIA_SETTINGS in before, "Noctalia's settings are not snapshotted"

        assert judge(home, before) == ([], []), "an untouched home reported writes"

        # The shell's own field: allowed, and set aside.
        last = 'path = "/home/user/Pictures/Wallpapers/harbour-dusk.png"'
        assert original.count(last) == 1
        settings.write_text(original.replace(last, 'path = "/elsewhere.png"'), encoding="utf-8")
        assert judge(home, before) == ([], []), "a shell-owned field change was not accepted"

        # What Noctalia writes on applying a wallpaper, tables included.
        applied = original + '\n[wallpaper.default]\npath = "/a.png"\n'
        applied += '\n[wallpaper.monitors.winit]\npath = "/a.png"\n'
        settings.write_text(applied, encoding="utf-8")
        assert judge(home, before) == ([], []), "Noctalia's wallpaper record was refused"
        # ... but nothing else in those tables.
        settings.write_text(applied + 'fill = "crop"\n', encoding="utf-8")
        assert "wallpaper.monitors.winit.fill" in problem(home, before)

        # Noctalia's one-time migration: allowed across its first start only.
        migrated = original.replace("config_version = 3", "config_version = 14")
        migrated += '\n[lockscreen_widgets]\nwidget_order = ["login-box@winit"]\n'
        migrated += '\n[lockscreen_widgets.grid]\ncell_size = 16\n'
        settings.write_text(migrated, encoding="utf-8")
        assert judge(home, before, first_start=True) == ([], [])
        found = problem(home, before)
        assert "config_version" in found and "lockscreen_widgets" in found, found

        # An unrelated key, as an app write would change it: a finding.
        mode = 'mode = "dark"'
        assert original.count(mode) == 1
        settings.write_text(original.replace(mode, 'mode = "light"'), encoding="utf-8")
        for first_start in (False, True):
            rest, problems = judge(home, before, first_start=first_start)
            assert rest == [] and len(problems) == 1, problems
            assert problems[0].endswith("changed theme.mode"), problems

        # An array value counts like any other.
        builtin = "builtin_ids = []"
        assert original.count(builtin) == 1
        settings.write_text(original.replace(builtin, 'builtin_ids = ["gtk"]'), encoding="utf-8")
        assert problem(home, before).endswith("changed theme.templates.builtin_ids")

        # Both at once: the allowed field does not hide the unrelated one.
        both = original.replace(last, 'path = "/elsewhere.png"').replace(mode, 'mode = "light"')
        settings.write_text(both, encoding="utf-8")
        assert "theme.mode" in problem(home, before)

        # A key added, and the file removed: findings too.
        settings.write_text(original + '\n[x-app]\nwritten = true\n', encoding="utf-8")
        assert "x-app" in problem(home, before)
        settings.unlink()
        assert "deleted" in problem(home, before)

        # Anything else below Noctalia's trees stays for the phase to judge.
        settings.write_text(original, encoding="utf-8")
        settings.chmod(0o600)
        backup = settings.parent / "settings.toml.bak-wall-in-one-x"
        backup.write_text(original)
        rest, problems = judge(home, before)
        assert problems == [] and len(rest) == 1 and "bak-wall-in-one-x" in rest[0], rest
        backup.unlink()

        # A chmod never rides along with an allowed write: not with the
        # shell's own field ...
        chmodded = f"mode: {vm_tool.NOCTALIA_SETTINGS} (600 -> 666)"
        settings.write_text(original.replace(last, 'path = "/elsewhere.png"'), encoding="utf-8")
        settings.chmod(0o666)
        assert problem(home, before) == chmodded

        # ... nor with a byte-identical rewrite, which alone is the shell's.
        def rewrite(mode: int) -> None:
            replacement = settings.with_name(".settings.toml.tmp")
            replacement.write_text(original, encoding="utf-8")
            replacement.chmod(mode)
            os.replace(replacement, settings)

        rewrite(0o666)
        assert problem(home, before) == chmodded
        rewrite(0o600)
        assert judge(home, before) == ([], []), "the shell's own rewrite was refused"

        # A shell-owned file's writes are set aside; its chmod is not.
        state.write_text("x = 2\n", encoding="utf-8")
        assert judge(home, before) == ([], [])
        state.chmod(0o666)
        rest, problems = judge(home, before)
        assert problems == [] and rest == [
            "mode: .local/state/noctalia/state.toml (600 -> 666)"
        ], (rest, problems)
    print("vm_tool self-check: Noctalia settings are accounted for field by field and mode")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
