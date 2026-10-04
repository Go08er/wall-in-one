"""The new interface's first run: the welcome screen, its "Not now", and its notices.

These run the whole application (`Application(ui="next")` through
`run_application`), with the classic window's questions -- the older-data check
and the first library folder -- asked on the welcome screen and answered with
its buttons. "Not now" saves nothing; the notice it leaves under the header is
honest about what is paused and asks again from its button.

Isolation as in ``test_ui_next_window``: a private profile, a scripted runtime,
no Noctalia, every wait bounded.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    FakeRuntime,
    Step,
    application_lanes,
    run_application,
    sandboxed_runtime,
    settled,
)
from wall_in_one import config, legacy_migration, paths  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.theme import noctalia  # noqa: E402
from wall_in_one.ui.app import Application, _PlaylistReferenceRepairResult  # noqa: E402
from wall_in_one.ui.next import window as next_window  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> Path:
    """A profile Wall-in-One has never saved, and a folder of wallpapers to suggest.

    ``~/Pictures/Wallpapers`` is what the first run finds on its own when
    Noctalia names no folder; nothing points Wall-in-One at it.
    """
    fake = FakeRuntime()
    monkeypatch.setattr(client, "send_runtime", fake.send_runtime)

    def no_shell(*_arguments: object, **_keywords: object) -> str:
        raise noctalia.NoctaliaError("noctalia is not reachable from this test")

    monkeypatch.setattr(noctalia, "_run", no_shell)
    paths.runtime_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    found = Path.home() / "Pictures" / "Wallpapers"
    found.mkdir(parents=True)
    for name in ("dawn.png", "dusk.jpg"):
        (found / name).write_bytes(b"placeholder image bytes")
    return found


def _ended(application: Application, lanes: list[ThreadPoolExecutor]) -> Iterator[Step]:
    yield "every tail to settle", lambda: settled(application)
    lanes.extend(application_lanes(application))
    window = application._window
    assert isinstance(window, NextWindow)
    window.close()


def _run(application: Application, scenario: Iterator[Step]) -> None:
    lanes: list[ThreadPoolExecutor] = []

    def whole() -> Iterator[Step]:
        yield from scenario
        yield from _ended(application, lanes)

    try:
        assert run_application(application, whole()) == 0
    finally:
        for lane in lanes:
            lane.shutdown(wait=True)


def _asking(window: NextWindow, heading: str) -> bool:
    question = window.welcome.question
    return window.welcome_shown and question is not None and question.get_heading() == heading


def _notice(window: NextWindow) -> str:
    """The action of the window's own notice, shown under the header, or empty.

    The runtime's notices (here: the service isn't running) have no action.
    """
    banner = window.state.banner()
    return banner.action if banner is not None and window.banner.get_revealed() else ""


def _no_notice_of_its_own(window: NextWindow) -> bool:
    banner = window.state.banner()
    return banner is None or banner.action == ""


def test_a_fresh_profile_opens_on_the_welcome_screen_and_not_now_saves_nothing(
    fresh: Path,
) -> None:
    application = Application(ui="next")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        assert window.welcome_shown, "a fresh profile opens on the welcome screen"
        yield "the folder question", lambda: _asking(window, "Choose a library folder")
        welcome = window.welcome
        assert str(fresh) in welcome.body_text, "the exact folder it found"
        assert {key: button.get_label() for key, button in welcome.buttons.items()} == {
            "default": "Use default",
            "manual": "Choose folder manually",
            "later": "Not now",
        }
        assert next(iter(welcome.buttons)) == "default"
        assert not paths.settings_path().exists(), "a suggestion is not consent"

        welcome.buttons["later"].emit("clicked")
        yield (
            "the window, with its notice",
            lambda: not window.welcome_shown and _notice(window) == next_window.LIBRARY_FOLDER,
        )
        assert window.banner.get_title() == next_window.NO_FOLDER
        assert window.banner.get_button_label() == "Choose a Folder…"
        assert not paths.settings_path().exists(), "Not now saves nothing"
        assert application.settings.roots == ()

        window.banner.emit("button-clicked")
        yield "the same question again", lambda: _asking(window, "Choose a library folder")
        assert _notice(window) == "", "the welcome screen asks; the notice steps aside"
        window.welcome.buttons["default"].emit("clicked")
        yield (
            "the folder saved and scanned",
            lambda: (
                settled(application)
                and window.library_text == "2 wallpapers in the library"
                and not window.welcome_shown
            ),
        )
        assert config.load().roots == (fresh,)
        assert _no_notice_of_its_own(window)

    _run(application, scenario())


def test_a_rebuilt_window_after_not_now_says_so_instead_of_asking(fresh: Path) -> None:
    """ "Not now" holds for the process: a window built again later doesn't ask on its own."""
    application = Application(ui="next")
    lanes: list[ThreadPoolExecutor] = []
    rebuilt: list[NextWindow] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield "the folder question", lambda: _asking(window, "Choose a library folder")
        window.welcome.buttons["later"].emit("clicked")
        yield "the notice", lambda: _notice(window) == next_window.LIBRARY_FOLDER
        application.hold()  # keep the process while no window is open
        window.close()
        yield "the window gone", lambda: application._window is None
        application.activate()
        again = application._window
        assert isinstance(again, NextWindow) and again is not window
        rebuilt.append(again)
        application.release()
        yield (
            "the rebuilt window's notice",
            lambda: _notice(again) == next_window.LIBRARY_FOLDER and not again.welcome_shown,
        )
        assert application.first_run_question is None
        yield "every tail to settle", lambda: settled(application)
        lanes.extend(application_lanes(application))
        again.close()

    try:
        assert run_application(application, scenario()) == 0
    finally:
        for lane in lanes:
            lane.shutdown(wait=True)
    assert rebuilt


def _older_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, list[Path]]:
    """Settings and playlists from the older plugin, waiting for a decision."""
    found = legacy_migration.Probe(
        "ready", "legacy authoring is ready", tmp_path / "legacy", schema=5, playlists=2
    )
    decided: dict[str, list[Path]] = {"later": [], "fresh": []}
    state = {"decided": False}

    def probe() -> legacy_migration.Probe:
        if state["decided"]:
            return legacy_migration.Probe("absent", "decided", found.source)
        return found

    def postpone(*, source_dir: Path) -> legacy_migration.Outcome:
        decided["later"].append(source_dir)
        return legacy_migration.Outcome(False, "postponed")

    def decline(*, source_dir: Path) -> legacy_migration.Outcome:
        decided["fresh"].append(source_dir)
        state["decided"] = True
        return legacy_migration.Outcome(True, "kept the current profile")

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", probe)
    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.postpone", postpone)
    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.decline", decline)
    return decided


def test_older_data_is_the_welcome_screens_import_step_and_not_now_pauses_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    decided = _older_data(monkeypatch, tmp_path)
    application = Application(ui="next")
    heading = "Older Wall-in-One data found"

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield "the import step", lambda: _asking(window, heading)
        buttons = window.welcome.buttons
        assert {key: button.get_label() for key, button in buttons.items()} == {
            "import": "Import safely",
            "fresh": "Start fresh",
            "later": "Not now",
        }
        assert next(iter(buttons)) == "import", "importing is offered first"
        assert "Playlists: 2" in window.welcome.body_text

        buttons["later"].emit("clicked")
        yield (
            "the window, with changes paused",
            lambda: not window.welcome_shown and _notice(window) == next_window.OLDER_DATA,
        )
        assert decided["later"] == [tmp_path / "legacy"]
        assert application.authoring_gate == "decision"
        assert not application.require_authoring_ready(report=False)
        assert window.banner.get_title() == next_window.OLDER_DATA_WAITING
        assert window.banner.get_button_label() == "Review…"
        assert window.library_text == "0 wallpapers in the library", "nothing loads meanwhile"

        window.banner.emit("button-clicked")
        yield "the import step again", lambda: _asking(window, heading)
        window.welcome.buttons["fresh"].emit("clicked")
        yield (
            "the decision saved and the library loaded",
            lambda: (
                settled(application)
                and window.library_text == "2 wallpapers in the library"
                and not window.welcome_shown
            ),
        )
        assert decided["fresh"] == [tmp_path / "legacy"]
        assert application.authoring_gate == "open"
        assert _no_notice_of_its_own(window)

    _run(application, scenario())


def test_a_failed_check_says_so_on_the_welcome_screen_and_tries_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    attempts = {"count": 0}

    def probe() -> legacy_migration.Probe:
        attempts["count"] += 1
        if attempts["count"] <= 2:  # the startup check, and the window's own
            raise legacy_migration.MigrationError("the older plugin's folder is unreadable")
        return legacy_migration.Probe("absent", "no older data", tmp_path / "legacy")

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", probe)
    application = Application(ui="next")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield "the failure", lambda: _asking(window, "Could not check older data")
        assert "unreadable" in window.welcome.body_text
        assert list(window.welcome.buttons) == ["retry", "later"]
        window.welcome.buttons["retry"].emit("clicked")
        yield (
            "the check passed and the library loaded",
            lambda: (
                settled(application)
                and window.library_text == "2 wallpapers in the library"
                and not window.welcome_shown
            ),
        )
        assert _no_notice_of_its_own(window)

    _run(application, scenario())


def test_an_existing_profile_never_shows_the_welcome_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The older-data check runs at every start; it is not a reason to cover the window."""
    sandboxed_runtime(monkeypatch, tmp_path)
    application = Application(ui="next")
    shown: list[str] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        root = window._root
        assert root is not None
        root.connect(
            "notify::visible-child-name",
            lambda stack, _param: shown.append(stack.get_visible_child_name() or ""),
        )
        assert not window.welcome_shown
        yield (
            "the check, the repair and the first scan",
            lambda: settled(application) and window.library_text == "2 wallpapers in the library",
        )
        assert "welcome" not in shown and not window.welcome_shown

    _run(application, scenario())


def test_a_long_check_shows_its_progress_after_a_moment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """As the classic window's progress dialog does, once the check is slow."""
    sandboxed_runtime(monkeypatch, tmp_path)
    release = threading.Event()
    monkeypatch.setattr(next_window, "CHECK_GRACE_MS", 50)

    def probe() -> legacy_migration.Probe:
        assert release.wait(5)
        return legacy_migration.Probe("absent", "no older data", tmp_path / "legacy")

    monkeypatch.setattr("wall_in_one.ui.app.legacy_migration.probe", probe)
    application = Application(ui="next")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield "the check's progress", lambda: window.welcome_shown and window.welcome.waiting
        assert window.welcome.title_text == "Checking older data"
        assert window.welcome.buttons == {}, "progress can't be answered"
        release.set()
        yield (
            "the window back, with its library",
            lambda: (
                settled(application)
                and not window.welcome_shown
                and window.library_text == "2 wallpapers in the library"
            ),
        )

    try:
        _run(application, scenario())
    finally:
        release.set()


def test_a_failed_repair_pauses_changes_and_its_notice_tries_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    application = Application(ui="next")
    window = NextWindow(application, application.settings)
    application._window = window
    window.present()
    try:
        result = application._finish_playlist_reference_repair(
            _PlaylistReferenceRepairResult(failures=("schedules.json is unreadable",))
        )
        assert not result.ok and application.authoring_gate == "repair"
        spin_until(lambda: _notice(window) == next_window.REPAIR)
        assert window.banner.get_title() == next_window.REPAIR_NEEDED
        assert window.banner.get_button_label() == "Try Again"

        window.banner.emit("button-clicked")  # the real repair, on this clean profile
        spin_until(lambda: application.authoring_gate == "open", timeout=5)
        spin_until(lambda: _no_notice_of_its_own(window))
    finally:
        application._window = None
        window.destroy()
        application._shutdown_authoring_jobs()
        application._shutdown_legacy_migration_jobs(wait=True)
        application._stills.shutdown()
        application.session.shutdown()
