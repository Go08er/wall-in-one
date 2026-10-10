"""The vendored released modules are the released ones, and stand alone."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests import released
from tests.released import released_source  # noqa: F401 -- a fixture


def test_the_vendored_modules_are_byte_for_byte_the_released_ones() -> None:
    """Each ``.py.frozen`` equals ``git show 1840dde:src/<path>``.

    Checked wherever git has that commit; the gates run from a copy without
    history, where there is nothing to compare against.
    """
    repository = Path(__file__).resolve().parents[1]
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available here")
    known = subprocess.run(
        [git, "-C", str(repository), "cat-file", "-e", f"{released.RELEASE}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    if known.returncode != 0:
        pytest.skip(f"this copy has no {released.RELEASE} to compare against")

    frozen = released.frozen_files()
    assert frozen, "nothing is vendored"
    for relative in frozen:
        path = f"src/{str(relative).removesuffix('.frozen')}"
        shown = subprocess.run(
            [git, "-C", str(repository), "show", f"{released.RELEASE}:{path}"],
            check=True,
            capture_output=True,
        ).stdout
        assert (released.FROZEN / relative).read_bytes() == shown, path


def test_the_released_package_runs_on_its_own(released_source: Path) -> None:  # noqa: F811
    """The child's ``wall_in_one`` is the released one, and its closure is complete."""
    done = released.run(
        released_source,
        "from wall_in_one.providers import download, http\n"
        "print(download.MEDIA_STAGING_PREFIX, http.STAGING_PREFIX)\n",
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == [".wall-in-one-media-stage-", ".wall-in-one-media-stage-"]
