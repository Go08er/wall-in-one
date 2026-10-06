"""A removal's own scan is installed only whole, and only of the current sources (z2b's review).

Two lows from r5-z2b's cold review, both on states the serial authoring lane
does not reach today, both of which left the library refused with nothing
queued to make it current again:

(i) A `RemovalResult` with ``rescanned`` but no ``rescanned_sources`` skipped
the adoption (no sources) and the fallback rescan (a library was there): the
inventory stayed invalid until someone rescanned by hand. Half a scan is now
no scan (`RemovalResult.own_scan`), so the application scans again.

(ii) A removal's own scan of sources the settings no longer name (they
changed between the worker's scan and its adoption, adopted the way the
``--service`` sync path adopts a folder change, which asks for no refresh)
was installed under its old sources: the guard and the compiler refused it,
rightly, and nothing replaced it. Where folders were added, everything in
them was also marked removed. It is no longer installed: one real rescan of
the current sources is queued, and until it lands the inventory is invalid,
so the guard and the compiler still refuse.

Real applications, their removal workers and scans (Y-1's harness, and the
compatibility service's); only the settings move inside the adoption, or
the half-emptied result, is injected. Each test except the missing-library
half of (i) fails on 3ab62b2.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import settle, spin_until  # noqa: E402
from tests.test_ui_astra030_y1 import BOUND, Profile, _lane_idle, profiles  # noqa: E402
from tests.test_ui_service_library_currency import lane_idle, services  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402

__all__ = ["profiles", "services"]  # the harnesses' fixtures, used here by name


def _count_refreshes(application: Application, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every `refresh_library` call from now on, queued ones included (bound at queue time)."""
    calls: list[int] = []
    real = application.refresh_library

    def counted(**keywords: Any) -> None:
        calls.append(1)
        real(**keywords)

    monkeypatch.setattr(application, "refresh_library", counted)
    return calls


def _remove_dawn(application: Application, profile: Profile) -> RemovalResult:
    item = application.session.library.find(profile.dawn)
    assert item is not None
    removed: list[RemovalResult] = []
    assert application.remove_item_async(item, trash=True, finish=removed.append)
    spin_until(lambda: bool(removed) and _lane_idle(application), BOUND, what="the removal")
    assert removed[0].committed and not profile.dawn.exists()
    return removed[0]


def _current(application: Application) -> bool:
    return (
        application._inventory_valid
        and application._library_scan_future is None
        and _lane_idle(application)
        and application.library_currency_refusal() == ""
    )


@pytest.mark.parametrize("missing", ["rescanned_sources", "rescanned"])
def test_half_of_a_removals_own_scan_is_scanned_again(
    profiles: Callable[..., Profile], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """(i): neither half is installed, and neither half leaves nothing queued."""
    profile = profiles()
    application = profile.application
    profile.scans.postscan_used = True  # the removal's own scan succeeds
    profile.scans.held_used = True  # and nothing is held
    refreshes = _count_refreshes(application, monkeypatch)
    adopt = application._adopt_item_removal

    def half(result: RemovalResult) -> None:
        whole = result.rescanned is not None and result.rescanned_sources is not None
        assert whole, "the worker returned a whole scan"
        if missing == "rescanned_sources":
            adopt(replace(result, rescanned_sources=None))
        else:
            adopt(replace(result, rescanned=None))

    monkeypatch.setattr(application, "_adopt_item_removal", half)
    _remove_dawn(application, profile)

    spin_until(lambda: _current(application), BOUND, what="the rescan after the removal")
    settings = application.settings
    assert application._accepted_library_sources == (settings.roots, settings.scan_workshop)
    assert application.session.library.find(profile.dawn) is None
    assert application.session.library.find(profile.noon) is not None
    settle(0.5)
    assert refreshes == [1], "one rescan, queued once"


@pytest.mark.parametrize("change", ["dropped", "added"])
def test_a_removals_own_scan_of_older_folders_is_replaced_by_a_rescan(
    profiles: Callable[..., Profile], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """(ii), in a window's application: never installed; refused until one rescan lands."""
    profile = profiles(both_folders=change == "dropped")
    application = profile.application
    profile.scans.postscan_used = True
    profile.scans.held_used = True
    old = (application.settings.roots, application.settings.scan_workshop)
    roots = (profile.root,) if change == "dropped" else (profile.root, profile.other)
    new = (roots, old[1])
    refreshes = _count_refreshes(application, monkeypatch)
    adopt = application._adopt_item_removal
    seen: dict[str, Any] = {}

    def adopted_later(result: RemovalResult) -> None:
        # Not reachable while every source change waits behind the removal on
        # the lane. As the service adopts a saved folder change: durably, in
        # the application and its Session, scanned inside (no refresh asked).
        saved = config.update({"roots": roots})
        application._settings = saved
        application._settings_requested = saved
        application.session.update_settings(saved, rescan_library=True)
        adopt(result)
        seen["valid"] = application._inventory_valid
        seen["refusal"] = application.library_currency_refusal()
        seen["accepted"] = application._accepted_library_sources

    monkeypatch.setattr(application, "_adopt_item_removal", adopted_later)
    result = _remove_dawn(application, profile)

    assert result.rescanned_sources == old, "the worker scanned the older folders"
    # Fail-closed until the rescan lands: the compiler installs nothing from an
    # invalid inventory (`_inventory_installable`), and the guard says why.
    assert not seen["valid"]
    assert "after a removal" in seen["refusal"]
    assert seen["accepted"] == new, "nothing recorded from the older folders' scan"

    spin_until(lambda: _current(application), BOUND, what="the rescan of the current folders")
    assert application._accepted_library_sources == new
    library = application.session.library
    assert library.find(profile.dawn) is None
    assert library.find(profile.noon) is not None
    assert (library.find(profile.dusk) is not None) is (change == "added")
    assert profile.dusk not in application._removed_item_paths, "only dawn was removed"
    settle(0.5)
    assert refreshes == [1], "one rescan, queued once"


def test_the_service_replaces_a_removals_own_scan_of_older_folders(
    services: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(ii) in ``--service``, removed from its window: its synchronous rescan follows.

    (Its ``ctl remove`` is the synchronous legacy removal, which runs no scan of
    its own; a window the service presents removes through the worker.)
    """
    service = services()
    application = service.application
    old = ((service.root, service.other), False)
    new = ((service.root,), False)
    assert application._accepted_library_sources == old
    refreshes = _count_refreshes(application, monkeypatch)
    adopt = application._adopt_item_removal
    seen: dict[str, Any] = {}

    def adopted_later(result: RemovalResult) -> None:
        # The service's own adoption of a saved folder change, which rescans
        # inside (`Session.update_settings`), landing between the worker's
        # scan and this adoption: not reachable while the lane is serial.
        application._adopt_settings(application.settings, config.update({"roots": new[0]}))
        assert application._accepted_library_sources == new, "the service's own rescan"
        adopt(result)
        seen["valid"] = application._inventory_valid
        seen["refusal"] = application.library_currency_refusal()

    monkeypatch.setattr(application, "_adopt_item_removal", adopted_later)
    item = application.session.library.find(service.noon)
    assert item is not None
    removed: list[RemovalResult] = []
    assert application.remove_item_async(item, trash=True, finish=removed.append)
    spin_until(lambda: bool(removed) and lane_idle(application), BOUND, what="the removal")
    assert removed[0].committed and not service.noon.exists()
    assert removed[0].rescanned_sources == old, "the worker scanned the older folders"
    assert not seen["valid"] and "after a removal" in seen["refusal"]

    spin_until(
        lambda: application._inventory_valid and lane_idle(application),
        BOUND,
        what="the service's rescan",
    )
    assert application.library_currency_refusal() == ""
    assert application._accepted_library_sources == new
    assert application.session.library.find(service.noon) is None
    assert not service.holds_dusk(), "the current folders' library"
    settle(0.5)
    assert refreshes == [1], "one rescan, queued once"
