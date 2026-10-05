"""``prototypes/ui-redesign/tools/smoke.sh`` passes on a clean run and fails on a real failure.

Its EXIT trap removed the throwaway profile with a bare ``rm -rf``. A
fontconfig cache written while the last GTK process exited raced it
("Directory not empty"), and under ``set -e`` the failed cleanup made a run
with 0 failures exit 1. Here every tool the script calls is a stand-in (``nix``
and ``xvfb-run`` on ``PATH``): the stand-in smoke run prints its verdict and
leaves a writer filling the profile's cache for a while after it returns.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
SMOKE = REPOSITORY / "prototypes" / "ui-redesign" / "tools" / "smoke.sh"

#: ``nix``: ``build`` prints a directory, ``develop --command`` runs the rest.
NIX: Final = """case "$1" in
  build) echo "$STUB_ICONS" ;;
  develop)
    shift 2
    exec "$@" ;;
esac
"""

#: ``xvfb-run``: the stand-in smoke run. Its verdict comes from the
#: environment; a detached writer keeps (re)creating cache files for a while.
XVFB_RUN: Final = """cache="$XDG_CACHE_HOME/fontconfig"
(
  end=$((SECONDS + 2))
  while ((SECONDS < end)); do
    mkdir -p "$cache" 2>/dev/null && : >"$cache/late-$RANDOM" 2>/dev/null
  done
) </dev/null >/dev/null 2>&1 &
sleep 0.2
echo "SMOKE steps=1 failures=$STUB_FAILURES"
if [[ -n "${STUB_WARNING:-}" ]]; then echo "(smoke.py): Gtk-WARNING **: $STUB_WARNING"; fi
"""


def _run(tmp_path: Path, failures: int, warning: str = "") -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - every supported build has bash
        pytest.skip("no bash")
    # The real script, in a stand-in checkout whose Protocol check only reports
    # success (the real one runs mypy through nix). Shebangs name this bash: a
    # build sandbox has no /usr/bin/env.
    tools = tmp_path / "repo" / "prototypes" / "ui-redesign" / "tools"
    tools.mkdir(parents=True)
    smoke = tools / "smoke.sh"
    shutil.copy2(SMOKE, smoke)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for path, text in (
        (stubs / "nix", NIX),
        (stubs / "xvfb-run", XVFB_RUN),
        (tools / "typecheck.sh", 'echo "Success: no issues found in 1 source file"\n'),
    ):
        path.write_text(f"#!{bash}\n{text}", encoding="utf-8")
        path.chmod(0o755)
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    environment = {
        **os.environ,
        "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
        "TMPDIR": str(temporary),
        "STUB_ICONS": str(tmp_path / "icons"),
        "STUB_FAILURES": str(failures),
        "STUB_WARNING": warning,
    }
    completed = subprocess.run(
        (bash, str(smoke)),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # The detached writer stops by itself within two seconds; nothing outlives the test.
    return completed


def test_a_clean_run_passes_even_while_a_late_writer_fills_the_profile(tmp_path: Path) -> None:
    completed = _run(tmp_path, failures=0)
    assert completed.returncode == 0, completed.stderr
    assert "SMOKE steps=1 failures=0" in completed.stdout
    assert "Directory not empty" not in completed.stderr
    assert not any((tmp_path / "tmp").iterdir()), "the throwaway profile is removed"


def test_a_failure_still_fails(tmp_path: Path) -> None:
    completed = _run(tmp_path, failures=1)
    assert completed.returncode == 1
    assert not any((tmp_path / "tmp").iterdir())


def test_a_gtk_warning_still_fails(tmp_path: Path) -> None:
    completed = _run(tmp_path, failures=0, warning="something broke")
    assert completed.returncode == 1
    assert "GTK warnings above" in completed.stdout
