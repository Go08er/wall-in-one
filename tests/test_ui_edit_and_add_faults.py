"""Edits and additions on a store file that can't be read, through the real application.

Same sandbox as `test_ui_rename_and_removal_faults`: `Application.run`, two
wallpapers, a fake runtime, and a native fault on a disposable file (the store
made unreadable by its mode, put back afterwards).

* An edit of a rule or a playlist says the file can't be read, not that the
  rule or playlist doesn't exist.
* An addition (a star) is refused and says so, instead of moving the list
  aside and writing a new one with only that star in it.
* The same in the classic window, and through ``ctl``, with the kind to
  branch on.
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

from tests.test_ui_next_editing import first_scan, item_at, reports_of, wallpaper  # noqa: E402
from tests.test_ui_next_forget_display import unreadable  # noqa: E402
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    FakeRuntime,
    Step,
    application_lanes,
    control,
    sandboxed_runtime,
    settled,
)
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import displays, favourites, pairings, playlists, schedules  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
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


def nothing_beside(target: Path) -> bool:
    """No ``.broken`` copy (or anything else) was made next to ``target``."""
    return not list(target.parent.glob(target.name + ".broken*"))


def test_the_new_window_says_an_unreadable_file_cannot_be_read(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        made = dock(application, tmp_path)
        rule = application.session.schedules.add(made.id, weekdays=("mon",))
        dusk = wallpaper(tmp_path, "dusk.jpg")
        application.session.favourites.add(Path(dusk))
        application.schedule_edited()
        yield "the setup's publication", lambda: settled(application)
        scheduling = window.state.scheduling
        editing = window.state.playlist_editing
        assert scheduling is not None and editing is not None

        # An edit of a rule: before, "no-such-rule: no rule …".
        outcomes: list[bool] = []
        held = schedules.state_path().read_bytes()
        with unreadable(schedules.state_path()):
            scheduling.enable_rule(rule.id, False, outcomes.append)
            yield "the rule edit to settle", lambda: bool(outcomes) and settled(application)
        assert outcomes == [False]
        assert said[-1].startswith(
            "Turning the rule off was not saved; nothing changed: unreadable: "
            "schedules.json can't be read"
        ), said[-1]
        assert schedules.state_path().read_bytes() == held

        # An edit of a playlist: before, "no-such-playlist: no playlist called …".
        heard = len(said)
        held = playlists.state_path().read_bytes()
        with unreadable(playlists.state_path()):
            editing.set_playlist_interval(made.id, 60)
            yield "the interval's refusal", lambda: settled(application) and len(said) > heard
        assert said[-1].startswith(
            "The interval of “Dock” was not saved; nothing changed: unreadable: "
            "playlists.json can't be read"
        ), said[-1]
        assert playlists.state_path().read_bytes() == held

        # A star: before, the list moved aside and a new one holding only dawn.
        heard = len(said)
        dawn = wallpaper(tmp_path, "dawn.png")
        held = favourites.state_path().read_bytes()
        with unreadable(favourites.state_path()):
            window.state.toggle_favorite(dawn)
            yield "the star's refusal", lambda: settled(application) and len(said) > heard
        assert (
            "could not be saved as a favorite: unreadable: favourites.json can't be read"
            in (said[-1])
        ), said[-1]
        assert favourites.state_path().read_bytes() == held
        assert nothing_beside(favourites.state_path())
        assert favourites.Store.open().paths == frozenset({Path(dusk)}), "the list as it was"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_the_classic_window_says_so_too(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        made = dock(application, tmp_path)
        rule = application.session.schedules.add(made.id, weekdays=("mon",))
        dusk = wallpaper(tmp_path, "dusk.jpg")
        application.session.favourites.add(Path(dusk))
        application.schedule_edited()
        yield "the setup's publication", lambda: settled(application)

        # The rule's on/off switch: before, "no-such-rule: no rule …".
        heard = len(said)
        switch = Adw.SwitchRow()
        with unreadable(schedules.state_path()):
            window._schedules_page._make_enabled(rule.id)(switch, None)
            yield "the switch's refusal", lambda: settled(application) and len(said) > heard
        assert said[-1].startswith("unreadable: schedules.json can't be read"), said[-1]
        assert [found.enabled for found in schedules.Store.open().rules] == [True]

        # A star: before, the list moved aside and a new one holding only dawn.
        heard = len(said)
        dawn = item_at(application, wallpaper(tmp_path, "dawn.png"))
        held = favourites.state_path().read_bytes()
        with unreadable(favourites.state_path()):
            window._on_favourite(dawn, True)
            yield "the star's refusal", lambda: settled(application) and len(said) > heard
        assert said[-1].startswith(
            "dawn was not starred; nothing changed: unreadable: favourites.json can't be read"
        ), said[-1]
        assert favourites.state_path().read_bytes() == held
        assert nothing_beside(favourites.state_path())
        yield "every tail to settle", lambda: settled(application)
        lanes.extend(application_lanes(application))
        window.close()

    run(application, scenario(), lanes)


def test_ctl_edits_and_additions_on_unreadable_files_answer_unreadable(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verbs scripts and classic share: refused, with the kind to branch on."""
    application = Application(ui="next")
    lanes: list[object] = []
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        session = application.session
        made = dock(application, tmp_path)
        rule = session.schedules.add(made.id, weekdays=("mon",))
        dawn = wallpaper(tmp_path, "dawn.png")
        dusk = wallpaper(tmp_path, "dusk.jpg")
        session.favourites.add(Path(dusk))
        session.displays.assign("DP-1", made.id)
        session.pairings.choose_mode(item_at(application, dusk), pairings.Mode.DARK)
        application.schedule_edited()
        yield "the setup's publication", lambda: settled(application)
        stores = (
            schedules.state_path(),
            playlists.state_path(),
            favourites.state_path(),
            displays.state_path(),
            pairings.state_path(),
        )
        held = {target: target.read_bytes() for target in stores}
        asked = (
            ("favourite", dawn),
            ("playlist-new", "Evening"),
            ("playlist-add", f"{made.id} {dusk}"),
            ("display-assign", f"DP-2 {made.id}"),
            ("schedule-name", f"{rule.id} Mornings"),
            ("palette", f"{dawn} {pairings.PalettePolicy().encode()}"),
        )
        answers: list[tuple[str, Response]] = []
        with contextlib.ExitStack() as faults:
            for target in stores:
                faults.enter_context(unreadable(target))
            for verb, argument in asked:
                asking = control(requests, verb, argument)
                yield f"ctl {verb}", asking.done
                answers.append((verb, asking.result()))
        for verb, answer in answers:
            assert not answer.ok and answer.kind == "unreadable", (verb, answer)
            assert "can't be read" in answer.message, (verb, answer)
        for target in stores:
            assert target.read_bytes() == held[target], target
            assert nothing_beside(target), target
        yield "every tail to settle", lambda: settled(application)
        yield from finish(application, window, lanes)

    try:
        run(application, scenario(), lanes)
    finally:
        requests.shutdown(wait=True)
