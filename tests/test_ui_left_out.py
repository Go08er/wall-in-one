"""What the published document was built without: said once, kept, and said again on a change.

The compiler leaves out a default, a schedule rule or a display's own
playlist that names a playlist which doesn't exist or has nothing to play,
and publishes everything else (`runtime_config.LeftOut`). The application
says what was left out where it says a publication problem: in the window's
toast and the activity log when the set of references left out changes, and
as a standing notice (`Application.left_out_notice`), which the new window
keeps under its header. It is never said by every publication or status
poll that meets the same set.
"""

from __future__ import annotations

import tomllib
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_runtime_health_sync import _snapshot as _failed_wallpaper  # noqa: E402
from tests.test_ui_codex030_round1b import (  # noqa: E402, F401
    Profile,
    profiles,
)
from tests.test_ui_codex030_round1b import _playlist as _saved  # noqa: E402
from tests.test_ui_codex030_round1c import (  # noqa: E402, F401
    Shown,
    shown,
    toolkit,
)
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.library import displays, schedules  # noqa: E402
from wall_in_one.ui.app import _RuntimeHealthRequest  # noqa: E402
from wall_in_one.ui.next import window as next_window  # noqa: E402

ALL_MEDIA = runtime_config.FALLBACK_PLAYLIST_ID
RULE_NAMES_NOTHING = (
    "A schedule rule names a playlist that doesn\u2019t exist (ghost), so that rule is left out."
)
DEFAULT_NAMES_NOTHING = (
    "The default names a playlist that doesn\u2019t exist ({}), so \u201cAll media\u201d "
    "plays instead."
)
DEFAULT_HAS_NOTHING = (
    "The default names \u201c{}\u201d, which has nothing to play, so \u201cAll media\u201d "
    "plays instead."
)
NOTHING_LEFT_OUT = "Nothing is left out of playback any more: every saved playlist plays."


def _published(application: Any) -> dict[str, Any]:
    """One publication through the application's own lane; the document on disk after it."""
    assert application._publish_runtime_async()
    spin_until(
        lambda: (
            not application._runtime_compile_pending
            and not application._authoring_active
            and not application._runtime_publication_held
        ),
        30,
        what="the publication lane",
    )
    return _document()


def _document() -> dict[str, Any]:
    return tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))


def _logged(application: Any) -> list[str]:
    """What the activity log holds from the application, every time each was said."""
    return [entry.text for entry in application.activity_log.entries if entry.source == "app"]


# -- said once ---------------------------------------------------------------------------------


def test_what_is_left_out_is_said_once_and_again_only_when_the_set_changes(
    profiles: Any,  # noqa: F811
) -> None:
    """Each thing below is another program's finished save: no guard of this app's is asked."""
    profile: Profile = profiles()
    application = profile.application
    reports = profile.window.reports
    lone = _saved(profile, "Lone", [profile.dawn], used=True)
    document = _published(application)
    assert [rule["playlist"] for rule in document["schedules"]] == [lone.id]
    assert application.left_out_notice == "" and reports == []

    schedules.Store.open().add("ghost", rule_id="dangling")
    document = _published(application)

    # Failed on a5999e2: "schedule 'dangling' refers to unknown playlist 'ghost'", and
    # nothing was published.
    assert application.publication_problem == ""
    assert [rule["playlist"] for rule in document["schedules"]] == [lone.id], "the rest is there"
    assert application.left_out_notice == RULE_NAMES_NOTHING
    assert reports == [RULE_NAMES_NOTHING]
    assert _logged(application).count(RULE_NAMES_NOTHING) == 1

    # Not again: by later publications, by one that changes the document, or
    # by a status poll whose health save compiles too.
    _published(application)
    done: list[object] = []
    assert application.update_settings_async(
        shuffle=not application.settings.shuffle,
        on_success=done.append,
        on_error=done.append,
    )
    spin_until(lambda: bool(done) and not application._runtime_compile_pending, 30, what="a save")
    assert _document()["settings"]["shuffle"] is application.settings.shuffle
    request = _RuntimeHealthRequest(
        application._runtime_authoring_generation,
        application.session.library,
        _failed_wallpaper(profile.dawn).message,
    )
    health = application._ingest_runtime_health(request)
    assert health.accepted and health.changed == 1, health
    application._apply_runtime_health_result(health)
    assert reports == [RULE_NAMES_NOTHING], "said once"
    assert _logged(application).count(RULE_NAMES_NOTHING) == 1

    # The set changes: said again, as it now is.
    config.update({"active_playlist": "phantom"})
    document = _published(application)
    both = f"{DEFAULT_NAMES_NOTHING.format('phantom')} {RULE_NAMES_NOTHING}"
    assert document["default_playlist"] == ALL_MEDIA
    assert application.left_out_notice == both
    assert reports == [RULE_NAMES_NOTHING, both]

    # A display's own playlist is not compiled while mirrored: nothing new to say.
    displays.Store.open().assign("DP-1", "ghost")
    _published(application)
    assert reports == [RULE_NAMES_NOTHING, both]

    # Put right elsewhere: the notice goes, and the log says so. No toast for good news.
    schedules.Store.open().remove("dangling")
    config.update({"active_playlist": lone.id})
    document = _published(application)
    assert document["default_playlist"] == lone.id
    assert application.left_out_notice == ""
    assert reports == [RULE_NAMES_NOTHING, both]
    assert _logged(application)[-1] == NOTHING_LEFT_OUT


def test_a_store_that_can_not_be_read_is_a_publication_problem_not_something_left_out(
    profiles: Any,  # noqa: F811
) -> None:
    """ "Couldn't look" is never "nothing there": refused, naming the file, and the
    document that was published before stays, with the notice that is true of it."""
    profile: Profile = profiles()
    application = profile.application
    _saved(profile, "Lone", [profile.dawn])
    schedules.Store.open().add("ghost", rule_id="dangling")
    _published(application)
    good = paths.runtime_config_path().read_bytes()
    assert application.left_out_notice == RULE_NAMES_NOTHING

    # Damaged: what the rules were is not known, so neither is what they name.
    schedules.state_path().write_text("{ this is not JSON", encoding="utf-8")
    _published(application)

    problem = application.publication_problem
    assert "schedules" in problem and schedules.state_path().name in problem, problem
    assert "the existing runtime configuration was left untouched" in problem
    assert paths.runtime_config_path().read_bytes() == good
    assert application.left_out_notice == RULE_NAMES_NOTHING, "still true of what is published"
    assert profile.window.reports == [RULE_NAMES_NOTHING, problem]


def test_the_headless_service_logs_what_it_left_out_once(
    profiles: Any,  # noqa: F811
) -> None:
    """``--service`` publishes synchronously and has no window: the log is where it is said."""
    profile: Profile = profiles(service=True)
    application = profile.application
    _saved(profile, "Lone", [profile.dawn])
    application.session.schedules.add("ghost", rule_id="dangling")

    assert application._update_runtime_document() == (True, True)
    assert application._update_runtime_document() == (True, False)

    assert "schedules" not in _document()
    assert _logged(application).count(RULE_NAMES_NOTHING) == 1
    assert application.left_out_notice == RULE_NAMES_NOTHING


# -- kept under the new window's header --------------------------------------------------------


def _publish(shown: Shown) -> dict[str, Any]:  # noqa: F811
    application = shown.opened.application
    assert application._publish_runtime_async()
    shown.opened.wait(
        "the publication",
        lambda: (
            not application._runtime_compile_pending and not application._runtime_publication_held
        ),
    )
    return _document()


def test_the_new_window_keeps_what_was_left_out_under_its_header(
    shown: Shown,  # noqa: F811
) -> None:
    opened = shown.opened
    application = opened.application
    state = opened.window.state
    evening = application.session.playlists.create("Evening")
    application.session.playlists.add(evening.id, shown.dawn.path)
    _publish(shown)

    def of_the_window() -> object:
        """The window's own notice; without one, the state shows the runtime's (not running)."""
        return opened.window._window_banner()

    assert of_the_window() is None

    schedules.Store.open().add("ghost", rule_id="dangling")  # another program
    _publish(shown)

    banner = state.banner()
    assert banner is not None and banner.title == RULE_NAMES_NOTHING
    assert next_window.PUBLICATION_FAILED not in banner.title, "it was published"
    assert banner.button == "Playlists"
    assert opened.reports == [RULE_NAMES_NOTHING]
    state.banner_activated(banner.action)
    assert opened.window.current_page is opened.window.pages["playlist"]

    # A publication that didn't get through is the more urgent thing to read;
    # what was left out stands again once one does.
    application._set_publication_problem("the disk is full")
    refused = state.banner()
    assert refused is not None
    assert refused.title == f"{next_window.PUBLICATION_FAILED} the disk is full"
    application._set_publication_problem("")
    again = state.banner()
    assert again is not None and again.title == RULE_NAMES_NOTHING

    schedules.Store.open().remove("dangling")
    _publish(shown)
    assert of_the_window() is None
    shown_now = state.banner()
    assert shown_now is None or shown_now.title != RULE_NAMES_NOTHING
    assert opened.reports == [RULE_NAMES_NOTHING]
