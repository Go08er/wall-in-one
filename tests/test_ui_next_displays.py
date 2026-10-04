"""The new interface's Displays page over the real adapter and a real session.

GTK's monitors are replaced by a fixed two-display layout (DP-1 and
HDMI-A-1, side by side), so the arrangement and the connected displays are
the same on any test machine. Writes wait on the recording application's
lane until the test runs it; the runtime's answers go through the real
status model.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_next_schedule_mapping import Publishing, _playlist  # noqa: E402
from tests.test_next_state import _library, _session  # noqa: E402
from tests.test_next_status_line import two_display_status  # noqa: E402
from wall_in_one import config, runtime_config  # noqa: E402
from wall_in_one.library import displays as display_store  # noqa: E402
from wall_in_one.library.model import MediaItem  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui.next import displays, real_schedule  # noqa: E402
from wall_in_one.ui.next.displays import DisplaysPage  # noqa: E402
from wall_in_one.ui.next.prefs import UiPrefsKeeper  # noqa: E402
from wall_in_one.ui.next.real_state import RealAppState  # noqa: E402
from wall_in_one.ui.next.schedule_state import DisplayInfo  # noqa: E402

LAYOUT = [
    DisplayInfo("DP-1", "Dell U2723QE", True, 0, 0, 2560, 1440, True, 1.5, 60000),
    DisplayInfo("HDMI-A-1", "LG 24GL600F", True, 2560, 0, 1920, 1080, True, 1.0, 144000),
]


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def monitors(monkeypatch: pytest.MonkeyPatch) -> list[DisplayInfo]:
    """GTK's monitors, as the test says they are."""
    layout = list(LAYOUT)
    monkeypatch.setattr(real_schedule, "_monitor_displays", lambda: list(layout))
    return layout


@pytest.fixture
def items(tmp_path: Path) -> tuple[MediaItem, ...]:
    return _library(tmp_path / "home" / "Pictures" / "Wallpapers")


def _independent(session: Session) -> None:
    session.update_settings(
        replace(
            session.settings,
            display_mode=config.DISPLAY_MODE_INDEPENDENT,
            theme_source_connector="DP-1",
        )
    )


@pytest.fixture
def backend(items: tuple[MediaItem, ...], monitors: list[DisplayInfo]) -> Iterator[Publishing]:
    session = _session(items)
    try:
        yield Publishing(session)
    finally:
        session.shutdown()


class Shown:
    """One Displays page in a window, over the real adapter."""

    def __init__(self, backend: Publishing) -> None:
        self.backend = backend
        self.keeper = UiPrefsKeeper(backend.window_report)
        self.state = RealAppState(backend, self.keeper)
        page = displays.create(self.state)
        assert isinstance(page, DisplaysPage)
        self.page = page
        self.window = Adw.Window(content=page.widget)
        self.window.set_default_size(1200, 1100)
        self.window.present()
        spin_until(page.widget.get_mapped, 5, what="the page on screen")

    def settle(self) -> None:
        self.backend.run_authoring()
        self.page.flush()

    def answer(self, status: dict[str, object]) -> None:
        self.backend.status_model.adopt(status)
        self.page.flush()

    def select(self, connector: str) -> None:
        self.page.demo(f"select:{connector}")
        assert self.page.selected == connector

    def shown_playlist(self) -> str:
        page = self.page
        return page._plays_ids[page.plays_row.get_selected()]

    def close(self) -> None:
        self.window.destroy()
        self.state.close()
        self.keeper.close()


def test_the_arrangement_is_the_compositors_layout(backend: Publishing) -> None:
    view = Shown(backend)
    try:
        page = view.page
        assert list(page.arrangement.tiles) == ["DP-1", "HDMI-A-1"]
        assert page.heading.get_label() == "DP-1 · Dell U2723QE"
        view.select("HDMI-A-1")
        assert page.heading.get_label() == "HDMI-A-1 · LG 24GL600F"
        assert page._meta.get_label() == "1920 \u00d7 1080 · 144 Hz · 100%"
        assert page.linked_note.get_visible(), "mirrored by default: one rotation"
        assert not page.plays_row.get_visible()
    finally:
        view.close()


def test_an_assignment_is_saved_then_shown(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    session = backend.session
    _independent(session)
    pid = _playlist(session, "Desk", items[0])
    view = Shown(backend)
    try:
        view.select("HDMI-A-1")
        page = view.page
        page.plays_row.set_selected(page._plays_ids.index(pid))
        assert session.displays.all() == (), "nothing before the store answers"
        assert view.shown_playlist() == pid, "the queued choice shows meanwhile"
        view.settle()
        assert json.loads(display_store.state_path().read_text())["displays"] == {"HDMI-A-1": pid}
        assert backend.published == 1, "the runtime configuration was published"
        assert view.shown_playlist() == pid
    finally:
        view.close()


def test_an_empty_playlist_is_refused_and_the_row_shows_what_is_saved(
    backend: Publishing,
) -> None:
    session = backend.session
    _independent(session)
    empty = session.playlists.create("Nothing yet").id
    view = Shown(backend)
    try:
        view.select("HDMI-A-1")
        page = view.page
        page.plays_row.set_selected(page._plays_ids.index(empty))
        page.flush()
        assert view.shown_playlist() == "", "back on the default"
        assert session.displays.all() == () and not backend.authoring
        assert "nothing to play yet" in backend.reports[-1]
    finally:
        view.close()


def test_the_last_queued_assignment_decides_what_shows(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    """Two choices queued, the first failing at the head of the lane: the second shows."""
    session = backend.session
    _independent(session)
    first = _playlist(session, "First", items[0])
    second = _playlist(session, "Second", items[1])
    view = Shown(backend)
    try:
        view.select("HDMI-A-1")
        page = view.page
        page.plays_row.set_selected(page._plays_ids.index(first))
        page.plays_row.set_selected(page._plays_ids.index(second))
        emptied = session.playlists.get(first)
        assert emptied is not None
        session.playlists.remove_entry(first, emptied.entries[0].id)  # fails when it runs
        view.backend.run_authoring()
        page.flush()
        assert session.displays.playlist_for("HDMI-A-1") == second
        assert view.shown_playlist() == second
    finally:
        view.close()


def test_a_failed_assignment_goes_back_to_what_is_saved(
    backend: Publishing, items: tuple[MediaItem, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = backend.session
    _independent(session)
    pid = _playlist(session, "Desk", items[0])

    def refuse(_connector: str, _playlist: str) -> None:
        raise display_store.DisplayError("local-io", "the disk is full")

    monkeypatch.setattr(session.displays, "assign", refuse)
    view = Shown(backend)
    try:
        view.select("HDMI-A-1")
        page = view.page
        page.plays_row.set_selected(page._plays_ids.index(pid))
        view.settle()
        assert view.shown_playlist() == ""
        assert "nothing changed" in backend.reports[-1]
    finally:
        view.close()


def test_beating_the_shared_rules_goes_only_in_the_overrides(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    session = backend.session
    _independent(session)
    pid = _playlist(session, "Desk", items[0])
    session.displays.assign("HDMI-A-1", pid)
    target = backend.target
    runtime_config.write(session.settings, session, target)
    before = target.read_bytes()
    view = Shown(backend)
    try:
        view.select("HDMI-A-1")
        page = view.page
        assert page.beats_row.get_sensitive()
        page.beats_row.set_active(True)
        view.settle()
        assert session.displays.beats_global_rules("HDMI-A-1")
        assert target.read_bytes() == before, "runtime.toml is byte for byte the same"
        overrides = tomllib.loads(runtime_config.overrides_path(target).read_text())
        assert overrides["displays"] == [{"connector": "HDMI-A-1", "beats_global_rules": True}]
        assert page.beats_row.get_active()
        assert "updated wallpaper service" in (page.beats_row.get_subtitle() or ""), (
            "this runtime does not say it reads the overrides"
        )
        view.select("DP-1")
        assert not page.beats_row.get_sensitive(), "no playlist of its own: nothing to beat with"
    finally:
        view.close()


def test_independent_displays_with_none_connected_are_refused(
    backend: Publishing, monitors: list[DisplayInfo]
) -> None:
    monitors.clear()
    view = Shown(backend)
    try:
        page = view.page
        assert not page.arrangement.get_visible()
        page.mode_group.set_active_name("independent")
        page.flush()
        assert page.mode_group.get_active_name() == "mirrored"
        assert backend.settings_changes == [], "no settings write was even queued"
        assert "needs an attached display" in backend.reports[-1]
        assert backend.session.settings.display_mode == config.DISPLAY_MODE_MIRRORED
    finally:
        view.close()


def test_switching_to_independent_saves_the_mode_and_a_colors_display(
    backend: Publishing,
) -> None:
    view = Shown(backend)
    try:
        page = view.page
        page.mode_group.set_active_name("independent")
        assert page.mode_group.get_active_name() == "independent", "queued"
        view.settle()
        settings = backend.session.settings
        assert (settings.display_mode, settings.theme_source_connector) == ("independent", "DP-1")
        assert page.plays_row.get_visible() and not page.linked_note.get_visible()
    finally:
        view.close()


def test_detached_displays_are_kept_until_forgotten(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    session = backend.session
    pid = _playlist(session, "Dock", items[0])
    session.displays.assign("DP-2", pid)
    rule = session.schedules.add(pid, connector="DP-2")
    view = Shown(backend)
    try:
        page = view.page
        assert list(page.remembered_rows) == ["DP-2"], "kept although not connected"
        subtitle = page.remembered_rows["DP-2"].get_subtitle() or ""
        assert "Dock" in subtitle and "1 rule aimed at it" in subtitle
        page.forget("DP-2")
        view.settle()
        assert session.displays.all() == ()
        assert rule.id not in {r.id for r in session.schedules.rules}
        assert page.remembered_rows == {}
        assert backend.published == 1, "forgetting republished once"
    finally:
        view.close()


def test_a_connected_display_offers_no_forget(backend: Publishing) -> None:
    view = Shown(backend)
    try:
        assert view.page.remembered_rows == {}
        assert backend.session.displays.all() == ()
        plan = view.state.scheduling
        assert plan is not None and plan.forget_plan("DP-1").refused
    finally:
        view.close()


def test_rotation_choices_are_runtime_commands_shown_from_the_runtime(
    backend: Publishing,
) -> None:
    _independent(backend.session)
    view = Shown(backend)
    try:
        view.answer(two_display_status())
        view.select("HDMI-A-1")
        page = view.page
        assert page.rotation_row.get_visible()
        assert page.cycle_toggle.get_active(), "the runtime says Cycle is on"
        assert not page.defaults_button.get_visible(), "nothing set by hand yet"
        page.cycle_toggle.set_active(False)
        assert ("HDMI-A-1", "cycle", "off") in backend.sent
        page.flush()
        assert page.cycle_toggle.get_active(), "back on the runtime's answer until it changes"
        status = two_display_status()
        records = status["displays"]
        assert isinstance(records, list)
        records[1].update(cycle_enabled=False, cycle_source="manual")
        view.answer(status)
        assert not page.cycle_toggle.get_active()
        assert page.defaults_button.get_visible()
        page.defaults_button.emit("clicked")
        assert backend.sent[-2:] == [
            ("HDMI-A-1", "cycle", "default"),
            ("HDMI-A-1", "shuffle", "default"),
        ]
        assert not backend.authoring and backend.settings_changes == [], "nothing is stored"
    finally:
        view.close()


def test_a_newer_display_file_turns_the_display_choices_off(
    backend: Publishing,
) -> None:
    path = display_store.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99, "displays": {}}))
    session = Session(backend.session.settings, owns_playback=False)
    _independent(session)
    newer = Publishing(session)
    view = Shown(newer)
    try:
        page = view.page
        assert not page.plays_row.get_sensitive()
        assert "newer version" in (page.plays_row.get_subtitle() or "")
        assert not page.mode_group.get_sensitive()
    finally:
        view.close()
        session.shutdown()
