"""Accepted library sources come only from the scan that read them (Z-2's cold review).

A Session a migration replaced reports nothing it scans afterwards. (Z-2b's
other hardening, a removal's own scan recording the sources its worker read,
is now stricter: a scan of sources the settings no longer name is not
installed at all, and the library is scanned again; see
``test_ui_z2c_removal_rescan.py``.) On Y-1's harness (the real application).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_astra030_y1 import Profile, profiles  # noqa: E402

__all__ = ["profiles"]  # Y-1's fixture, used here by name


def test_a_session_a_migration_replaced_records_nothing_it_scans_later(
    profiles: Callable[..., Profile], monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = profiles()
    application = profile.application
    replaced = application.session
    monkeypatch.setattr(replaced, "shutdown", lambda: None)  # so it can still scan
    monkeypatch.setattr(application, "reload_palette", lambda: None)  # not what this is about
    application._replace_session_after_migration(application.settings)
    assert application.session is not replaced
    assert application._accepted_library_sources is None, "the new Session is empty"

    replaced.refresh(mutate_removals=False)  # a stray late refresh on the old one

    assert application._accepted_library_sources is None
