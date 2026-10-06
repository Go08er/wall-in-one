"""Accepted library sources come only from the scan that read them (Z-2's cold review).

Two hardenings, neither reachable while the authoring lane runs one thing at
a time, both cheap: a removal's own scan records the sources its worker
read (its plan's settings), not what the settings say when it is adopted;
and a Session a migration replaced reports nothing it scans afterwards. On
Y-1's harness (the real application, its removal worker and scans).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_astra030_y1 import BOUND, Profile, _lane_idle, profiles  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402

__all__ = ["profiles"]  # Y-1's fixture, used here by name


def test_a_removals_own_scan_records_the_sources_its_worker_read(
    profiles: Callable[..., Profile], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings moved on while the worker scanned both folders: both are recorded."""
    profile = profiles(both_folders=True)
    application = profile.application
    profile.scans.postscan_used = True  # the removal's own scan succeeds
    profile.scans.held_used = True  # and no refresh is held
    both = (application.settings.roots, application.settings.scan_workshop)
    assert both[0] == (profile.root, profile.other)
    adopt = application._adopt_item_removal

    def adopted_later(result: RemovalResult) -> None:
        # Not reachable while the lane is serial: the settings changed
        # between the worker's scan and this adoption.
        application._settings = replace(application._settings, roots=(profile.root,))
        adopt(result)

    monkeypatch.setattr(application, "_adopt_item_removal", adopted_later)
    item = application.session.library.find(profile.dawn)
    assert item is not None
    removed: list[RemovalResult] = []
    assert application.remove_item_async(item, trash=True, finish=removed.append)
    spin_until(lambda: bool(removed) and _lane_idle(application), BOUND, what="the removal")

    assert removed[0].committed and removed[0].rescanned is not None
    assert application._accepted_library_sources == both, "what the worker read"
    assert "changed folders" in application.library_currency_refusal()
    assert removed[0].rescanned_sources == both


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
