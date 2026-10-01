"""Fixtures for the golden-profile tests; see :mod:`tests.golden.sandbox`."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.golden import harness, sandbox
from tests.golden.sandbox import Golden


@pytest.fixture
def runtime_dir() -> Iterator[Path]:
    """A private ``XDG_RUNTIME_DIR`` short enough for Unix socket names."""
    path = Path(tempfile.mkdtemp(prefix="wio-run-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def golden(tmp_path: Path, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Golden:
    """The committed synthetic profile, sealed."""
    return sandbox.enter(harness.FIXTURE, tmp_path / "sandbox", runtime_dir, monkeypatch)


@pytest.fixture(params=sandbox.sources())
def any_golden(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Golden:
    """The synthetic profile, and the ``WIO_GOLDEN_PROFILE`` copy when set."""
    source = request.param
    assert isinstance(source, Path)
    if not (source / harness.MANIFEST).is_file():
        pytest.fail(f"{sandbox.LOCAL_PROFILE_ENV}={source} has no {harness.MANIFEST}")
    return sandbox.enter(source, tmp_path / "sandbox", runtime_dir, monkeypatch)
