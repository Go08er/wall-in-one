"""After Codex 0.3.0 round 1: every path keeps the runtime configuration buildable.

Round 1 checked the new window's writes at the head of the authoring lane.
These are the rest of the writes that put a playlist into use or take away
what one plays, in the real application, whichever window or control verb
asks: ``ctl`` on the authoring lane and in the headless service, the classic
window's pages, Quick choice, the synchronous settings write, and pairing
changes that would leave a video without a still. Each is refused with the
same check (`runtime_config`'s, called from the write's ``prepare``) and
nothing is written; each reproduces a write that, on f442d1e, saved and left
a document the compiler refuses (``runtime_config.render`` raises).

``ctl`` runs through the real verb table and dispatcher with wire-encoded
requests, as ``test_ui_runtime`` drives it. A verb never queues behind
another write (`_Commands.authoring_gate` answers ``authoring-busy``), so
its cases are bad writes; the classic window's cases include a held lane,
where its write is checked against what the one queued before it saved.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_next_state import FakeApplication, _library, _session  # noqa: E402
from tests.test_ui_next_editing import hold_the_lane  # noqa: E402
from tests.test_ui_next_playlists import Shown  # noqa: E402
from tests.test_ui_playlists_polish import QuietLoader  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _attach, _close  # noqa: E402
from tests.whole_document import builds_whole  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import pairings, playlists  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import QUICK_CHOICE_ID  # noqa: E402
from wall_in_one.ui import playlists_page, schedules_page  # noqa: E402
from wall_in_one.ui.app import Application, _Commands  # noqa: E402
from wall_in_one.ui.next import widgets  # noqa: E402
from wall_in_one.ui.next.real_state import RealAppState  # noqa: E402

#: `runtime_config.PlaybackGuardError.kind`, spelled out so this file imports on f442d1e too.
GUARD = "nothing-to-play"


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class Profile(NamedTuple):
    application: Application
    window: FakeWindow
    dawn: MediaItem
    dusk: MediaItem
    rain: MediaItem  # a video with no still of its own


@pytest.fixture
def profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    made: list[Application] = []

    def make(*, service: bool = False) -> Profile:
        """A windowless application over two pictures and a video, and a quiet runtime."""
        for name in ("state", "config", "cache", "data"):
            monkeypatch.setenv(f"XDG_{name.upper()}_HOME", str(tmp_path / name))
        application = Application(service=service)
        application._authoring_migration_ready = True
        made.append(application)
        window = FakeWindow()
        _attach(application, window)
        monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
        root = tmp_path / "wallpapers"
        root.mkdir()
        for name in ("dawn.png", "dusk.png", "rain.mp4"):
            (root / name).write_bytes(b"media")
        dawn = MediaItem(root / "dawn.png", Kind.STILL, 5, 1)
        dusk = MediaItem(root / "dusk.png", Kind.STILL, 5, 1)
        rain = MediaItem(root / "rain.mp4", Kind.VIDEO, 5, 1)
        settings = replace(application.settings, roots=(root,)).validated()
        config.save(settings)
        application._settings = settings
        application._settings_requested = settings
        application.session.update_settings(settings, rescan_library=False)
        application.session.adopt_library(Library((root,), (dawn, dusk, rain)))
        application._accepted_library_sources = (settings.roots, settings.scan_workshop)
        return Profile(application, window, dawn, dusk, rain)

    yield make
    for application in made:
        _close(application)


def _playlist(
    profile: Profile, name: str, items: Sequence[MediaItem], *, used: bool = False
) -> playlists.Playlist:
    """A saved playlist of ``items`` (entry ids ``<name>-<n>``), a rule's if ``used``."""
    store = profile.application.session.playlists
    made = store.create(name)
    for index, item in enumerate(items):
        store.add(made.id, item.path, entry_id=f"{name.lower()}-{index}")
    if used:
        profile.application.session.schedules.add(made.id)
    return store.find(made.id)


def _entries(profile: Profile, pid: str) -> list[str]:
    return [entry.id for entry in profile.application.session.playlists.find(pid).entries]


def _builds(profile: Profile) -> bool:
    """Whether the runtime document is built whole: nothing left out, no playlist dropped.

    What "it builds" meant while one playlist that can't play refused the
    whole compile (`tests.whole_document`).
    """
    application = profile.application
    return builds_whole(application.settings, application.session)


def _ctl(profile: Profile, verb: str, argument: str | None = None) -> Response:
    """One verb through the real verb table, as the control socket hands it over."""
    replies: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(profile.application)),
        Request(verb, argument).encode(),
        replies.append,
    )
    spin_until(lambda: bool(replies))
    return replies[0]


def _refused(reply: Response, *words: str) -> None:
    assert not reply.ok and reply.kind == GUARD, reply
    for word in words:
        assert word in reply.message, reply.message


def _lane_idle(application: Application) -> bool:
    """Every queued write has run and been adopted (publication may still follow, or fail)."""
    return (
        not application._authoring_active
        and not application._authoring_queue
        and not application._settings_authoring_running
    )


def _said(profile: Profile, words: str) -> bool:
    return any(words in report for report in profile.window.reports)


def _independent(application: Application) -> None:
    """Each display its own route, so the compiler builds display assignments."""
    application.update_settings(
        display_mode=config.DISPLAY_MODE_INDEPENDENT, theme_source_connector="DP-1"
    )
    assert application.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT


def _unresolved_when_cleared(profile: Profile) -> playlists.Playlist:
    """``rain`` shows ``dusk`` by choice, and a rule uses a playlist of it alone."""
    profile.application.session.pairings.choose_still(profile.rain, profile.dusk.path)
    moving = _playlist(profile, "Moving", [profile.rain], used=True)
    assert _builds(profile)
    return moving


# -- ctl, on the authoring lane ------------------------------------------------------------


def test_ctl_playlist_remove_keeps_a_used_playlists_last_wallpaper(profiles: Any) -> None:
    profile = profiles()
    lone = _playlist(profile, "Lone", [profile.dawn], used=True)
    _refused(
        _ctl(profile, "playlist-remove", f"{lone.id} lone-0"),
        "Nothing was removed from \u201cLone\u201d",
        "(used by 1 schedule rule) would have nothing left to play",
    )
    assert _entries(profile, lone.id) == ["lone-0"]
    assert _builds(profile)


def test_ctl_playlist_remove_keeps_an_unused_playlist_buildable(profiles: Any) -> None:
    profile = profiles()
    mixed = _playlist(profile, "Mixed", [profile.dawn, profile.rain])
    _refused(
        _ctl(profile, "playlist-remove", f"{mixed.id} mixed-0"),
        "\u201cMixed\u201d would hold only wallpapers playback can\u2019t use",
    )
    assert _entries(profile, mixed.id) == ["mixed-0", "mixed-1"]
    assert _builds(profile)


def test_ctl_playlist_add_refuses_an_unresolved_video_into_an_empty_playlist(
    profiles: Any,
) -> None:
    profile = profiles()
    empty = _playlist(profile, "Empty", [])
    _refused(
        _ctl(profile, "playlist-add", f"{empty.id} {profile.rain.path}"),
        "Nothing was added to \u201cEmpty\u201d",
    )
    assert _entries(profile, empty.id) == []
    assert _builds(profile)
    assert _ctl(profile, "playlist-add", f"{empty.id} {profile.dawn.path}").ok, "a picture is fine"


def test_ctl_schedule_add_refuses_a_playlist_with_nothing_to_play(profiles: Any) -> None:
    profile = profiles()
    _playlist(profile, "Empty", [])
    _refused(
        _ctl(profile, "schedule-add", "Empty days=sat,sun"),
        "The schedule rule was not added: \u201cEmpty\u201d has nothing to play yet",
    )
    assert profile.application.session.schedules.rules == ()
    assert _builds(profile)


def test_ctl_display_assign_refuses_a_playlist_with_nothing_to_play(profiles: Any) -> None:
    profile = profiles()
    _independent(profile.application)
    empty = _playlist(profile, "Empty", [])
    _refused(
        _ctl(profile, "display-assign", f"DP-1 {empty.id}"),
        "DP-1\u2019s playlist was not changed",
    )
    assert profile.application.session.displays.all() == ()
    assert _builds(profile)


def test_ctl_remove_keeps_a_used_playlists_last_wallpaper(profiles: Any) -> None:
    profile = profiles()
    _playlist(profile, "Lone", [profile.dawn], used=True)
    _refused(
        _ctl(profile, "remove", str(profile.dawn.path)),
        "dawn was not removed: \u201cLone\u201d (used by 1 schedule rule)",
    )
    assert profile.dawn.path.exists(), "nothing was moved to the Trash"
    assert profile.application.session.library.find(profile.dawn.path) is not None
    assert _builds(profile)


def test_ctl_select_refuses_a_video_without_a_still(profiles: Any) -> None:
    profile = profiles()
    _refused(
        _ctl(profile, "select", str(profile.rain.path)),
        "Playback unavailable for rain: it has no still for playback to show yet",
    )
    assert profile.application.session.playlists.get(QUICK_CHOICE_ID) is None
    assert _builds(profile)


def test_ctl_still_default_keeps_a_used_video_its_still(profiles: Any) -> None:
    profile = profiles()
    _unresolved_when_cleared(profile)
    _refused(
        _ctl(profile, "still", f"{profile.rain.path} default"),
        "rain\u2019s still was not changed: it would have none",
        "\u201cMoving\u201d (used by 1 schedule rule) would have nothing left to play",
    )
    record = profile.application.session.pairings.get(pairings.Identity.of(profile.rain))
    assert record is not None and record.still == profile.dusk.path
    assert _builds(profile)


def test_ctl_reset_pairing_keeps_a_used_video_its_still(profiles: Any) -> None:
    profile = profiles()
    _unresolved_when_cleared(profile)
    _refused(
        _ctl(profile, "reset-pairing", str(profile.rain.path)),
        "rain was not reset: it would have no still",
    )
    record = profile.application.session.pairings.get(pairings.Identity.of(profile.rain))
    assert record is not None and record.still == profile.dusk.path
    assert _builds(profile)


# -- ctl, in the headless service ------------------------------------------------------------


def test_the_headless_service_refuses_the_same_writes(profiles: Any) -> None:
    """The service writes at once, without the lane; each verb is checked just before."""
    profile = profiles(service=True)
    application = profile.application
    assert application.legacy_service
    session = application.session
    _independent(application)
    lone = _playlist(profile, "Lone", [profile.dawn], used=True)
    mixed = _playlist(profile, "Mixed", [profile.dawn, profile.rain])
    empty = _playlist(profile, "Empty", [])

    _refused(_ctl(profile, "playlist-remove", f"{lone.id} lone-0"), "nothing left to play")
    _refused(_ctl(profile, "playlist-remove", f"{mixed.id} mixed-0"), "only wallpapers")
    _refused(_ctl(profile, "playlist-add", f"{empty.id} {profile.rain.path}"), "only wallpapers")
    _refused(_ctl(profile, "schedule-add", "Empty"), "nothing to play yet")
    _refused(_ctl(profile, "display-assign", f"DP-1 {empty.id}"), "nothing to play yet")
    _refused(_ctl(profile, "remove", str(profile.dawn.path)), "dawn was not removed")
    _refused(_ctl(profile, "select", str(profile.rain.path)), "no still for playback")
    assert _entries(profile, lone.id) == ["lone-0"]
    assert _entries(profile, mixed.id) == ["mixed-0", "mixed-1"]
    assert _entries(profile, empty.id) == []
    assert [rule.playlist for rule in session.schedules.rules] == [lone.id]
    assert session.displays.all() == ()
    assert profile.dawn.path.exists()
    assert session.playlists.get(QUICK_CHOICE_ID) is None
    assert _builds(profile)

    # And the pairing verbs, once a playlist in use holds only the video.
    session.playlists.remove_entry(mixed.id, "mixed-0")  # what can't play goes first
    session.playlists.remove_entry(mixed.id, "mixed-1")
    _unresolved_when_cleared(profile)
    _refused(_ctl(profile, "still", f"{profile.rain.path} default"), "it would have none")
    _refused(_ctl(profile, "reset-pairing", str(profile.rain.path)), "would have no still")
    record = session.pairings.get(pairings.Identity.of(profile.rain))
    assert record is not None and record.still == profile.dusk.path
    assert _builds(profile)


# -- the classic window ----------------------------------------------------------------------


def _classic_playlists(
    profile: Profile, monkeypatch: pytest.MonkeyPatch, pid: str
) -> playlists_page.PlaylistsPage:
    monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
    page = playlists_page.PlaylistsPage(profile.application)
    page._selected = pid
    page.refresh(profile.application.session)
    return page


def _classic_schedules(profile: Profile) -> schedules_page.SchedulesPage:
    page = schedules_page.SchedulesPage(profile.application)
    page.refresh(profile.application.session)
    return page


def _descendants(widget: Gtk.Widget) -> list[Gtk.Widget]:
    found: list[Gtk.Widget] = []
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        found.extend(_descendants(child))
        child = child.get_next_sibling()
    return found


def _choose(row: Adw.ComboRow, name: str) -> None:
    model = cast(Gtk.StringList, row.get_model())
    row.set_selected(
        next(index for index in range(model.get_n_items()) if model.get_string(index) == name)
    )


def test_classic_entry_removal_queued_behind_the_default_it_would_empty_is_refused(
    profiles: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default saves first; the classic removal then finds "Lone" in use."""
    profile = profiles()
    application = profile.application
    lone = _playlist(profile, "Lone", [profile.dawn])
    page = _classic_playlists(profile, monkeypatch, lone.id)
    saved: list[object] = []

    gate = hold_the_lane(application)
    application.update_settings_async(active_playlist=lone.id, on_success=saved.append)
    page._make_remove("lone-0")(Gtk.Button())
    gate.set()
    spin_until(lambda: bool(saved) and _lane_idle(application), what="both writes to settle")

    assert config.load_strict().active_playlist == lone.id, "the default saved"
    assert _entries(profile, lone.id) == ["lone-0"], "the removal was refused"
    assert _said(
        profile,
        "Nothing was removed from \u201cLone\u201d: \u201cLone\u201d (used by the default) "
        "would have nothing left to play",
    )
    assert _builds(profile)


def test_classic_rule_save_queued_behind_the_removal_that_empties_it_is_refused(
    profiles: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classic removal empties "Lone" first; the classic rule for it is then refused."""
    profile = profiles()
    application = profile.application
    lone = _playlist(profile, "Lone", [profile.dawn])
    entries = _classic_playlists(profile, monkeypatch, lone.id)
    rules = _classic_schedules(profile)
    _choose(rules._rule_playlist, "Lone")

    gate = hold_the_lane(application)
    entries._make_remove("lone-0")(Gtk.Button())
    rules._rule_commit.emit("clicked")
    gate.set()
    spin_until(lambda: _lane_idle(application), what="both writes to settle")

    assert _entries(profile, lone.id) == [], "an unused playlist may be emptied"
    assert application.session.schedules.rules == (), "the rule was refused"
    assert _said(profile, "The schedule rule was not saved: \u201cLone\u201d has nothing to play")
    assert _builds(profile)


def test_classic_display_assignment_of_a_playlist_with_nothing_to_play_is_refused(
    profiles: Any,
) -> None:
    profile = profiles()
    application = profile.application
    _independent(application)
    lone = _playlist(profile, "Lone", [profile.dawn])
    _playlist(profile, "Empty", [])
    application.session.displays.assign("DP-1", lone.id)
    page = _classic_schedules(profile)
    (row,) = (
        widget
        for widget in _descendants(page)
        if isinstance(widget, Adw.ComboRow) and widget.get_title() == "DP-1"
    )

    _choose(row, "Empty")
    spin_until(lambda: _lane_idle(application), what="the assignment to settle")

    assert application.session.displays.playlist_for("DP-1") == lone.id, "it was refused"
    assert _said(profile, "DP-1\u2019s playlist was not changed: \u201cEmpty\u201d has nothing")
    assert _builds(profile)


def test_classic_add_of_a_video_without_a_still_to_an_empty_playlist_is_refused(
    profiles: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = profiles()
    empty = _playlist(profile, "Empty", [])
    page = _classic_playlists(profile, monkeypatch, empty.id)

    page._make_add(profile.rain)(Gtk.Button())
    spin_until(lambda: _lane_idle(profile.application), what="the addition to settle")

    assert _entries(profile, empty.id) == [], "it was refused"
    assert _said(profile, "\u201cEmpty\u201d would hold only wallpapers playback can\u2019t use")
    assert _builds(profile)


# -- Quick choice, and the synchronous settings write -------------------------------------------


def test_quick_choice_of_a_video_without_a_still_is_refused_everywhere_and_on_one(
    profiles: Any,
) -> None:
    profile = profiles()
    application = profile.application

    def done() -> bool:
        return _lane_idle(application) and not application._quick_choice_pending

    application.play_item_async(profile.rain)
    spin_until(done, what="Quick choice to settle")
    assert [playlist.name for playlist in application.session.playlists.all()] == []
    application.play_item_on_async(profile.rain, "DP-1")
    spin_until(done, what="Quick choice on DP-1 to settle")
    assert [playlist.name for playlist in application.session.playlists.all()] == []

    refusals = [text for text in profile.window.reports if "Playback unavailable for rain" in text]
    assert len(refusals) == 2 and "it has no still for playback to show yet" in refusals[0]
    assert _builds(profile)


def test_the_synchronous_settings_write_refuses_a_default_with_nothing_to_play(
    profiles: Any,
) -> None:
    profile = profiles()
    application = profile.application
    empty = _playlist(profile, "Empty", [])

    with pytest.raises(config.ConfigError) as refused:
        application.update_settings(active_playlist=empty.id)
    assert "the chosen default playlist can\u2019t be used" in str(refused.value)
    assert "\u201cEmpty\u201d has nothing to play yet" in str(refused.value)

    assert config.load_strict().active_playlist == ""
    assert application.settings.active_playlist == ""
    assert _builds(profile)


# -- the new window: what can't play is shown so, and not offered ---------------------------


@pytest.fixture
def next_backend(tmp_path: Path) -> Iterator[FakeApplication]:
    session = _session(_library(tmp_path / "home" / "Pictures" / "Wallpapers"))
    try:
        yield FakeApplication(session)
    finally:
        session.shutdown()


def test_the_picker_and_the_library_card_show_a_video_without_a_still_as_not_playable(
    next_backend: FakeApplication,
) -> None:
    session = next_backend.session
    video = next(item for item in session.library.items if item.kind is Kind.VIDEO)
    store = session.playlists
    store.create("Evening", entry_id="evening")
    store.add("evening", session.library.items[0].path, entry_id="e0")
    shown = Shown(next_backend, "evening")
    try:
        picker = shown.page.open_picker()
        assert picker is not None
        picker.toggle(str(video.path))
        assert picker.picked == [], "not offered: adding it would be refused"
        assert "playback can\u2019t use it yet, so it can\u2019t be added: it has no still" in (
            picker._cards[str(video.path)].get_tooltip_text() or ""
        )

        state = shown.state
        assert isinstance(state, RealAppState)
        card = widgets.WallpaperCard(state.wallpaper(str(video.path)), on_apply=lambda _w: None)
        assert card.apply_button is not None
        assert not card.apply_button.get_sensitive()
        assert card.apply_button.get_label() == "Apply", "not \u201cSkipped\u201d: it never played"
        assert "it has no still for playback to show yet" in (
            card.apply_button.get_tooltip_text() or ""
        )
    finally:
        shown.close()
