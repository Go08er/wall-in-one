"""Codex's 0.3.0 round-1 findings P-1 and P-2, in the real application with the lane held.

Each runs `Application.run` with the new window over a sandboxed profile
(``dawn.png`` and ``dusk.jpg``) and a fake runtime, as ``test_ui_next_editing``
does, holds the authoring lane, queues the review's two gestures in the
review's order, then lets go: the second write is checked when it reaches
the head of the lane, against what the first one saved.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

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
    sandboxed_runtime,
    settled,
)
from wall_in_one import config, runtime_config  # noqa: E402
from wall_in_one.library import playlists, schedules  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.real_playlists import RealPlaylists  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402
from wall_in_one.ui.next.schedule_state import RuleView  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


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


def _lone(tmp_path: Path) -> tuple[str, str]:
    """An unused playlist holding only ``dawn``: (its id, its one entry's id)."""
    store = playlists.Store.open()
    made = store.create("Lone")
    saved = store.add(made.id, Path(wallpaper(tmp_path, "dawn.png")), entry_id="dawn-entry")
    return saved.id, "dawn-entry"


def test_p1_a_default_queued_behind_the_removal_that_empties_it_is_refused(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review: the entry removal emptied P, then Schedule's default saved P anyway."""
    pid, entry = _lone(tmp_path)
    application = Application(ui="next")
    lanes: list[object] = []
    results: list[bool] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        editing = window.state.playlist_editing
        scheduling = window.state.scheduling
        assert isinstance(editing, RealPlaylists) and isinstance(scheduling, RealScheduling)

        gate = hold_the_lane(application)
        editing.remove_entries(pid, [entry])
        scheduling.choose_default_playlist(pid, results.append)
        gate.set()
        yield "both writes to settle", lambda: settled(application) and bool(results)

        assert results == [False], "the default was refused at the head of the lane"
        emptied = playlists.Store.open().get(pid)
        assert emptied is not None and emptied.entries == (), "the removal ran first"
        assert config.load_strict().active_playlist == ""
        assert application.settings.active_playlist == ""
        assert any("nothing changed" in text and "nothing to play" in text for text in said)
        runtime_config.render(application.settings, application.session)  # still buildable
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_p2_a_removal_queued_behind_the_rule_that_uses_its_playlist_is_refused(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review: a rule saved P while the removal of P's only wallpaper waited."""
    pid, entry = _lone(tmp_path)
    dawn = wallpaper(tmp_path, "dawn.png")
    application = Application(ui="next")
    lanes: list[object] = []
    results: list[bool] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        scheduling = window.state.scheduling
        editing = window.state.editing
        assert isinstance(scheduling, RealScheduling) and editing is not None

        gate = hold_the_lane(application)
        scheduling.save_rule("", RuleView("", pid), results.append)
        editing.remove_wallpapers([dawn])
        gate.set()
        yield (
            "the rule and the refused removal",
            lambda: (
                settled(application)
                and bool(results)
                and any("was not removed" in text for text in said)
            ),
        )

        assert results == [True], "the rule saved"
        assert [rule.playlist for rule in schedules.Store.open().rules] == [pid]
        assert Path(dawn).exists(), "nothing was moved to the Trash"
        kept = playlists.Store.open().get(pid)
        assert kept is not None and [e.id for e in kept.entries] == [entry]
        removed = next(text for text in said if "was not removed" in text)
        assert "“Lone”" in removed and "nothing left to play" in removed
        runtime_config.render(application.settings, application.session)  # still buildable
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)
