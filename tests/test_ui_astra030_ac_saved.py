"""Every "is this in use, can this be used?" judgement is made on what is saved at the write.

The second half of AC-1 and AC-2 (``test_ui_astra030_ac`` has the first): the
guards that keep the runtime configuration buildable used to judge by what
this process held, which is what it read when it started. Nothing watches
``settings.toml`` or the stores, so a default, a rule, a display assignment, a
playlist's entries or the library folders another program saved since were
not seen, and a write that broke them was let through.

Each guard is now asked again on the authoring worker, just before the write,
of one view read from disk there (`Session.saved_view`,
`runtime_config.asked_of_what_is_saved`). Each test here runs the real
application, has a second process save something, and then makes the gesture:
through the new window, the classic one, ``ctl`` on the authoring lane, and
``ctl`` in the headless service, where each applies. A still choice, a reset
and a removal reach the application through one call from both windows
(`prepare_still_pairing_mutation`, `prepare_pairing_reset`,
`remove_item_async`), which is the call ``ctl`` on the lane makes too.

Each fails on 7bdc1a6.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from tests import released  # noqa: E402
from tests.gtk_helpers import spin_until  # noqa: E402
from tests.released import released_source  # noqa: E402, F401
from tests.test_ui_astra030_ac import (  # noqa: E402
    _RELEASED_SAVES_A_DEFAULT,
    _builds_from_what_is_saved,
)
from tests.test_ui_codex030_round1b import (  # noqa: E402, F401
    GUARD,
    Profile,
    _classic_playlists,
    _lane_idle,
    _said,
    profiles,
)
from tests.test_ui_codex030_round1b import _playlist as _saved  # noqa: E402
from tests.test_ui_codex030_round1c import (  # noqa: E402, F401
    Shown,
    _playlist,
    shown,
    toolkit,
)
from wall_in_one import config, runtime_config  # noqa: E402
from wall_in_one.control import server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import displays, pairings, playlists, schedules  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402
from wall_in_one.ui import preferences  # noqa: E402
from wall_in_one.ui.app import _Commands  # noqa: E402
from wall_in_one.ui.next.real_playlists import RealPlaylists  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402

#: Another program: its own process, saving through the same files.
_ANOTHER_PROGRAM = r"""
import json
import sys
from pathlib import Path

from wall_in_one import config
from wall_in_one.library import displays, playlists, schedules

wanted = json.loads(sys.argv[1])
store = playlists.Store.open()
for name, sources in wanted.get("playlists", {}).items():
    made = store.create(name)
    for source in sources:
        store.add(made.id, Path(source))
for playlist in wanted.get("emptied", []):
    for entry in store.find(playlist).entries:
        store.remove_entry(playlist, entry.id)
settings = dict(wanted.get("settings", {}))
if "roots" in settings:
    settings["roots"] = tuple(Path(root) for root in settings["roots"])
if settings:
    config.update(settings)
for playlist in wanted.get("rules", []):
    schedules.Store.open().add(playlist)
for connector, playlist in wanted.get("assignments", {}).items():
    displays.Store.open().assign(connector, playlist)
for target, text in wanted.get("files", {}).items():
    Path(target).write_text(text, encoding="utf-8")
"""


def _another_program(**wanted: object) -> None:
    """A second process saves, and has finished before anything else happens here."""
    project = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(project / "src") + (os.pathsep + existing if existing else "")
    subprocess.run(
        [sys.executable, "-c", _ANOTHER_PROGRAM, json.dumps(wanted)],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _put_to_use(playlist: playlists.Playlist, use: str) -> str:
    """Another program puts ``playlist`` to use; the words a refusal names that use by."""
    if use == "default by name":
        # In another letter case: the compiler resolves it all the same.
        _another_program(settings={"active_playlist": playlist.name.lower()})
        return "the default"
    if use == "rule":
        _another_program(rules=[playlist.id])
        return "1 schedule rule"
    assert use == "assignment", use
    _another_program(assignments={"DP-1": playlist.id})
    return "DP-1\u2019s own playlist"


def _entries_saved(playlist: playlists.Playlist) -> list[str]:
    return [entry.id for entry in playlists.Store.open().find(playlist.id).entries]


def _reply(profile: Profile, verb: str, argument: str) -> Response:
    """One ``ctl`` verb through the real verb table, as the control socket hands it over.

    Given longer than `test_ui_codex030_round1b._ctl` gives: the worker reads
    what is saved, and may scan, before it answers.
    """
    replies: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(profile.application)),
        Request(verb, argument).encode(),
        replies.append,
    )
    spin_until(lambda: bool(replies), 30, what=f"{verb} to reply")
    return replies[0]


def _verb(shown: Shown, verb: str, argument: str) -> Response:  # noqa: F811
    """One ``ctl`` verb to the application behind the new window."""
    reply: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(shown.opened.application)),
        Request(verb, argument).encode(),
        reply.append,
    )
    shown.opened.wait(f"{verb} to settle", lambda: bool(reply))
    return reply[0]


# -- row 4: taking away what a playlist in use plays ---------------------------------------


@pytest.mark.parametrize(
    ("via", "use"),
    [
        ("ctl", "default by name"),
        ("ctl", "rule"),
        ("ctl", "assignment"),
        ("service", "default by name"),
        ("classic", "default by name"),
    ],
)
def test_the_last_wallpaper_of_a_playlist_another_program_put_to_use_stays(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    via: str,
    use: str,
) -> None:
    """Nothing here uses "Lone" when the application starts. Another program then
    makes it the default, a rule's or a display's. Taking its only wallpaper out
    has to see that use, which only the files know."""
    profile: Profile = profiles(service=via == "service")
    application = profile.application
    lone = _saved(profile, "Lone", [profile.dawn])
    words = _put_to_use(lone, use)
    held = runtime_config.playlist_uses(lone.id, application.settings, application.session)
    assert held == (), "this process knows nothing of it"

    if via == "classic":
        page = _classic_playlists(profile, monkeypatch, lone.id)
        page._make_remove("lone-0")(Gtk.Button())
        spin_until(lambda: _lane_idle(application), 30, what="the removal to settle")
        assert _said(profile, f"(used by {words}) would have nothing left to play")
    else:
        reply = _reply(profile, "playlist-remove", f"{lone.id} lone-0")
        assert not reply.ok and reply.kind == GUARD, reply
        assert f"(used by {words}) would have nothing left to play" in reply.message

    # Failed on 7bdc1a6: taken out, and a playlist in use had nothing to play.
    assert _entries_saved(lone) == ["lone-0"]


@pytest.mark.parametrize("use", ["default by name", "rule"])
def test_the_new_window_keeps_the_last_wallpaper_of_a_playlist_put_to_use_elsewhere(
    shown: Shown,  # noqa: F811
    use: str,
) -> None:
    """The new window's page has its own early answer, from what it shows. The
    one that counts is still the application's, on what is saved."""
    opened = shown.opened
    evening = _playlist(shown, "Evening", [shown.dawn])
    words = _put_to_use(evening, use)
    editing = opened.window.state.playlist_editing
    assert isinstance(editing, RealPlaylists)

    editing.remove_entries(evening.id, [evening.entries[0].id])
    opened.wait("the removal to settle")

    # Failed on 7bdc1a6: taken out, and the configuration no longer built.
    assert _entries_saved(evening) == [evening.entries[0].id]
    assert f"(used by {words}) would have nothing left to play" in opened.reports[-1]
    assert _builds_from_what_is_saved(shown)


def test_the_last_wallpaper_of_a_default_the_released_version_saved_stays(
    shown: Shown,  # noqa: F811
    released_source: Path,  # noqa: F811
) -> None:
    """Rule 4. The other program is 0.2.2 itself, saving the default by name."""
    evening = _playlist(shown, "Evening", [shown.dawn])
    done = released.run(released_source, _RELEASED_SAVES_A_DEFAULT, "Evening")
    assert done.returncode == 0, done.stderr
    assert config.load_strict().active_playlist == "Evening", "0.2.2 wrote it by name"

    reply = _verb(shown, "playlist-remove", f"{evening.id} {evening.entries[0].id}")

    assert not reply.ok and reply.kind == GUARD, reply
    assert "(used by the default) would have nothing left to play" in reply.message
    assert _entries_saved(evening) == [evening.entries[0].id]
    assert _builds_from_what_is_saved(shown)
    assert released.VERSION == "0.2.2"


@pytest.mark.parametrize("service", [False, True], ids=["lane", "service"])
@pytest.mark.parametrize("verb", ["still", "reset-pairing"])
def test_a_video_keeps_the_still_a_playlist_put_to_use_elsewhere_plays_through(
    profiles: Any,  # noqa: F811
    verb: str,
    service: bool,
) -> None:
    """A video plays through a chosen still, in a playlist whose only other
    wallpaper playback skips. Without the still the playlist still compiles, so
    nothing stops the still going while nothing uses the playlist. Another
    program makes it the default, and then going back to the worked-out still
    (there is none) or resetting the pairing would leave the default nothing to
    play."""
    profile: Profile = profiles(service=service)
    held = profile.application.session.pairings
    held.choose_still(profile.rain, profile.dusk.path)
    held.mark_borked(profile.dawn, "decoder crashed", "renderer-crash")
    moving = _saved(profile, "Moving", [profile.rain, profile.dawn])
    _put_to_use(moving, "default by name")

    argument = f"{profile.rain.path} default" if verb == "still" else str(profile.rain.path)
    reply = _reply(profile, verb, argument)

    # Failed on 7bdc1a6: the still was dropped.
    assert not reply.ok and reply.kind == GUARD, reply
    assert "“Moving” (used by the default) would have nothing left to play" in (reply.message)
    record = pairings.Store.open().get(pairings.Identity.of(profile.rain))
    assert record is not None and record.still == profile.dusk.path


@pytest.mark.parametrize("via", ["window", "ctl", "service"])
def test_the_only_wallpaper_of_a_playlist_put_to_use_elsewhere_is_not_removed(
    profiles: Any,  # noqa: F811
    via: str,
) -> None:
    """The removal's dry run takes the default and the uses from what is saved."""
    profile: Profile = profiles(service=via == "service")
    application = profile.application
    lone = _saved(profile, "Lone", [profile.dawn])
    _put_to_use(lone, "default by name")

    if via == "window":
        finished: list[RemovalResult] = []
        assert application.remove_item_async(profile.dawn, trash=True, finish=finished.append)
        spin_until(lambda: _lane_idle(application), 30, what="the removal to settle")
        assert not any(result.committed for result in finished)
        said = " ".join([*(result.error_message for result in finished), *profile.window.reports])
    else:
        reply = _reply(profile, "remove", str(profile.dawn.path))
        assert not reply.ok and reply.kind == GUARD, reply
        said = reply.message

    # Failed on 7bdc1a6: removed, and the default had nothing to play.
    assert profile.dawn.path.exists()
    assert "dawn was not removed: “Lone” (used by the default)" in said, said
    assert _entries_saved(lone) == ["lone-0"]


@pytest.mark.parametrize("window", ["new", "classic"])
def test_removing_a_folder_counts_a_rule_another_program_saved(
    shown: Shown,  # noqa: F811
    window: str,
) -> None:
    """The folder dry run takes what uses each playlist from what is saved too.

    "Wet" plays rain, from the second folder, and holds dawn, which playback
    skips. Without rain it still compiles, so nothing stops the folder going
    while nothing uses "Wet". A rule another program saved does use it, and
    then it would have nothing left to play."""
    opened = shown.opened
    application = opened.application
    application.session.pairings.mark_borked(shown.dawn, "decoder crashed", "renderer-crash")
    wet = _playlist(shown, "Wet", [shown.rain, shown.dawn])  # rain is in the second folder
    _put_to_use(wet, "rule")

    if window == "new":
        access = opened.page.access
        assert access is not None
        access.remove_folder(str(shown.walls2))
    else:
        classic = preferences.PreferencesPage(application)
        classic.apply_settings(application.settings)
        classic._make_root_remover(shown.walls2)(Gtk.Button())
    opened.wait("the folder gesture to settle")

    # Failed on 7bdc1a6: the folder went, and the rule's playlist with it.
    assert config.load_strict().roots == (shown.walls, shown.walls2)
    assert "“Wet” (used by 1 schedule rule) would have nothing left to play" in (opened.reports[-1])


def test_a_use_that_can_not_be_read_is_not_taken_as_none(
    profiles: Any,  # noqa: F811
) -> None:
    """The schedule file is damaged by the time of the gesture. A store opened on
    it holds no rule, and "nothing uses the playlist" from that would be a
    guess: whether a rule used it is not known, so its last wallpaper stays."""
    profile: Profile = profiles()
    lone = _saved(profile, "Lone", [profile.dawn])
    _another_program(files={str(schedules.state_path()): "{ half a schedule"})

    reply = _reply(profile, "playlist-remove", f"{lone.id} lone-0")

    # Failed on 7bdc1a6: taken out, judged by the rules this process held (none).
    assert not reply.ok and reply.kind == "unreadable", reply
    assert "what is saved could not be read, so this change could not be checked" in reply.message
    assert _entries_saved(lone) == ["lone-0"]


# -- the library a judgement goes by -------------------------------------------------------


def test_a_folder_another_program_added_is_not_removed_from_under_its_playlist(
    shown: Shown,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another program adds a folder and a playlist, used by nothing, that plays
    only from it. The window picks the folder up with its next save, and has not
    scanned it yet. Removing that folder then must not pass because the library
    in memory, which never saw the folder, has the playlist as broken already:
    on disk it is fine, and the removal is what would break it."""
    opened = shown.opened
    application = opened.application
    outside, chosen = tmp_path / "outside", tmp_path / "chosen"
    for folder in (outside, chosen):
        folder.mkdir()
        (folder / "sea.png").write_bytes(b"placeholder image bytes")
    _another_program(
        playlists={"Sea": [str(outside / "sea.png")]},
        settings={"roots": [str(shown.walls), str(shown.walls2), str(outside)]},
    )
    monkeypatch.setattr(application, "refresh_library", lambda *_a, **_k: None)  # not scanned yet
    access = opened.page.access
    assert access is not None
    access.add_folder(str(chosen))
    opened.wait("the first gesture to settle")
    everything = (shown.walls, shown.walls2, outside, chosen)
    assert config.load_strict().roots == everything
    assert application.session.library.find(outside / "sea.png") is None, "not scanned yet"

    access.remove_folder(str(outside))
    opened.wait("the removal to settle")

    # Failed on afa71e5 (and on 7bdc1a6, where the first gesture had already
    # dropped the folder): allowed, and a fresh start could not build "Sea".
    assert config.load_strict().roots == everything
    assert "“Sea” would hold only wallpapers playback can\u2019t use" in opened.reports[-1]


@pytest.mark.parametrize("via", ["new", "ctl"])
def test_a_playlist_is_judged_by_the_folders_that_are_saved(
    shown: Shown,  # noqa: F811
    via: str,
) -> None:
    """The default plays dawn and rain, from two folders. Another program takes
    rain's folder out of ``settings.toml``. The library in memory still lists
    rain, but the next scan won't: dawn is all the default has, and it stays."""
    opened = shown.opened
    application = opened.application
    mixed = _playlist(shown, "Mixed", [shown.dawn, shown.rain])
    application.update_settings(active_playlist=mixed.id)
    _another_program(settings={"roots": [str(shown.walls)]})
    assert application.session.library.find(shown.rain.path) is not None, "still listed here"
    dawn = mixed.entries[0].id

    if via == "new":
        editing = opened.window.state.playlist_editing
        assert isinstance(editing, RealPlaylists)
        editing.remove_entries(mixed.id, [dawn])
        opened.wait("the removal to settle")
        said = " ".join(opened.reports)
    else:
        said = _verb(shown, "playlist-remove", f"{mixed.id} {dawn}").message

    # Failed on 7bdc1a6: taken out, leaving the default only a wallpaper the
    # saved folders no longer hold.
    assert len(_entries_saved(mixed)) == 2
    assert "(used by the default) would have nothing left to play" in said, said


def test_an_entry_is_not_excused_as_broken_when_its_file_is_on_disk(
    shown: Shown,  # noqa: F811
) -> None:
    """A playlist nothing uses holds a wallpaper that is gone and one whose file
    another program has put back since the last scan. The library in memory has
    both as missing, so the playlist looks broken already and taking an entry out
    looks like tidying. On disk the second one plays: taking it out is what would
    leave the playlist holding only what the compiler drops."""
    back = shown.walls / "back.png"
    sea = _playlist(shown, "Sea", [])
    store = shown.opened.application.session.playlists
    store.add(sea.id, shown.walls / "gone.png")
    store.add(sea.id, back)
    sea = store.find(sea.id)
    _another_program(files={str(back): "placeholder image bytes"})

    reply = _verb(shown, "playlist-remove", f"{sea.id} {sea.entries[1].id}")

    # Failed on 7bdc1a6: taken out, and a fresh start could not build "Sea".
    assert not reply.ok and reply.kind == GUARD, reply
    assert "“Sea” would hold only wallpapers playback can\u2019t use" in reply.message
    assert len(_entries_saved(sea)) == 2


# -- row 8: putting a playlist to use -------------------------------------------------------


@pytest.mark.parametrize("via", ["a window", "the service"])
def test_a_default_another_program_emptied_is_not_saved(
    profiles: Any,  # noqa: F811
    via: str,
) -> None:
    """ "Lone" has a wallpaper as far as this process knows. Another program takes
    it out. Making "Lone" the default (both windows save it through
    `update_settings_async`) is judged by the playlist that is saved."""
    profile: Profile = profiles(service=via == "the service")
    application = profile.application
    lone = _saved(profile, "Lone", [profile.dawn])
    _another_program(emptied=[lone.id])
    assert len(application.session.playlists.find(lone.id).entries) == 1, "not known here"

    said: list[str] = []
    if via == "a window":
        application.update_settings_async(
            active_playlist=lone.id,
            on_success=lambda _settings: said.append("saved"),
            on_error=said.append,
        )
        spin_until(lambda: bool(said) and _lane_idle(application), 30, what="the save to settle")
    else:
        with pytest.raises(config.ConfigError) as refused:
            application.update_settings(active_playlist=lone.id)
        said.append(str(refused.value))

    # Failed on 7bdc1a6: saved, and the default had nothing to play.
    assert "“Lone” has nothing to play yet" in said[-1], said
    assert config.load_strict().active_playlist == ""


@pytest.mark.parametrize("service", [False, True], ids=["lane", "service"])
@pytest.mark.parametrize("verb", ["schedule-add", "display-assign"])
def test_a_playlist_another_program_emptied_is_not_given_a_rule_or_a_display(
    profiles: Any,  # noqa: F811
    verb: str,
    service: bool,
) -> None:
    profile: Profile = profiles(service=service)
    lone = _saved(profile, "Lone", [profile.dawn])
    _another_program(emptied=[lone.id])

    argument = lone.id if verb == "schedule-add" else f"DP-1 {lone.id}"
    reply = _reply(profile, verb, argument)

    # Failed on 7bdc1a6: saved, naming a playlist with nothing to play.
    assert not reply.ok and reply.kind == GUARD, reply
    assert "“Lone” has nothing to play yet" in reply.message
    assert schedules.Store.open().rules == ()
    assert displays.Store.open().all() == ()


def test_the_new_window_does_not_give_a_display_a_playlist_emptied_elsewhere(
    shown: Shown,  # noqa: F811
) -> None:
    opened = shown.opened
    evening = _playlist(shown, "Evening", [shown.dawn])
    _another_program(emptied=[evening.id])
    scheduling = opened.window.state.scheduling
    assert isinstance(scheduling, RealScheduling)
    settled: list[bool] = []

    scheduling.assign_display("DP-1", evening.id, settled.append)
    opened.wait("the assignment to settle", lambda: bool(settled))

    # Failed on 7bdc1a6: assigned.
    assert settled == [False]
    assert displays.Store.open().all() == ()
    assert "“Evening” has nothing to play yet" in opened.reports[-1]
