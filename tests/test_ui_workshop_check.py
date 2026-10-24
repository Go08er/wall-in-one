"""Wallpaper Engine in both Libraries: the unsupported notice, and checking again.

The owner added a Workshop wallpaper Wall-in-One never showed, and asked for a
way to pick up what Steam adds (docs/wallpaper-engine.md). The Scenes view
(classic: "Videos and scenes") counts the items no renderer here runs, and both
it and the Videos view offer "Check Wallpaper Engine for new wallpapers",
which is the library's own rescan.

Steam's folder here is a fixture under the test's own HOME; the real one is
never read.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import destroy_window  # noqa: E402
from tests.started import started  # noqa: E402
from tests.test_ui_next_slice import finish, library_page, run  # noqa: E402
from tests.test_ui_next_window import Step, sandboxed_runtime, settled  # noqa: E402
from wall_in_one import paths  # noqa: E402
from wall_in_one.library import filter as library_filter  # noqa: E402
from wall_in_one.library import workshop  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem, Unplayable  # noqa: E402
from wall_in_one.library.scan import WORKSHOP_PROVIDER  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402

TWO = "2 Wallpaper Engine wallpapers need a runtime linux-wallpaperengine doesn\u2019t support yet"
THREE = TWO.replace("2 ", "3 ", 1)
ONE = TWO.replace("2 ", "1 ", 1).replace("wallpapers need", "wallpaper needs", 1)


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _content(tmp_path: Path) -> Path:
    """Steam's Wallpaper Engine folder in this test's HOME, where the scan looks."""
    return (
        tmp_path / "home" / ".local" / "share" / "Steam" / "steamapps" / "workshop" / "content"
    ) / workshop.APP_ID


def _install(content: Path, workshop_id: str, project: dict[str, object]) -> Path:
    directory = content / workshop_id
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(json.dumps(project), encoding="utf-8")
    return directory


def test_the_scenes_view_counts_what_cant_run_and_checks_wallpaper_engine_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    content = _content(tmp_path)
    trojan = _install(content, "100", {"type": "application", "title": "Trojan", "file": "run.exe"})
    web = _install(content, "200", {"type": "web", "title": "Electric Eye", "file": "index.html"})
    _install(
        content,
        "300",
        {"title": "Trojan preset", "dependency": "100", "preset": {"speed": 2}},
    )
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        page = library_page(window)
        state = window.state
        yield (
            "the first scan, Wallpaper Engine included",
            lambda: settled(application) and state.has_wallpaper(str(trojan)),
        )

        # Every view but Scenes and Videos: neither the notice nor the button.
        assert page.unsupported_text == ""
        assert not page.check_workshop.get_visible()

        page._kinds.set_active_name("scene")
        assert page.unsupported_text == TWO
        assert page.unsupported_notice.get_tooltip_text() == (
            "Listed, never played: Trojan, Trojan preset. "
            "Executable wallpapers are never run, for security."
        )
        assert page.check_workshop.get_visible() and page.check_workshop.get_sensitive()
        listed = {view.name: view for view in page._matches()}
        assert set(listed) == {"Trojan", "Electric Eye", "Trojan preset"}
        assert listed["Electric Eye"].kind_detail == "web"
        assert not listed["Electric Eye"].never_plays
        assert listed["Trojan"].kind_detail == "application" and listed["Trojan"].never_plays
        assert listed["Trojan preset"].kind_detail == "preset"
        assert state.apply_blocked(str(trojan)).startswith("Playback unavailable: it is a program")

        # What the service is given: never the executable, nor the preset of one.
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert 'scene_id = "100"' not in document and 'scene_id = "300"' not in document
        assert 'scene_base = "100"' not in document

        # Steam adds another while the window is open: the button is F5's rescan.
        _install(content, "400", {"type": "application", "title": "Another", "file": "x"})
        page.check_workshop.emit("clicked")
        yield (
            "the rescan the button asked for",
            lambda: settled(application) and state.has_wallpaper(str(content / "400")),
        )
        assert page.unsupported_text == THREE

        # The Videos view checks too, and counts nothing: those are scenes.
        page._kinds.set_active_name("video")
        assert page.check_workshop.get_visible() and page.unsupported_text == ""
        assert state.has_wallpaper(str(web))
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def _workshop_item(workshop_id: str, title: str, kind: str, blocked: bool) -> MediaItem:
    return MediaItem(
        Path("/steam/steamapps/workshop/content/431960") / workshop_id,
        Kind.SCENE,
        1,
        1,
        provider=WORKSHOP_PROVIDER,
        scene=workshop_id,
        title=title,
        workshop_type=kind,
        unplayable=Unplayable.RUNTIME if blocked else None,
        unplayable_reason="it is a program" if blocked else "",
    )


def test_the_classic_library_bar_says_it_once_and_checks_through_the_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one import config
    from wall_in_one.session import Session
    from wall_in_one.theme import source
    from wall_in_one.ui.window import MainWindow

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    library = Library(
        roots=(),
        items=(
            _workshop_item("100", "Trojan", "application", blocked=True),
            _workshop_item("200", "Electric Eye", "web", blocked=False),
        ),
    )
    rescans: list[str] = []

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.WorkshopCheck")
            self.settings = config.Settings(roots=(tmp_path,))
            self.resolved_palette = source.resolve()
            self.session = Session(self.settings, scanner=lambda _roots: library)
            self.session.refresh()

        def refresh_library(self) -> None:
            rescans.append("rescan")

        def runtime_action_async(self, verb: str, argument: str | None = None) -> bool:
            return True

        def with_task_durability(self, message: str) -> str:
            return message

    application = started(FakeApp())
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    try:
        window.show_library(application.session)
        assert not window.unsupported_notice.get_visible()
        assert not window.check_workshop.get_visible()

        moving = library_filter.KIND_CHOICES.index(library_filter.Kinds.VIDEOS)
        window._kinds.set_selected(moving)
        assert window.unsupported_notice.get_visible()
        assert window._unsupported_label.get_label() == ONE
        assert "Trojan" in (window.unsupported_notice.get_tooltip_text() or "")
        assert window.check_workshop.get_visible()

        window.check_workshop.emit("clicked")
        assert rescans == ["rescan"], "the library's own rescan, nothing else"
        window.show_library_scanning(True)
        assert not window.check_workshop.get_sensitive()
        window.show_library_scanning(False)
        assert window.check_workshop.get_sensitive()

        # Workshop scanning off, or no library folder (so no scan at all): no
        # button, and nothing to count.
        for unscanned in (
            config.Settings(roots=(tmp_path,), scan_workshop=False),
            config.Settings(roots=()),
        ):
            window.apply_settings(unscanned)
            assert not window.check_workshop.get_visible()
            assert not window.unsupported_notice.get_visible()
    finally:
        destroy_window(window)
        application.session.shutdown()
