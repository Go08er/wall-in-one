"""The released version (0.2.2, ``1840dde``) run for real, in a child process.

An invariant that holds for what this checkout's code produces does not hold
for what the released code already left on disk (review rule 4). So a test
that depends on one runs the released code itself as the *producer*, never a
hand-made filename, and then runs this checkout's code on what it left.
Updates are one-way: the released code is never the consumer of what this
checkout wrote.

The released modules are vendored byte for byte under
``tests/fixtures/released_0_2_2/`` as ``<path>.py.frozen``, mirroring ``src/``.
They are fixtures only: nothing in ``src/`` imports them, the package is built
from ``src/`` alone, and the ``.frozen`` suffix keeps them out of pytest
collection, ruff and mypy. ``tests/test_released_fixture.py`` checks each one
against ``git show 1840dde:src/<path>`` wherever git has that commit (the gates
run from a store copy without one). ``WIO_OLD_SRC_0_2_2``, the ``src`` of a real
0.2.2 checkout, replaces the vendored copy.

What is vendored is the import closure of ``wall_in_one.providers.download``
and ``wall_in_one.providers.http``; of the state-file stores
(``library.state_file``, ``playlists``, ``displays``, ``schedules``,
``favourites``, ``pairings``, and ``ui_prefs``); of ``library.stills``; and of
``theme.template``, with the palette template it installs
(``data/palette.json.tmpl.frozen``: any file is vendored the same way); and of
the startup paths: ``session``, ``library.capture_upgrade`` (with
``library.adopted``), ``library.manage`` and ``library.removals``, and
``legacy_migration``. To probe another module, freeze it and whatever it
imports::

    git show 1840dde:src/wall_in_one/<module>.py \\
        > tests/fixtures/released_0_2_2/wall_in_one/<module>.py.frozen

`tests.released_faults` runs such a child under one fault at a time.

A child that imports a module nobody froze fails with ``ModuleNotFoundError``:
its only ``wall_in_one`` is the released one, never this checkout's.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

#: The released commit, and the version it shipped as.
RELEASE: Final = "1840dde"
VERSION: Final = "0.2.2"
#: The vendored modules, each ``<path under src>.frozen``.
FROZEN: Final = Path(__file__).parent / "fixtures" / "released_0_2_2"
#: A real checkout's ``src`` to use instead of the vendored modules.
OVERRIDE: Final = "WIO_OLD_SRC_0_2_2"
_SUFFIX: Final = ".frozen"

#: Run first in every child: prove the package it imports is the released one.
_PREAMBLE: Final = """\
import sys
from pathlib import Path

import wall_in_one

_released = Path(sys.argv.pop(1)).resolve()
if not Path(wall_in_one.__file__).resolve().is_relative_to(_released):
    raise SystemExit(f"not the released package: {wall_in_one.__file__}")
"""


def frozen_files() -> list[Path]:
    """Every vendored module, as its path under `FROZEN`."""
    return sorted(path.relative_to(FROZEN) for path in FROZEN.rglob(f"*{_SUFFIX}"))


def materialise(destination: Path) -> Path:
    """The released package as an importable tree; returns what goes on ``PYTHONPATH``."""
    override = os.environ.get(OVERRIDE)
    if override:
        return Path(override)
    for relative in frozen_files():
        target = destination / relative.with_name(relative.name.removesuffix(_SUFFIX))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((FROZEN / relative).read_bytes())
    return destination


@pytest.fixture(scope="session")
def released_source(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The released package, materialised once per session."""
    yield materialise(tmp_path_factory.mktemp(f"released-{VERSION}"))


def _child(
    source: Path, script: str, arguments: tuple[str, ...]
) -> tuple[list[str], dict[str, str]]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return [sys.executable, "-c", _PREAMBLE + script, str(source), *arguments], environment


def run(
    source: Path, script: str, *arguments: str, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a child whose only ``wall_in_one`` is the released one.

    The child inherits this process's environment (so the test's scratch XDG
    directories), with ``PYTHONPATH`` replaced by ``source``. ``arguments``
    arrive as ``sys.argv[1:]``.
    """
    command, environment = _child(source, script, arguments)
    return subprocess.run(
        command,
        cwd=source,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def start(source: Path, script: str, *arguments: str) -> subprocess.Popen[str]:
    """Like `run`, but the child keeps running: its stdin and stdout are pipes.

    For a test that needs the released code to be in the middle of something
    (holding a lease) while this checkout acts. The caller ends it.
    """
    command, environment = _child(source, script, arguments)
    return subprocess.Popen(
        command,
        cwd=source,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
