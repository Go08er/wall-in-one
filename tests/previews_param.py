"""Run the web-off lane tests with the previews sub-option on and off (S3-1).

An upgraded 0.4.1 profile reads `web_wallpaper_previews` as on, so turning Allow web
wallpapers off also moves `Settings.preview_stills`, and with it the library sources.
The web-off contract (it reaches the service at once, by every lane) must not depend
on that. A module that imports `_previews_param` runs each of its tests both ways; its
web-on setups build their settings with `web_on`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from wall_in_one import config

CURRENT = [False]


@pytest.fixture(params=[False, True], ids=["previews-off", "previews-on"], autouse=True)
def _previews_param(request: pytest.FixtureRequest) -> Iterator[None]:
    CURRENT[0] = bool(request.param)
    yield
    CURRENT[0] = False


def web_on(roots: tuple[Path, ...]) -> config.Settings:
    """Web wallpapers on, with the sub-option as this run has it."""
    return config.Settings(
        roots=roots, allow_web_wallpapers=True, web_wallpaper_previews=CURRENT[0]
    )
