"""Rename and removals when a store fails or can't be read, through the real application.

Same sandbox as `test_ui_next_forget_display`: `Application.run` with the new
window, two wallpapers, a fake runtime, native faults on disposable files
(a store's private lock, or the store itself made unreadable by its mode),
put back afterwards.

* A playlist an older profile names by name is renamed by first giving each
  reference its id, then the name. A store failing partway says exactly what
  was saved, never "nothing changed", and what was saved is published.
* Nothing is written for a rename while a store involved can't be read.
* Removing a rule, or a star, from a file that can't be read is refused and
  said, through either window and through ``ctl``, instead of removing
  nothing and calling it done. The classic window renames through the same
  preparer, so its partial rename is covered here too.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.test_ui_next_editing import first_scan, reports_of, wallpaper  # noqa: E402
from tests.test_ui_next_forget_display import (  # noqa: E402
    after_the_first_schedule_commit,
    lock_of,
    unreadable,
)
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    FakeRuntime,
    Step,
    application_lanes,
    control,
    sandboxed_runtime,
    settled,
)
from tests.test_ui_playlists_polish import QuietLoader  # noqa: E402
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.library import displays, favourites, playlists, schedules  # noqa: E402
from wall_in_one.ui import playlists_page  # noqa: E402
from wall_in_one.ui.app import Application, PlaylistRenameResult, rename_partly_saved  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def dock(application: Application, tmp_path: Path) -> playlists.Playlist:
    return application.session.playlists.create(
        "Dock",
        entries=[playlists.Entry(playlists.new_id(), wallpaper(tmp_path, "dawn.png"))],
    )


def window_of(application: Application) -> NextWindow:
    window = application._window
    assert isinstance(window, NextWindow)
    return window


# -- rename ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("saved", "said"),
    [
        (("1 schedule rule",), "1 schedule rule now names it by its id"),
        (("the default playlist",), "the default playlist now names it by its id"),
        (("2 schedule rules",), "2 schedule rules now name it by its id"),
        (
            ("the default playlist", "1 schedule rule", "1 display\u2019s own playlist"),
            "the default playlist, 1 schedule rule and 1 display\u2019s own playlist now name it",
        ),
        (("2 displays\u2019 own playlists",), "2 displays\u2019 own playlists now name it"),
    ],
)
def test_a_partial_rename_is_said_in_plain_words(saved: tuple[str, ...], said: str) -> None:
    before = playlists.Playlist(id="p1", name="Dock")
    message = rename_partly_saved(PlaylistRenameResult(before, None, None, saved, "local-io: x"))
    assert message.startswith("\u201cDock\u201d was not renamed: local-io: x. Saved before that: ")
    assert said in message, message
    assert message.endswith("Rename it again to finish.")


def test_a_rename_stopped_partway_says_what_it_saved_publishes_it_and_can_finish(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older profile's rule and display name "Dock" by name; the display store fails."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = window_of(application)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        session = application.session
        made = dock(application, tmp_path)
        rule = session.schedules.add("Dock", weekdays=("mon",))  # by name
        session.displays.assign("DP-2", "Dock")  # by name
        application.playlists_changed()
        yield "the baseline's publication", lambda: settled(application)
        generation = application._runtime_compiled_generation
        editing = window.state.playlist_editing
        assert editing is not None
        heard = len(said)
        with after_the_first_schedule_commit(lock_of(displays.state_path())):
            editing.rename_playlist(made.id, "Harbour")
            yield "the rename to stop", lambda: settled(application) and len(said) > heard
        message = said[-1]
        assert "nothing changed" not in message, message
        assert message.startswith("“Dock” was not renamed: local-io"), message
        assert "Saved before that: 1 schedule rule now names it by its id" in message, message
        stored = playlists.Store.open().get(made.id)
        assert stored is not None and stored.name == "Dock"
        assert [found.playlist for found in schedules.Store.open().rules] == [made.id]
        assert dict(displays.Store.open().all()) == {"DP-2": "Dock"}
        assert application._runtime_compiled_generation > generation, "what was saved, published"
        assert rule.id in paths.runtime_config_path().read_text()

        editing.rename_playlist(made.id, "Harbour")  # the same rename, the store back
        yield (
            "the retried rename",
            lambda: (
                settled(application)
                and (
                    (renamed := playlists.Store.open().get(made.id)) is not None
                    and renamed.name == "Harbour"
                )
            ),
        )
        assert dict(displays.Store.open().all()) == {"DP-2": made.id}
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_the_classic_rename_stopped_partway_says_what_it_saved_and_publishes_it(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classic page renames through the same preparer and adopts the same result."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = window_of(application)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        session = application.session
        made = dock(application, tmp_path)
        session.schedules.add("Dock", weekdays=("mon",))  # by name
        session.displays.assign("DP-2", "Dock")  # by name
        application.playlists_changed()
        yield "the baseline's publication", lambda: settled(application)
        generation = application._runtime_compiled_generation
        monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
        classic = playlists_page.PlaylistsPage(application)
        classic._selected = made.id
        classic.refresh(session)
        heard = len(said)
        with after_the_first_schedule_commit(lock_of(displays.state_path())):
            classic._rename("Harbour")
            yield "the classic rename to stop", lambda: settled(application) and len(said) > heard
        message = said[-1]
        assert message.startswith("\u201cDock\u201d was not renamed: local-io"), message
        assert "Saved before that: 1 schedule rule now names it by its id" in message, message
        assert [found.playlist for found in schedules.Store.open().rules] == [made.id]
        assert dict(displays.Store.open().all()) == {"DP-2": "Dock"}
        assert application._runtime_compiled_generation > generation, "what was saved, published"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_a_rename_writes_nothing_while_a_store_it_reads_is_unreadable(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default names "Dock" by name; schedules.json can't be read: refused first."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = window_of(application)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        made = dock(application, tmp_path)
        application.session.schedules.add(made.id, weekdays=("mon",))
        # An older profile's default, by name, as its file holds it; the
        # application takes it as it takes any change to the file.
        config.update({"active_playlist": "Dock"})
        application._on_settings_changed(config.load())
        yield "the default to be adopted", lambda: settled(application)
        assert application.settings.active_playlist == "Dock"
        settings_bytes = paths.settings_path().read_bytes()
        editing = window.state.playlist_editing
        assert editing is not None
        heard = len(said)
        with unreadable(schedules.state_path()):
            editing.rename_playlist(made.id, "Harbour")
            yield "the refusal", lambda: settled(application) and len(said) > heard
        message = said[-1]
        assert message.startswith("“Dock” was not renamed; nothing changed:"), message
        assert "schedules.json can't be read" in message, message
        assert paths.settings_path().read_bytes() == settings_bytes, "the default kept its name"
        stored = playlists.Store.open().get(made.id)
        assert stored is not None and stored.name == "Dock"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


# -- removals from a file that can't be read ---------------------------------------------------


def test_removing_a_rule_from_an_unreadable_schedule_is_refused_not_done(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = window_of(application)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        made = dock(application, tmp_path)
        rule = application.session.schedules.add(made.id, weekdays=("mon",))
        application.schedule_edited()
        yield "the rule's publication", lambda: settled(application)
        scheduling = window.state.scheduling
        assert scheduling is not None
        outcomes: list[bool] = []
        with unreadable(schedules.state_path()):
            scheduling.remove_rule(rule.id, outcomes.append)
            yield "the removal to settle", lambda: bool(outcomes) and settled(application)
        assert outcomes == [False]
        assert "nothing changed" in said[-1] and "schedules.json can't be read" in said[-1]
        assert [kept.id for kept in schedules.Store.open().rules] == [rule.id]
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_unstarring_from_an_unreadable_list_is_refused_and_the_star_stays(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = window_of(application)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        wid = wallpaper(tmp_path, "dawn.png")
        window.state.toggle_favorite(wid)
        yield "the star", lambda: settled(application) and window.state.wallpaper(wid).favorite
        listed = favourites.state_path().read_bytes()
        heard = len(said)
        with unreadable(favourites.state_path()):
            window.state.toggle_favorite(wid)
            yield "the refusal", lambda: settled(application) and len(said) > heard
        assert "favourites.json can't be read" in said[-1], said[-1]
        assert window.state.wallpaper(wid).favorite, "the star stays"
        assert favourites.state_path().read_bytes() == listed
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_the_classic_window_refuses_and_says_so_too(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classic's unstar and rule removal: the same store refusals, said in its toast."""
    application = Application(ui="classic")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, MainWindow)
        said: list[str] = []
        shown = window.report

        def report(message: str) -> None:
            said.append(message)
            shown(message)

        monkeypatch.setattr(window, "report", report)
        yield "the first activation", lambda: settled(application) and window._playable == 2
        item = application.session.library.find(Path(wallpaper(tmp_path, "dawn.png")))
        assert item is not None
        window._on_favourite(item, True)
        yield (
            "the star",
            lambda: settled(application) and item.path in favourites.Store.open().paths,
        )
        made = dock(application, tmp_path)
        rule = application.session.schedules.add(made.id, weekdays=("mon",))
        application.schedule_edited()
        yield "the rule's publication", lambda: settled(application)

        heard = len(said)
        with unreadable(favourites.state_path()):
            window._on_favourite(item, False)
            yield "the unstar's refusal", lambda: settled(application) and len(said) > heard
        assert said[-1].startswith("dawn was not unstarred; nothing changed: unreadable"), said
        assert "favourites.json can't be read" in said[-1], said[-1]
        assert item.path in favourites.Store.open().paths, "the star stays"

        heard = len(said)
        with unreadable(schedules.state_path()):
            window._schedules_page._make_remove(rule.id)(Gtk.Button())
            yield "the removal's refusal", lambda: settled(application) and len(said) > heard
        assert said[-1].startswith("unreadable: schedules.json can't be read"), said[-1]
        assert [kept.id for kept in schedules.Store.open().rules] == [rule.id]
        yield "every tail to settle", lambda: settled(application)
        lanes.extend(application_lanes(application))
        window.close()

    run(application, scenario(), lanes)


def test_ctl_removals_from_unreadable_files_answer_unreadable(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control verbs classic and scripts share: refused, with the kind to branch on."""
    application = Application(ui="next")
    lanes: list[object] = []
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")

    def scenario() -> Iterator[Step]:
        window = window_of(application)
        yield first_scan(application, window)
        made = dock(application, tmp_path)
        rule = application.session.schedules.add(made.id, weekdays=("mon",))
        dawn = wallpaper(tmp_path, "dawn.png")
        application.session.favourites.add(Path(dawn))
        application.schedule_edited()
        yield "the setup's publication", lambda: settled(application)
        with contextlib.ExitStack() as faults:
            faults.enter_context(unreadable(schedules.state_path()))
            faults.enter_context(unreadable(favourites.state_path()))
            removed = control(requests, "schedule-remove", rule.id)
            yield "ctl schedule-remove", removed.done
            unstarred = control(requests, "unfavourite", dawn)
            yield "ctl unfavourite", unstarred.done
        for answer in (removed.result(), unstarred.result()):
            assert not answer.ok and answer.kind == "unreadable", answer
        assert [kept.id for kept in schedules.Store.open().rules] == [rule.id]
        assert Path(dawn) in favourites.Store.open().paths
        yield "every tail to settle", lambda: settled(application)
        yield from finish(application, window, lanes)

    try:
        run(application, scenario(), lanes)
    finally:
        requests.shutdown(wait=True)
