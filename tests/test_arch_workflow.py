"""The Arch CI job's shell steps parse.

Each `run: |` block of .github/workflows/arch.yml is a POSIX sh script (the
container's default shell). Several hand a single-quoted script to
`su builder -c`, where one stray apostrophe in a comment ends the quoting
early and the rest of the step runs as something else entirely; only the
first release run on Arch would show it. `sh -n` shows it here.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "arch.yml"


def run_blocks(text: str) -> list[tuple[int, str]]:
    """Every `run: |` block, as (line number, script with its indentation removed)."""
    lines = text.splitlines()
    blocks = []
    for number, line in enumerate(lines):
        found = re.match(r"^(\s*)(?:- )?run: \|\s*$", line)
        if not found:
            continue
        body: list[str] = []
        indent: int | None = None
        for following in lines[number + 1 :]:
            if following.strip() == "":
                body.append("")
                continue
            current = len(following) - len(following.lstrip(" "))
            if indent is None:
                indent = current
            if current < indent:
                break
            body.append(following[indent:])
        blocks.append((number + 1, "\n".join(body).rstrip() + "\n"))
    return blocks


@pytest.mark.skipif(not WORKFLOW.exists(), reason="no Arch workflow in this tree")
def test_every_shell_step_of_the_arch_job_parses() -> None:
    sh = shutil.which("sh")
    assert sh is not None
    blocks = run_blocks(WORKFLOW.read_text(encoding="utf-8"))
    assert len(blocks) >= 5, blocks
    for number, script in blocks:
        result = subprocess.run(
            [sh, "-n"], input=script, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"arch.yml line {number}: {result.stderr}\n{script}"


def test_an_apostrophe_in_a_quoted_su_script_is_caught() -> None:
    sh = shutil.which("sh")
    assert sh is not None
    broken = "su builder -c '\n  # this package's release\n  true\n' \"$GITHUB_WORKSPACE\"\n"
    result = subprocess.run([sh, "-n"], input=broken, capture_output=True, text=True)
    assert result.returncode != 0
