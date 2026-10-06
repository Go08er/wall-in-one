"""Putting a playlist into use is judged only by a current library (Y-1's follow-up).

Y-1 refused every playlist change that takes wallpapers from the library, or
is judged by it, while the application knows that library is stale. Putting
a playlist into use is judged by it too (`runtime_config.use_refusal`: does
it hold anything playable?), and was still judged against the stale one: a
schedule rule, a display's own playlist, the default. It could not block
publication (the removal's dry run keeps every playlist with entries
compilable), but every path now judges playability against a current
library, at the head of the authoring lane, like the rest.

Y-1's harness: the real application, its authoring lane, removal worker and
scans; dawn is trashed, its own scan fails and the fallback scan is held.
Each path is refused while it is held, saying to wait for the scan, and
nothing is saved; once it lands, the same request is saved. Each test fails
on f33fff0.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_astra030_y1 import (  # noqa: E402
    BOUND,
    WAIT,
    Editing,
    Profile,
    _ctl,
    _lane_idle,
    _refused,
    _release,
    _remove_dawn,
    editing,
    profiles,
)
from wall_in_one import config  # noqa: E402
from wall_in_one.library import displays, playlists, schedules  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402
from wall_in_one.ui.next.schedule_state import RuleView  # noqa: E402
from wall_in_one.ui.schedules_page import SchedulesPage  # noqa: E402

__all__ = ["editing", "profiles"]  # Y-1's fixtures, used here by name


def _evening(profile: Profile) -> playlists.Playlist:
    """A playlist that stays playable: noon is never removed."""
    return profile.application.session.playlists.create(
        "Evening", entries=[playlists.Entry(playlists.new_id(), str(profile.noon))]
    )


def _nothing_in_use() -> bool:
    return schedules.Store.open().rules == () and displays.Store.open().all() == ()


def _waited(profile: Profile, heard: int) -> list[str]:
    said = profile.window.reports[heard:]
    assert said and all(WAIT in line for line in said), said
    return said


def _scheduling(
    editing: Callable[[Application], Editing], application: Application
) -> RealScheduling:
    scheduling = editing(application).state.scheduling
    assert isinstance(scheduling, RealScheduling)
    return scheduling


def test_ctl_puts_a_playlist_into_use_only_against_a_current_library(
    profiles: Callable[..., Profile], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``schedule-add`` and ``display-assign``, on the lane and in the headless service."""
    profile = profiles()
    application = profile.application
    evening = _evening(profile)
    _remove_dawn(profile)

    _refused(_ctl(application, "schedule-add", "Evening days=mon"))
    _refused(_ctl(application, "display-assign", "DP-1 Evening"))
    monkeypatch.setattr(application, "_service_start", True)  # the headless service's path
    _refused(_ctl(application, "schedule-add", "Evening days=tue"))
    _refused(_ctl(application, "display-assign", "DP-1 Evening"))
    monkeypatch.setattr(application, "_service_start", False)
    assert _nothing_in_use()

    _release(profile)
    assert _ctl(application, "schedule-add", "Evening days=mon").ok
    assert _ctl(application, "display-assign", "DP-1 Evening").ok
    assert [rule.playlist for rule in schedules.Store.open().rules] == [evening.id]
    assert dict(displays.Store.open().all()) == {"DP-1": evening.id}


def test_the_classic_schedules_page_puts_a_playlist_into_use_only_against_a_current_library(
    profiles: Callable[..., Profile],
) -> None:
    """Its rule editor's Save and a display's playlist."""
    profile = profiles()
    application = profile.application
    evening = _evening(profile)
    page = SchedulesPage(application)
    page.refresh(application.session)
    choices = application.session.playlists.all()
    index = [playlist.id for playlist in choices].index(evening.id)

    def save_rule() -> None:
        page._rule_playlist.set_selected(index)
        page._weekdays[0].set_active(True)
        page._save_rule(choices)

    def assign() -> None:
        names = ["Follow default", *(playlist.name for playlist in choices)]
        row = Adw.ComboRow(title="DP-1", model=Gtk.StringList.new(names))
        row.set_selected(index + 1)
        page._make_display_changed("DP-1", choices)(row, None)

    _remove_dawn(profile)
    heard = len(profile.window.reports)
    save_rule()
    assign()
    spin_until(
        lambda: _lane_idle(application) and len(profile.window.reports) >= heard + 2,
        BOUND,
        what="both refusals",
    )
    _waited(profile, heard)
    assert _nothing_in_use()

    _release(profile)
    save_rule()
    assign()
    spin_until(
        lambda: (
            _lane_idle(application)
            and len(schedules.Store.open().rules) == 1
            and dict(displays.Store.open().all()) == {"DP-1": evening.id}
        ),
        BOUND,
        what="both saves",
    )
    assert [rule.playlist for rule in schedules.Store.open().rules] == [evening.id]


def test_the_new_schedule_and_displays_pages_put_a_playlist_into_use_only_against_a_current_library(
    profiles: Callable[..., Profile], editing: Callable[[Application], Editing]
) -> None:
    """A rule's Save and a display's own playlist."""
    profile = profiles()
    application = profile.application
    evening = _evening(profile)
    scheduling = _scheduling(editing, application)
    _remove_dawn(profile)

    heard = len(profile.window.reports)
    outcomes: list[bool] = []
    scheduling.save_rule("", RuleView("", evening.id), outcomes.append)
    scheduling.assign_display("DP-1", evening.id, outcomes.append)
    spin_until(lambda: len(outcomes) == 2 and _lane_idle(application), BOUND, what="refusals")
    assert outcomes == [False, False]
    _waited(profile, heard)
    assert _nothing_in_use()

    _release(profile)
    outcomes.clear()
    scheduling.save_rule("", RuleView("", evening.id), outcomes.append)
    scheduling.assign_display("DP-1", evening.id, outcomes.append)
    spin_until(lambda: len(outcomes) == 2 and _lane_idle(application), BOUND, what="the saves")
    assert outcomes == [True, True]
    assert [rule.playlist for rule in schedules.Store.open().rules] == [evening.id]
    assert dict(displays.Store.open().all()) == {"DP-1": evening.id}


def test_the_default_playlist_is_judged_only_by_a_current_library_in_either_window(
    profiles: Callable[..., Profile], editing: Callable[[Application], Editing]
) -> None:
    """The new window's Schedule page and classic's default row: one settings batch, refused."""
    profile = profiles()
    application = profile.application
    evening = _evening(profile)
    scheduling = _scheduling(editing, application)
    page = SchedulesPage(application)
    page.refresh(application.session)
    choices = application.session.playlists.all()
    index = [playlist.id for playlist in choices].index(evening.id)

    def classic() -> None:
        names = ["All wallpapers", *(playlist.name for playlist in choices)]
        row = Adw.ComboRow(title="Default", model=Gtk.StringList.new(names))
        row.set_selected(index + 1)
        page._make_default_changed(choices)(row, None)

    def written() -> bool:
        return not application._settings_authoring_running and _lane_idle(application)

    _remove_dawn(profile)
    heard = len(profile.window.reports)
    outcomes: list[bool] = []
    scheduling.choose_default_playlist(evening.id, outcomes.append)
    spin_until(lambda: bool(outcomes) and written(), BOUND, what="the new window's refusal")
    classic()
    spin_until(
        lambda: written() and len(profile.window.reports) >= heard + 2,
        BOUND,
        what="classic's refusal",
    )
    assert outcomes == [False]
    _waited(profile, heard)
    assert config.load_strict().active_playlist == ""
    assert application.settings.active_playlist == ""

    _release(profile)
    scheduling.choose_default_playlist(evening.id, outcomes.append)
    spin_until(lambda: len(outcomes) == 2 and written(), BOUND, what="the new window's save")
    assert outcomes == [False, True]
    assert config.load_strict().active_playlist == evening.id
    scheduling.choose_default_playlist("", outcomes.append)  # back to All wallpapers
    spin_until(lambda: len(outcomes) == 3 and written(), BOUND, what="the reset")
    classic()
    spin_until(
        lambda: written() and config.load_strict().active_playlist == evening.id,
        BOUND,
        what="classic's save",
    )
