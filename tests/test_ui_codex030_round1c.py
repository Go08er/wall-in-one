"""After Codex 0.3.0 round 1, the last gaps, in the real application.

Library folders and Wallpaper Engine scanning: a change that takes away what
a playlist plays is refused at the head of the authoring lane, in the
settings write itself (`Application._prepare_settings_batch`), so both
windows get it; the new window's Settings page names the playlists before
asking. Each test reproduces a change that, on b478b0a, was saved and left a
playlist the compiler refuses.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_next_editing import hold_the_lane  # noqa: E402
from tests.test_ui_next_settings import Opened, _close, _open  # noqa: E402
from tests.test_ui_next_window import sandboxed_runtime  # noqa: E402
from tests.test_ui_playlists_polish import QuietLoader  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _application, _attach  # noqa: E402
from tests.test_ui_runtime import _close as _close_application  # noqa: E402
from wall_in_one import config, paths, runtime_compatibility, runtime_config  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import displays, playlists, scan, schedules  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.ui import playlists_page, preferences  # noqa: E402
from wall_in_one.ui.app import _Commands  # noqa: E402
from wall_in_one.ui.next.real_playlists import RealPlaylists  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402
from wall_in_one.ui.next.schedule_state import RuleView  # noqa: E402
from wall_in_one.ui.next.window import PUBLICATION_FAILED  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class Shown(NamedTuple):
    opened: Opened
    walls: Path  # dawn.png and dusk.jpg
    walls2: Path  # rain.png; "wallpapers" is a prefix of "wallpapers2" as text only
    rain: MediaItem
    loop: MediaItem  # a Workshop video
    dawn: MediaItem


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Shown]:
    """The new window's Settings page over two library folders and a Workshop video."""
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    walls, walls2 = tmp_path / "wallpapers", tmp_path / "wallpapers2"
    walls2.mkdir()
    (walls2 / "rain.png").write_bytes(b"placeholder image bytes")
    steam = tmp_path / "steam" / "123"
    steam.mkdir(parents=True)
    (steam / "loop.mp4").write_bytes(b"placeholder video bytes")
    config.update({"roots": (walls, walls2), "scan_workshop": True})
    opened = _open(monkeypatch)
    application = opened.application
    spin_until(lambda: application._library_scan_future is None, 10, what="no scan running")
    dawn = MediaItem(walls / "dawn.png", Kind.STILL, 5, 1)
    rain = MediaItem(walls2 / "rain.png", Kind.STILL, 5, 1)
    loop = MediaItem(steam / "loop.mp4", Kind.VIDEO, 5, 1, provider=scan.WORKSHOP_PROVIDER)
    items = (dawn, MediaItem(walls / "dusk.jpg", Kind.STILL, 5, 1), rain, loop)
    application.session.adopt_library(Library((walls, walls2), items), reconcile_workshop=False)
    settings = application.settings
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    try:
        yield Shown(opened, walls, walls2, rain, loop, dawn)
    finally:
        _close(opened)


def _playlist(shown: Shown, name: str, items: list[MediaItem]) -> playlists.Playlist:
    store = shown.opened.application.session.playlists
    made = store.create(name)
    for item in items:
        store.add(made.id, item.path)
    return store.find(made.id)


def _builds(shown: Shown) -> bool:
    application = shown.opened.application
    try:
        runtime_config.render(application.settings, application.session)
    except runtime_config.RuntimeConfigError:
        return False
    return True


# -- the new window ------------------------------------------------------------------------


def test_removing_a_folder_a_used_playlist_needs_is_refused_and_named_first(
    shown: Shown,
) -> None:
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.rain])
    application.update_settings(active_playlist=evening.id)
    page = opened.page

    dialog = page.confirm_remove(str(shown.walls2))
    assert dialog is not None
    assert dialog.get_heading() == "This folder can\u2019t be removed now"
    assert "\u201cEvening\u201d (used by the default) would have nothing left to play" in (
        dialog.get_body()
    )
    dialog.force_close()

    # Past the dialog, the head of the lane decides.
    assert page.access is not None
    page.access.remove_folder(str(shown.walls2))
    opened.wait("the removal to settle")
    assert config.load().roots == (shown.walls, shown.walls2), "nothing was removed"
    assert opened.reports[-1] == (
        "Library folders were not saved; nothing changed: that change to the library folders "
        "would take wallpapers out of the library: \u201cEvening\u201d (used by the default) "
        "would have nothing left to play"
    )
    assert _builds(shown)

    # "wallpapers" is not the folder "wallpapers2" is: removing it takes nothing Evening needs.
    page.access.remove_folder(str(shown.walls))
    opened.wait("the other removal", lambda: config.load().roots == (shown.walls2,))


def test_a_folder_removal_queued_behind_the_rule_that_needs_it_is_refused(
    shown: Shown,
) -> None:
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.rain])
    scheduling = opened.window.state.scheduling
    assert isinstance(scheduling, RealScheduling)
    assert opened.page.access is not None
    results: list[bool] = []

    gate = hold_the_lane(application)
    scheduling.save_rule("", RuleView("", evening.id), results.append)
    opened.page.access.remove_folder(str(shown.walls2))
    gate.set()
    opened.wait("both writes to settle", lambda: bool(results))

    assert results == [True], "the rule saved first"
    assert config.load().roots == (shown.walls, shown.walls2), "then the removal was refused"
    assert (
        "\u201cEvening\u201d (used by 1 schedule rule) would have nothing left to play"
        in (opened.reports[-1])
    )
    assert _builds(shown)


def test_turning_off_workshop_scanning_a_used_playlist_needs_is_refused_in_both_windows(
    shown: Shown,
) -> None:
    opened = shown.opened
    application = opened.application
    application.session.pairings.choose_still(shown.loop, shown.dawn.path)  # it plays
    scenes = _playlist(shown, "Scenes", [shown.loop])
    application.session.schedules.add(scenes.id)
    refusal = (
        "turning off Wallpaper Engine scanning would take wallpapers out of the library: "
        "\u201cScenes\u201d (used by 1 schedule rule) would have nothing left to play"
    )

    row = opened.row("scan_workshop")
    row.set_active(False)
    opened.wait("the new window's change to settle")
    assert config.load().scan_workshop is True
    assert opened.reports[-1] == f"Settings were not saved; nothing changed: {refusal}"
    spin_until(lambda: row.get_active(), what="the switch back on")

    classic = preferences.PreferencesPage(application)
    classic.apply_settings(application.settings)
    classic._workshop.set_active(False)
    opened.wait("the classic change to settle")
    assert config.load().scan_workshop is True
    assert opened.reports[-1] == f"Settings were not saved; nothing changed: {refusal}"
    assert _builds(shown)


# -- the classic window, and the batch -------------------------------------------------------


def test_removing_a_folder_a_used_playlist_needs_is_refused_in_classic_settings(
    shown: Shown,
) -> None:
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.rain])
    application.update_settings(active_playlist=evening.id)
    classic = preferences.PreferencesPage(application)
    classic.apply_settings(application.settings)

    classic._make_root_remover(shown.walls2)(Gtk.Button())
    opened.wait("the classic removal to settle")

    assert config.load().roots == (shown.walls, shown.walls2)
    assert opened.reports[-1].startswith(
        "Library folders were not saved; nothing changed: that change to the library folders"
    )
    assert _builds(shown)


def test_a_refused_folder_change_rolls_back_the_whole_batch_it_came_in(shown: Shown) -> None:
    """Changes made together save together: the refusal takes the opacity with it."""
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.rain])
    application.update_settings(active_playlist=evening.id)
    before = config.load().opacity
    errors: list[str] = []

    gate = hold_the_lane(application)
    application.update_settings_async(opacity=0.5, on_error=errors.append)
    application.update_settings_async(roots=(shown.walls,), on_error=errors.append)
    gate.set()
    opened.wait("the batch to settle")

    saved = config.load()
    assert (saved.opacity, saved.roots) == (before, (shown.walls, shown.walls2))
    assert application.requested_settings.opacity == before, "the controls go back too"
    assert len(errors) == 2 and all("Evening" in error for error in errors)
    assert _builds(shown)


# -- references by name, which older profiles may hold ---------------------------------------


def _by_name(shown: Shown, playlist: playlists.Playlist) -> None:
    """An older profile's references: the default, a rule and DP-1, each by name."""
    application = shown.opened.application
    application.update_settings(active_playlist=playlist.name.lower())  # names ignore case
    application.session.schedules.add(playlist.name)
    application.session.displays.assign("DP-1", playlist.name)
    assert _builds(shown), "the compiler resolves a playlist by name too"


def test_the_startup_repair_keeps_an_older_profiles_references_by_name(shown: Shown) -> None:
    """They were judged dangling and removed (the default became All media); now repointed."""
    application = shown.opened.application
    evening = _playlist(shown, "Evening", [shown.dawn])
    _by_name(shown, evening)
    application.session.schedules.add("Gone")  # names nothing: still removed

    result = application._repair_dangling_playlist_references()
    application._finish_playlist_reference_repair(result)

    assert config.load().active_playlist == evening.id
    assert [rule.playlist for rule in schedules.Store.open().rules] == [evening.id]
    assert displays.Store.open().playlist_for("DP-1") == evening.id
    assert application.settings.active_playlist == evening.id
    assert [rule.playlist for rule in application.session.schedules.rules] == [evening.id]
    assert application.session.displays.playlist_for("DP-1") == evening.id
    assert _builds(shown)


def test_renaming_a_playlist_named_by_name_keeps_its_references_in_both_windows(
    shown: Shown, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.dawn])
    _by_name(shown, evening)

    editing = opened.window.state.playlist_editing
    assert isinstance(editing, RealPlaylists)
    editing.rename_playlist(evening.id, "Night")
    opened.wait(
        "the new window's rename",
        lambda: application.session.playlists.find(evening.id).name == "Night",
    )
    assert config.load().active_playlist == evening.id, "the existing key, given the id"
    assert application.settings.active_playlist == evening.id
    assert [rule.playlist for rule in application.session.schedules.rules] == [evening.id]
    assert application.session.displays.playlist_for("DP-1") == evening.id
    assert _builds(shown)

    morning = _playlist(shown, "Morning", [shown.rain])
    application.session.schedules.add("Morning")
    monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
    classic = playlists_page.PlaylistsPage(application)
    classic._selected = morning.id
    classic.refresh(application.session)
    classic._rename("Dawn")
    opened.wait(
        "the classic rename", lambda: application.session.playlists.find(morning.id).name == "Dawn"
    )
    assert [rule.playlist for rule in application.session.schedules.rules] == [
        evening.id,
        morning.id,
    ]
    assert _builds(shown)


def test_deleting_a_playlist_named_by_name_takes_those_references_too(shown: Shown) -> None:
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.dawn])
    _by_name(shown, evening)
    editing = opened.window.state.playlist_editing
    assert isinstance(editing, RealPlaylists)

    editing.delete_playlist(evening.id)
    opened.wait("the deletion", lambda: application.session.playlists.get(evening.id) is None)

    assert config.load().active_playlist == ""
    assert application.session.schedules.rules == ()
    assert application.session.displays.all() == ()
    assert _builds(shown)


# -- files deleted outside the app ------------------------------------------------------------


def test_a_playlists_files_deleted_outside_the_app_are_reported_and_an_edit_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing can stop it; what follows is honest: the service keeps what worked, the app
    names the playlist and keeps saying so, and the next valid edit gets through."""
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    verbs: list[str] = []

    def runtime(verb: str, *_arguments: object, **_keywords: object) -> Response:
        verbs.append(verb)
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", runtime)
    root = tmp_path / "wallpapers"
    root.mkdir()
    for name in ("dawn.png", "dusk.png"):
        (root / name).write_bytes(b"placeholder image bytes")
    dawn = MediaItem(root / "dawn.png", Kind.STILL, 5, 1)
    dusk = MediaItem(root / "dusk.png", Kind.STILL, 5, 1)
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), (dawn, dusk)))
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    evening = application.session.playlists.create("Evening")
    application.session.playlists.add(evening.id, dawn.path)
    application.update_settings(active_playlist=evening.id)
    document = paths.runtime_config_path()

    def published() -> bool:
        return not application._runtime_compile_pending and application._library_scan_future is None

    try:
        assert application._publish_runtime_async()
        spin_until(lambda: published() and document.exists(), 10, what="the first publication")
        good = document.read_bytes()
        assert str(dawn.path).encode() in good
        assert application.publication_problem == ""

        dawn.path.unlink()  # outside the app
        application.refresh_library()
        spin_until(lambda: bool(application.publication_problem), 10, what="the refusal")

        problem = application.publication_problem
        assert "'Evening'" in problem and "no playable entries" in problem, problem
        assert problem in window.reports, "said once, and kept (`publication_problem`)"
        assert document.read_bytes() == good, "the service keeps the last setup that worked"

        reply: list[Response] = []
        server.dispatch(
            server.build_verb_table(_Commands(application)),
            Request("playlist-add", f"{evening.id} {dusk.path}").encode(),
            reply.append,
        )
        spin_until(
            lambda: bool(reply) and published() and application.publication_problem == "",
            10,
            what="the edit to get through",
        )
        assert reply[0].ok, reply
        assert str(dusk.path).encode() in document.read_bytes()
    finally:
        _close_application(application)


def test_the_new_window_keeps_saying_why_changes_arent_reaching_playback(shown: Shown) -> None:
    opened = shown.opened
    application = opened.application
    problem = (
        "default playlist refers to playlist 'Evening', which has no playable entries. "
        "Add media or choose another playlist; the existing runtime configuration was "
        "left untouched."
    )

    application._set_publication_problem(problem)
    banner = opened.window.state.banner()
    assert banner is not None and banner.title == f"{PUBLICATION_FAILED} {problem}"
    assert banner.button == "Playlists"
    opened.window.state.banner_activated(banner.action)
    assert opened.window.current_page is opened.window.pages["playlist"]

    application._set_publication_problem("")
    assert opened.window.state.banner() is None
