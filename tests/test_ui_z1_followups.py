"""Three older seams Z-1's audit found: a failure on GTK after the work saved never strands it.

Each is reached only by a bug in this process's own GTK-side code (a window
that raises, an in-memory adoption that raises), never by a filesystem
failure: those come back from the worker as results. Injected here as such
bugs, through Y-1's harness (the real application, its authoring lane,
removal worker and scans):

(a) a library refresh whose result couldn't be shown, by a window that can't
    stop its spinner either: the scan barrier is released all the same, and
    whoever waits on the refresh is told it didn't land;
(b) a committed removal whose adoption raised: the inventory stays invalid
    (nothing is taken from, or published from, a library naming the removed
    file), a scan is asked for, and the lane says the removal saved;
(c) the startup repair of dangling playlist references, saved, whose
    adoption raised: the gate rests at "repair", where Try again opens it,
    never at "checking" until a restart.

Each test fails on 3bc464a.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_astra030_y1 import (  # noqa: E402
    BOUND,
    Profile,
    _lane_idle,
    _settled,
    profiles,
)
from wall_in_one.library import schedules  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402

__all__ = ["profiles"]  # Y-1's fixture, used here by name

SAVED_BUT = "Saved, but it couldn't be applied"


def test_a_refresh_a_broken_window_couldnt_show_still_releases_its_scan_barrier(
    profiles: Callable[..., Profile], monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = profiles()
    application = profile.application
    profile.scans.held_used = True  # nothing held: the refresh lands at once

    def show_library(_session: object) -> None:
        raise RuntimeError("injected: the window could not show the library")

    real_scanning = profile.window.show_library_scanning

    def show_library_scanning(scanning: bool) -> None:
        if not scanning:
            raise RuntimeError("injected: nor stop its spinner")
        real_scanning(scanning)

    monkeypatch.setattr(profile.window, "show_library", show_library)
    monkeypatch.setattr(profile.window, "show_library_scanning", show_library_scanning)
    told: list[bool] = []
    application.refresh_library(finished=told.append)
    spin_until(
        lambda: application._library_scan_future is None and _lane_idle(application),
        BOUND,
        what="the refresh and its reconciliation",
    )

    assert application._runtime_library_ready.is_set(), "a waiting gesture is let go"
    assert not application._runtime_library_scan_pending
    assert not application._runtime_library_scan_held, "and the application's hold"
    assert told == [False], "whoever waits is told it didn't land"


def test_a_committed_removal_whose_adoption_failed_keeps_the_inventory_invalid(
    profiles: Callable[..., Profile], monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = profiles()
    application = profile.application
    profile.scans.postscan_used = True  # the removal's own scan succeeds
    profile.scans.held_used = True
    real_refresh = application.refresh_library
    asked: list[Any] = []
    monkeypatch.setattr(application, "refresh_library", lambda **_k: asked.append(True))

    def adopting(_cleanups: object) -> None:
        raise RuntimeError("injected: adopting the removal failed")

    real_cleanups = application.session.adopt_library_cleanups
    monkeypatch.setattr(application.session, "adopt_library_cleanups", adopting)
    item = application.session.library.find(profile.dawn)
    assert item is not None
    finished: list[RemovalResult] = []
    assert application.remove_item_async(item, trash=True, finish=finished.append)
    spin_until(
        lambda: (
            not profile.dawn.exists()
            and not application._removal_active
            and _lane_idle(application)
        ),
        BOUND,
        what="the removal",
    )

    # Before: the inventory in force before the removal came back, and a
    # library naming the removed file was published.
    assert not application._inventory_valid
    assert "being scanned again after a removal" in application.library_currency_refusal()
    assert any(SAVED_BUT in report for report in profile.window.reports), profile.window.reports
    spin_until(lambda: bool(asked), BOUND, what="a scan to be asked for")

    monkeypatch.setattr(application.session, "adopt_library_cleanups", real_cleanups)
    monkeypatch.setattr(application, "refresh_library", real_refresh)
    application.refresh_library()
    spin_until(
        lambda: _settled(application) and application.library_currency_refusal() == "",
        BOUND,
        what="the scan after the removal",
    )
    assert application.session.library.find(profile.dawn) is None


def test_a_saved_repair_whose_adoption_failed_rests_the_gate_at_repair(
    profiles: Callable[..., Profile], monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = profiles()
    application = profile.application
    schedules.Store.open().add("deleted-playlist", weekdays=("mon",))  # a dangling rule
    store = application.session.schedules
    real_adopt = store.adopt_worker_forget_playlists

    def adopting(_playlists: object) -> None:
        raise RuntimeError("injected: adopting the repair failed")

    monkeypatch.setattr(store, "adopt_worker_forget_playlists", adopting)
    application._authoring_migration_ready = False
    application._authoring_repair_started = False
    application._open_authoring_after_migration()
    spin_until(
        lambda: _lane_idle(application) and any(SAVED_BUT in r for r in profile.window.reports),
        BOUND,
        what="the repair",
    )

    assert schedules.Store.open().rules == (), "the repair saved"
    assert application.authoring_gate == "repair", "before: checking, until a restart"
    assert not application.authoring_ready

    monkeypatch.setattr(store, "adopt_worker_forget_playlists", real_adopt)
    assert application.retry_authoring_repair()
    spin_until(lambda: application.authoring_ready, BOUND, what="Try again")
    assert application.authoring_gate == "open"
