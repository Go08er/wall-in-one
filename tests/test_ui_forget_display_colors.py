"""Forget Display's colors step never undoes a newer choice (review Y-2), through the real app.

Forget Display stops the desktop colors following the display it forgets,
but only while they still do and displays are mirrored, where that choice
has no effect. It used to decide that when Forget was asked for and then
queue ``theme_source_connector=""`` regardless, after its stores: a newer
choice queued meanwhile (another display for the colors, or independent
displays) was erased, and independent displays with no colors' display were
turned back into mirrored ones (`config.Settings.validated`).

Each case: displays mirrored, the colors on detached DP-2 (or, for the
controls, on DP-3), DP-2 with its own playlist and a rule. The authoring lane
is held while a newer choice (colors on DP-1, mirrored or independent) and
Forget DP-2 are asked for, in either order; then the lane runs.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import REAL_IO_S  # noqa: E402
from tests.test_ui_next_editing import (  # noqa: E402
    first_scan,
    hold_the_lane,
    reports_of,
    wallpaper,
)
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    FakeRuntime,
    Step,
    first_frame_step,
    patient,
    sandboxed_runtime,
    settled,
)
from wall_in_one import config  # noqa: E402
from wall_in_one.library import displays, playlists, schedules  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402

MIRRORED = config.DISPLAY_MODE_MIRRORED
INDEPENDENT = config.DISPLAY_MODE_INDEPENDENT


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


def scheduling_of(window: NextWindow) -> RealScheduling:
    scheduling = window.state.scheduling
    assert isinstance(scheduling, RealScheduling)
    return scheduling


def choose(scheduling: RealScheduling, newer: str, chose: list[bool]) -> None:
    """The newer choice: colors on DP-1, displays mirrored or independent.

    ``independent-only`` switches to independent displays and nothing else,
    while the colors still follow DP-2, which the Displays page allows.
    """
    if newer == "independent-only":
        scheduling.choose_display_mode(INDEPENDENT, chose.append)
        return
    scheduling.choose_color_source("DP-1", chose.append)
    if newer == INDEPENDENT:
        scheduling.choose_display_mode(INDEPENDENT, chose.append)


@contextlib.contextmanager
def held(application: Application) -> Iterator[None]:
    """The authoring lane busy with one accepted task until the block ends."""
    gate = hold_the_lane(application)
    try:
        yield
    finally:
        gate.set()


def scenario(
    application: Application,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lanes: list[object],
    *,
    origin: str,
    newer: str,
    order: str,
    expected: tuple[str, str],
    said_kept: bool = False,
) -> Iterator[Step]:
    window = application._window
    assert isinstance(window, NextWindow)
    said = reports_of(window, monkeypatch)
    yield first_frame_step(window)
    yield patient(first_scan(application, window), REAL_IO_S)
    session = application.session
    made = session.playlists.create(
        "Dock",
        entries=[playlists.Entry(playlists.new_id(), wallpaper(tmp_path, "dawn.png"))],
    )
    session.displays.assign("DP-2", made.id)
    session.schedules.add(made.id, connector="DP-2", weekdays=("mon",))
    shared = session.schedules.add(made.id, weekdays=("wed",))
    assert application.update_settings_async(display_mode=MIRRORED, theme_source_connector=origin)
    application.schedule_edited()
    yield patient(("the setup to be saved", lambda: settled(application)), REAL_IO_S)
    assert (application.settings.display_mode, application.settings.theme_source_connector) == (
        MIRRORED,
        origin,
    )
    scheduling = scheduling_of(window)
    heard = len(said)
    chose: list[bool] = []
    forgot: list[bool] = []
    with held(application):
        if order == "choice-first":
            choose(scheduling, newer, chose)
            scheduling.forget_display("DP-2", forgot.append)
        else:
            scheduling.forget_display("DP-2", forgot.append)
            choose(scheduling, newer, chose)
    yield patient(
        ("the lane to run", lambda: bool(forgot) and bool(chose) and settled(application)),
        REAL_IO_S,
    )

    saved = config.load_strict()
    assert (saved.display_mode, saved.theme_source_connector) == expected, saved
    assert (
        application.settings.display_mode,
        application.settings.theme_source_connector,
    ) == expected
    assert (
        application.requested_settings.display_mode,
        application.requested_settings.theme_source_connector,
    ) == expected
    assert [rule.id for rule in schedules.Store.open().rules] == [shared.id], "DP-2's rule went"
    assert dict(displays.Store.open().all()) == {}, "and its own playlist"
    assert forgot == [True] and all(chose), (forgot, chose)
    lately = said[heard:]
    assert not any("nothing changed" in line or "partly" in line for line in lately), lately
    kept = [line for line in lately if "the desktop colors still follow it" in line]
    if said_kept:
        assert kept == [
            "Forgetting DP-2 removed its own playlist (“Dock”) and its 1 schedule "
            "rule, but the desktop colors still follow it: displays are now controlled "
            "separately, which needs a display for the colors. Choose another display for "
            "them, then forget DP-2 again."
        ], lately
    else:
        assert kept == [], lately
    yield from finish(application, window, lanes)


ORDERS = ["choice-first", "forget-first"]


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("newer", [MIRRORED, INDEPENDENT])
def test_a_newer_colors_choice_survives_forgetting_the_display_they_followed(
    runtime: FakeRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    newer: str,
    order: str,
) -> None:
    """Codex's four counterexamples. Before: the colors cleared, and the mode mirrored."""
    application = Application(ui="next")
    lanes: list[object] = []
    run(
        application,
        scenario(
            application,
            tmp_path,
            monkeypatch,
            lanes,
            origin="DP-2",
            newer=newer,
            order=order,
            expected=(newer, "DP-1"),
        ),
        lanes,
    )


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("newer", [MIRRORED, INDEPENDENT])
def test_a_newer_colors_choice_survives_forgetting_another_display(
    runtime: FakeRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    newer: str,
    order: str,
) -> None:
    """Codex's four controls: the colors followed DP-3, so Forget DP-2 never touched them."""
    application = Application(ui="next")
    lanes: list[object] = []
    run(
        application,
        scenario(
            application,
            tmp_path,
            monkeypatch,
            lanes,
            origin="DP-3",
            newer=newer,
            order=order,
            expected=(newer, "DP-1"),
        ),
        lanes,
    )


@pytest.mark.parametrize("order", ORDERS)
def test_forgetting_never_turns_newly_independent_displays_back_into_mirrored_ones(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    """Independent displays chosen while the colors still follow DP-2: they stay, and it's said.

    Before: the cleared colors' display made `Settings.validated` turn the
    newly independent displays back into mirrored ones, without a word.
    """
    application = Application(ui="next")
    lanes: list[object] = []
    run(
        application,
        scenario(
            application,
            tmp_path,
            monkeypatch,
            lanes,
            origin="DP-2",
            newer="independent-only",
            order=order,
            expected=(INDEPENDENT, "DP-2"),
            said_kept=True,
        ),
        lanes,
    )


def test_forgetting_the_colors_display_with_nothing_newer_still_clears_it(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard: with no other choice made, the colors stop following DP-2, as designed."""
    application = Application(ui="next")
    lanes: list[object] = []

    def plain() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_frame_step(window)
        yield patient(first_scan(application, window), REAL_IO_S)
        session = application.session
        made = session.playlists.create(
            "Dock",
            entries=[playlists.Entry(playlists.new_id(), wallpaper(tmp_path, "dawn.png"))],
        )
        session.displays.assign("DP-2", made.id)
        assert application.update_settings_async(
            display_mode=MIRRORED, theme_source_connector="DP-2"
        )
        application.schedule_edited()
        yield patient(("the setup to be saved", lambda: settled(application)), REAL_IO_S)
        heard = len(said)
        forgot: list[bool] = []
        scheduling_of(window).forget_display("DP-2", forgot.append)
        yield patient(("Forget DP-2", lambda: bool(forgot) and settled(application)), REAL_IO_S)
        saved = config.load_strict()
        assert (saved.display_mode, saved.theme_source_connector) == (MIRRORED, "")
        assert application.settings.theme_source_connector == ""
        assert application.requested_settings.theme_source_connector == ""
        assert forgot == [True] and said[heard:] == []
        assert dict(displays.Store.open().all()) == {}
        yield from finish(application, window, lanes)

    run(application, plain(), lanes)
