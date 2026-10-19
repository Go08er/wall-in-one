"""The setup card and Settings → Setup, in the real application on the golden profile.

Both interfaces show the same card while something required is missing, open
Settings at Setup from it, and run Check Again through the application's own
publication path and an unconditional reload, writing nothing a start does
not. The machine is this test's: `setup_check.local` answers that nothing is
installed; the sandbox's own runtime answers status, so the service is
running. (Kept apart from ``test_ui_setup`` because these run whole
applications.)
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.golden import harness, sandbox  # noqa: E402
from tests.shown_text import shown_texts  # noqa: E402
from tests.test_golden_tidy_card import _quiet_machine, _whitelist  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    Step,
    application_lanes,
    run_application,
    settled,
)
from wall_in_one import programs, setup_check  # noqa: E402
from wall_in_one.setup_check import Companion, Local  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.settings import SettingsPage  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.setup_section import SetupCard, SetupSection  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402
from wall_in_one.ui.window_services import UiKind  # noqa: E402

#: A machine with none of it: no renderer, no Noctalia, no service, no unit.
NOTHING = Local(
    tuple(
        programs.Program(name, Path("/usr/bin") / name, found=False)
        for name in setup_check.PROGRAMS
    ),
    False,
    Companion("absent"),
)


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sandbox.Golden]:
    runtime_dir = Path(tempfile.mkdtemp(prefix="wio-run-"))
    try:
        yield sandbox.enter(harness.FIXTURE, tmp_path / "sandbox", runtime_dir, monkeypatch)
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _card(window: object) -> SetupCard:
    if isinstance(window, MainWindow):
        return window._setup_card
    assert isinstance(window, NextWindow)
    return window.setup_card


def _section_shown(window: object) -> SetupSection:
    """Settings' Setup section, which must be the page on screen."""
    if isinstance(window, MainWindow):
        assert window._stack.get_visible_child_name() == "settings"
        return window._settings_page.setup
    assert isinstance(window, NextWindow)
    page = window.current_page
    assert isinstance(page, SettingsPage) and isinstance(page.setup, SetupSection)
    return page.setup


def _close(window: object) -> None:
    assert isinstance(window, MainWindow | NextWindow)
    window.close()


@pytest.mark.parametrize("ui", ["classic", "next"])
def test_the_card_says_what_is_required_and_check_again_writes_nothing_new(
    golden: sandbox.Golden, monkeypatch: pytest.MonkeyPatch, ui: UiKind
) -> None:
    _quiet_machine(monkeypatch)
    monkeypatch.setattr(setup_check, "local", lambda _environ=None: NOTHING)
    home = golden.profile.home
    before = harness.snapshot(home)
    application = Application(ui=ui)
    reloads: list[object] = []
    real_reload = application._reload_for_setup_check

    def reload() -> object:
        answer = real_reload()
        reloads.append(answer)
        return answer

    monkeypatch.setattr(application, "_reload_for_setup_check", reload)
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        card = _card(window)
        yield (
            "the first scan, the card's look and the service's first answer",
            lambda: (
                settled(application)
                and card.watch.findings is not None
                and application.status_model.view.service == "running"
            ),
        )
        assert card.get_reveal_child()
        assert [finding.key for finding in card.shown] == ["noctalia", "renderers"]
        texts = shown_texts(card)
        assert setup_check.NOTICE_TITLE in texts
        assert any(text.startswith("Noctalia isn't installed. ") for text in texts)
        assert any(text.startswith("No wallpaper renderer is installed. ") for text in texts)
        assert not card.link.get_visible()

        card.check_again.emit("clicked")
        yield (
            "Check Again's compile, its reload and its look",
            lambda: settled(application) and reloads != [] and card.check_again.get_sensitive(),
        )
        assert len(reloads) == 1, "the reload went, whatever the compile changed"
        # The compile is the one a start does: the same bytes, nothing new written.
        harness.check_changes(harness.diff(before, harness.snapshot(home)), _whitelist(golden))

        card.details.emit("clicked")
        section = _section_shown(window)
        yield ("the section's look", lambda: section.watch.findings is not None)
        rows = [text for row in section.rows for text in shown_texts(row) if text]
        assert "Noctalia isn't installed" in rows
        assert "The companion plugin wasn't found" in rows, "optional ones are listed here"

        card.not_now.emit("clicked")
        assert not card.get_reveal_child()
        assert len(application.setup_dismissed) == 2
        yield "every tail to settle", lambda: settled(application)
        lanes.extend(application_lanes(application))
        _close(window)

    try:
        assert run_application(application, scenario()) == 0
    finally:
        for lane in lanes:
            lane.shutdown(wait=True)  # type: ignore[attr-defined]
    harness.check_changes(harness.diff(before, harness.snapshot(home)), _whitelist(golden))
