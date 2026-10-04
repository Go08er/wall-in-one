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
from wall_in_one import config, runtime_compatibility, runtime_config  # noqa: E402
from wall_in_one.library import playlists, scan  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.ui import preferences  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402
from wall_in_one.ui.next.schedule_state import RuleView  # noqa: E402


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
