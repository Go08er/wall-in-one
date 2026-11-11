"""Codex review 0.4.2: Classic read-only sub-option (R42-7)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.test_ui_settings_read_only import (  # noqa: F401
    _settings_with_unknown_key,
    make_application,
    toolkit,
)
from wall_in_one import paths
from wall_in_one.ui.app import Application
from wall_in_one.ui.preferences import PreferencesPage

pytestmark = pytest.mark.gui


@pytest.mark.parametrize("web", [True, False], ids=["web-on", "control-web-off"])
def test_classic_preview_suboption_stays_disabled_for_read_only_settings(
    tmp_path: Path,
    make_application: Callable[[], Application],  # noqa: F811
    web: bool,
) -> None:
    before = _settings_with_unknown_key(
        tmp_path, allow_web_wallpapers=web, web_wallpaper_previews=web
    )
    app = make_application()
    page = PreferencesPage(app)
    assert page._read_only_banner.get_revealed()
    assert not page._web.get_sensitive()
    assert paths.settings_path().read_bytes() == before
    assert not page._web_previews.get_sensitive(), (
        "_load re-enabled the preview sub-option after _refresh_read_only disabled all writers"
    )
