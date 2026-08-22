"""Display authoring keeps detached choices visible and honest."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from wall_in_one import config  # noqa: E402
from wall_in_one.library import schedules  # noqa: E402
from wall_in_one.ui import preferences, schedules_page  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class _PreferencesApp:
    def __init__(self, root: Path) -> None:
        self.settings = config.Settings(
            roots=(root,),
            display_mode=config.DISPLAY_MODE_INDEPENDENT,
            theme_source_connector="DP-9",
        )
        self.session = SimpleNamespace(
            displays=SimpleNamespace(all=lambda: (("DP-9", "evening"),)),
            schedules=SimpleNamespace(rules=()),
        )
        self.resolved_palette = None

    def update_settings(self, **changes: Any) -> config.Settings:
        self.settings = replace(self.settings, **changes).validated()
        return self.settings

    def window_report(self, _message: str) -> None: ...

    def open_palette_browser(self) -> None: ...

    def reload_palette(self) -> None: ...


def _model_strings(row: Adw.ComboRow) -> tuple[str, ...]:
    model = row.get_model()
    assert isinstance(model, Gtk.StringList)
    return tuple(model.get_string(index) or "" for index in range(model.get_n_items()))


def test_theme_source_preserves_a_detached_choice_across_hotplug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connected = ["eDP-1"]
    monkeypatch.setattr(preferences, "_connected_outputs", lambda: tuple(connected))
    root = tmp_path / "library"
    root.mkdir()
    application = _PreferencesApp(root)
    page = preferences.PreferencesPage(application)  # type: ignore[arg-type]

    assert page._selected_theme_connector() == "DP-9"
    assert "DP-9 (not attached)" in _model_strings(page._theme_source)
    assert "temporarily follow eDP-1" in page._theme_source.get_subtitle()

    connected.append("DP-9")
    page._on_outputs_changed()

    assert page._selected_theme_connector() == "DP-9"
    assert "DP-9" in _model_strings(page._theme_source)
    assert "not attached" not in page._theme_source.get_subtitle()
    assert application.settings.theme_source_connector == "DP-9"


class _Displays:
    def __init__(self) -> None:
        self.values = (("DP-9", "evening"),)

    def all(self) -> tuple[tuple[str, str], ...]:
        return self.values

    def __len__(self) -> int:
        return len(self.values)


def _schedule_app(mode: str) -> SimpleNamespace:
    playlist = SimpleNamespace(id="evening", name="Evening")
    rule = schedules.Rule(id="dock", playlist="evening", connector="DP-9")
    session = SimpleNamespace(
        settings=config.Settings(
            active_playlist="evening",
            display_mode=mode,
            theme_source_connector="DP-9",
        ),
        manual_playlist=None,
        playlists=SimpleNamespace(all=lambda: (playlist,)),
        displays=_Displays(),
        schedules=SimpleNamespace(rules=(rule,)),
    )
    return SimpleNamespace(session=session, runtime_status=None)


def _group_titles(page: schedules_page.SchedulesPage) -> tuple[str, ...]:
    titles: list[str] = []
    child = page._content.get_first_child()
    while child is not None:
        if isinstance(child, Adw.PreferencesGroup):
            titles.append(child.get_title())
        child = child.get_next_sibling()
    return tuple(titles)


def test_mirrored_mode_hides_assignment_clutter_but_keeps_rule_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: ("eDP-1",))
    application = _schedule_app(config.DISPLAY_MODE_MIRRORED)
    page = schedules_page.SchedulesPage(application)  # type: ignore[arg-type]
    page.refresh(application.session)

    assert "Screens" not in _group_titles(page)
    assert "Independent display setup" in _group_titles(page)
    assert page._rule_target.get_sensitive() is False

    rule = application.session.schedules.rules[0]
    page._edit_rule(rule)
    assert page._selected_rule_connector() == "DP-9"

    application.session.settings = replace(
        application.session.settings,
        display_mode=config.DISPLAY_MODE_INDEPENDENT,
    )
    page.refresh(application.session)

    assert "Screens" in _group_titles(page)
    assert page._rule_target.get_sensitive() is True
    assert page._playback_row is not None
    assert page._playback_row.get_sensitive() is False
