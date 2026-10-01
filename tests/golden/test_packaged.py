"""The ``golden-profile`` flake check must exercise the built package."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest

PACKAGE_ENV: Final = "WIO_GOLDEN_EXPECT_PACKAGE"


@pytest.mark.skipif(
    not os.environ.get(PACKAGE_ENV), reason=f"{PACKAGE_ENV} is set by the Nix check"
)
def test_the_golden_profile_runs_against_the_installed_package() -> None:
    """Not this checkout's ``src``: the site-packages the Nix build installed."""
    import wall_in_one

    module = Path(wall_in_one.__file__).resolve()
    assert "site-packages" in module.parts, module
    assert module.is_relative_to(Path("/nix/store")), module
    assert not module.is_relative_to(Path(__file__).resolve().parents[2] / "src"), module
