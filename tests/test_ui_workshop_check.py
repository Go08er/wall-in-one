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
from dataclasses import replace
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
from tests.test_ui_next_window import Step, patient, sandboxed_runtime, settled  # noqa: E402
from wall_in_one import paths  # noqa: E402
from wall_in_one.library import filter as library_filter  # noqa: E402
from wall_in_one.library import workshop  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem, Unplayable  # noqa: E402
from wall_in_one.library.scan import WORKSHOP_PROVIDER  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.stills import StillMaker  # noqa: E402

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


def _visible_labels(widget: Gtk.Widget) -> list[str]:
    found: list[str] = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label) and child.get_visible():
            found.append(child.get_label())
        found.extend(_visible_labels(child))
        child = child.get_next_sibling()
    return found


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
        assert page.web_off_text == ""
        assert state.has_wallpaper(str(web))
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_web_wallpapers_are_off_until_allowed_and_then_play(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Owner decision: off by default, listed, said apart, and never compiled or captured."""
    from tests.golden import harness

    sandboxed_runtime(monkeypatch, tmp_path)
    content = _content(tmp_path)
    web = _install(
        content,
        "200",
        {"type": "web", "title": "Electric Eye", "file": "index.html", "preview": "p.png"},
    )
    (web / "p.png").write_bytes(harness.tiny_png((40, 120, 200), size=8))
    _install(content, "210", {"title": "Y2K Matrix", "dependency": "200", "preset": {"a": 1}})
    requested: list[set[str]] = []
    real_request = StillMaker.request

    def request(maker: StillMaker, items: object, *arguments: object, **keywords: object) -> None:
        requested.append({item.scene for item in items if item.scene})  # type: ignore[attr-defined]
        real_request(maker, items, *arguments, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(StillMaker, "request", request)
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        page = library_page(window)
        state = window.state
        yield (
            "the first scan",
            lambda: settled(application) and state.has_wallpaper(str(web)) and bool(requested),
        )
        page._kinds.set_active_name("scene")
        assert page.web_off_text == "2 web wallpapers are off"
        assert page.unsupported_text == "", "a choice, not a missing runtime"
        assert "Turn on Allow web wallpapers" in (page.web_off_button.get_tooltip_text() or "")
        electric = state.wallpaper(str(web))
        assert electric.never_plays and electric.settings_target == "library.web"
        assert electric.unplayable == workshop.WEB_OFF_REASON
        assert state.apply_blocked(str(web)) == f"Playback unavailable: {workshop.WEB_OFF_REASON}"
        assert all(not ({"200", "210"} & asked) for asked in requested), "no still while off"
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert 'scene_id = "200"' not in document and 'scene_id = "210"' not in document

        # Its details lead to the setting.
        page.inspect(electric)
        said = _visible_labels(page.inspector).count(workshop.WEB_PREVIEW_OFF_NOTE)
        assert said == 2, "the header picture and the colours' miniature desktop both say it"
        button = page.inspector.setting_button
        assert button is not None
        button.emit("clicked")
        yield (
            "the setting",
            lambda: window.current_page is not None and window.current_page.name == "settings",
        )
        window.navigate("library")

        # Turned on: compiled and reloaded through the ordinary settings path,
        # its still made from its preview first.
        application.update_settings_async(allow_web_wallpapers=True)
        yield patient(
            (
                "the web wallpaper compiled in",
                lambda: (
                    settled(application)
                    and 'scene_id = "200"'
                    in paths.runtime_config_path().read_text(encoding="utf-8")
                ),
            ),
            60.0,
        )
        assert not state.wallpaper(str(web)).never_plays
        assert page.web_off_text == ""

        # And off again: gone again, through the same path.
        application.update_settings_async(allow_web_wallpapers=False)
        yield (
            "the web wallpaper compiled out",
            lambda: (
                settled(application)
                and 'scene_id = "200"'
                not in paths.runtime_config_path().read_text(encoding="utf-8")
            ),
        )
        assert state.wallpaper(str(web)).never_plays
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
            replace(_workshop_item("200", "Electric Eye", "web", blocked=False), web_page=True),
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
        # Web wallpapers are off by default: said apart, and their tile can't play.
        assert window.web_off_notice.get_visible()
        assert window.web_off_notice.get_label() == "1 web wallpaper is off"
        web_tile = window._grid._tiles[library.items[1].path]
        assert web_tile._never == workshop.WEB_OFF_REASON
        assert not web_tile._apply.get_sensitive()
        window.apply_settings(config.Settings(roots=(tmp_path,), allow_web_wallpapers=True))
        assert not window.web_off_notice.get_visible()
        web_tile = window._grid._tiles[library.items[1].path]
        assert web_tile._never is None
        window.apply_settings(config.Settings(roots=(tmp_path,)))

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
