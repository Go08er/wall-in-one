"""R42-1: a publication never reuses a library resolved under the other preview policy.

The saved `web_wallpaper_previews` decides, in the scan, whether a web page's still
is its preview (`Library.previews`). Another writer (a second program, a hand edit)
can move it between a scan and a publication; the publication request is admitted
against the saved sources, so the saved policy is one of them
(`Settings.library_sources`), and the old library is refused until it is rescanned.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

pytestmark = pytest.mark.gui

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_astra030_y1 import (  # noqa: E402, F401
    BOUND,
    Profile,
    _settled,
    profiles,
    toolkit,
)
from wall_in_one import config, paths  # noqa: E402


def test_a_publication_refuses_a_library_scanned_under_the_other_preview_policy(
    profiles: Callable[..., Profile],  # noqa: F811
) -> None:
    profile = profiles()
    application = profile.application
    before = paths.runtime_config_path().read_bytes()
    assert not application.settings.preview_stills
    # Another writer turns web wallpapers and their previews on (a 0.4.1 profile's
    # reading): the library in memory was scanned with previews off.
    saved = replace(application.settings, allow_web_wallpapers=True, web_wallpaper_previews=True)
    config.save(saved)
    assert application._accepted_library_sources != saved.library_sources

    assert application._publish_runtime_async()
    spin_until(lambda: _settled(application), BOUND, what="the publication")

    assert "source settings" in application.publication_problem, application.publication_problem
    assert paths.runtime_config_path().read_bytes() == before, "the old library was published"
