"""The setup card and Settings → Setup, over an application double.

What the widgets show comes from `wall_in_one.setup_check`; these tests drive
what it observes (`setup_check.local`, replaced, and the status model the
application polls) and check what is on screen, what Check Again asks of the
application, and that Not Now holds until a finding changes. The real windows
are ``tests/test_golden_setup_card.py``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import FIRST_FRAME_S, REAL_IO_S, first_frame, spin_until  # noqa: E402
from tests.shown_text import shown_texts  # noqa: E402
from wall_in_one import programs, setup_check  # noqa: E402
from wall_in_one.library.model import Kind  # noqa: E402
from wall_in_one.setup_check import Companion, Local  # noqa: E402
from wall_in_one.ui.setup_section import SetupCard, SetupLane, SetupSection  # noqa: E402
from wall_in_one.ui.status_model import RuntimeStatusModel  # noqa: E402

_Result = TypeVar("_Result")

#: A library with a video in it, and one of stills only.
_VIDEO_LIBRARY = (SimpleNamespace(kind=Kind.STILL), SimpleNamespace(kind=Kind.VIDEO))
_STILLS_LIBRARY = (SimpleNamespace(kind=Kind.STILL),)


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


INSTALLED = Companion("installed", "/state/noctalia/plugins/x/wall-in-one")


def _machine(
    *missing: str,
    unit: bool | None = True,
    companion: Companion = INSTALLED,
) -> Local:
    return Local(
        tuple(
            programs.Program(name, Path("/usr/bin") / name, found=name not in missing)
            for name in setup_check.PROGRAMS
        ),
        unit,
        companion,
    )


class _ObservedLane(SetupLane):
    """The application's lane, unchanged, with its worker's progress observable."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._unfinished = 0
        self.submitted = 0

    @property
    def working(self) -> bool:
        with self._lock:
            return self._unfinished > 0

    def submit(
        self, work: Callable[[], _Result], done: Callable[[_Result | None, str], None]
    ) -> bool:
        def observed() -> _Result:
            try:
                return work()
            finally:
                with self._lock:
                    self._unfinished -= 1

        with self._lock:
            self._unfinished += 1
        self.submitted += 1
        accepted = super().submit(observed, done)
        if not accepted:
            with self._lock:
                self._unfinished -= 1
        return accepted


class _Application:
    """What the card and the section ask of the application, and nothing else."""

    def __init__(self) -> None:
        self.status_model = RuntimeStatusModel()
        self.setup_lane = _ObservedLane()
        self.setup_dismissed: set[str] = set()
        self.checks_again = 0
        #: The library the application holds: kinds only matter here.
        self.session = SimpleNamespace(library=SimpleNamespace(items=_VIDEO_LIBRARY))

    def check_setup_again(self) -> bool:
        self.checks_again += 1
        return True


class _Machine:
    """What `setup_check.local` answers: a test changes it as a user installs things."""

    def __init__(self, local: Local) -> None:
        self.local = local
        self.looks = 0

    def __call__(self, _environ: object = None) -> Local:
        self.looks += 1
        return self.local


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> _Machine:
    observed = _Machine(_machine())
    monkeypatch.setattr(setup_check, "local", observed)
    return observed


@pytest.fixture
def application() -> Iterator[_Application]:
    double = _Application()
    try:
        yield double
    finally:
        double.setup_lane.shutdown()


def _present(child: Gtk.Widget) -> tuple[Gtk.Window, Callable[[], bool]]:
    window = Gtk.Window(child=child, default_width=900, default_height=700)
    window.present()
    return window, first_frame(window)


def _looked(
    application: _Application,
    painted: Callable[[], bool],
    condition: Callable[[], object],
    what: str,
) -> None:
    """The first frame, then the lane's worker (real filesystem looks are fakes here), then GTK."""
    spin_until(painted, FIRST_FRAME_S, what="the window's first frame")
    spin_until(
        lambda: not application.setup_lane.working, REAL_IO_S, what=f"the setup lane: {what}"
    )
    spin_until(condition, what=what)


def _section_texts(section: SetupSection) -> list[str]:
    found: list[str] = []
    for row in section.rows:
        found.extend(text for text in shown_texts(row) if text)
    return found


def test_the_section_lists_every_finding_and_follows_the_service(
    machine: _Machine, application: _Application
) -> None:
    machine.local = _machine("linux-wallpaperengine", companion=Companion("absent"))
    page = Adw.PreferencesPage()
    section = SetupSection(application)  # type: ignore[arg-type]
    page.add(section)
    window, painted = _present(page)
    try:
        assert setup_check.SECTION_TITLE in shown_texts(section)
        _looked(application, painted, lambda: section.watch.findings is not None, "the first look")
        texts = _section_texts(section)
        # No status yet: nothing is said about the service.
        assert "The wallpaper service isn't running" not in texts
        assert "linux-wallpaperengine isn't installed" in texts
        assert "The companion plugin wasn't found" in texts
        links = [
            widget
            for row in section.rows
            for widget in _descendants(row)
            if isinstance(widget, Gtk.LinkButton)
        ]
        assert [link.get_uri() for link in links] == [setup_check.COMPANION_URL]

        # The application's poll finds no service: said at once, no new look.
        looks = machine.looks
        application.status_model.mark_unavailable(forget=True)
        spin_until(lambda: "The wallpaper service isn't running" in _section_texts(section))
        assert machine.looks == looks

        # The companion starts it, and nothing is missing any more.
        machine.local = _machine()
        application.status_model.adopt({"status_version": 2, "missing_renderers": []})
        section.check_again.emit("clicked")
        assert application.checks_again == 1, "Check Again compiles and reloads"
        _looked(
            application,
            painted,
            lambda: _section_texts(section) == [setup_check.ALL_PRESENT],
            "everything present",
        )
        assert section.check_again.get_sensitive()
    finally:
        window.destroy()


def test_the_section_says_when_it_could_not_look(
    monkeypatch: pytest.MonkeyPatch, application: _Application
) -> None:
    def broken(_environ: object = None) -> Local:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(setup_check, "local", broken)
    page = Adw.PreferencesPage()
    section = SetupSection(application)  # type: ignore[arg-type]
    page.add(section)
    window, painted = _present(page)
    try:
        _looked(
            application,
            painted,
            lambda: any("the disk went away" in text for text in _section_texts(section)),
            "the failed look",
        )
        assert _section_texts(section) == ["Couldn't check: the disk went away"]
    finally:
        window.destroy()


def _descendants(widget: Gtk.Widget) -> Iterator[Gtk.Widget]:
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


def test_the_card_shows_only_what_is_required(machine: _Machine, application: _Application) -> None:
    machine.local = _machine("mpvpaper", companion=Companion("absent"))
    details: list[str] = []
    card = SetupCard(application, lambda: details.append("details"))  # type: ignore[arg-type]
    window, painted = _present(card)
    try:
        card.start()
        _looked(application, painted, lambda: card.watch.findings is not None, "the first look")
        assert not card.get_reveal_child(), "one renderer and the companion are Settings' only"
        assert not card.shown

        machine.local = _machine("mpvpaper", "linux-wallpaperengine", companion=Companion("absent"))
        card.check_again.emit("clicked")
        assert application.checks_again == 1
        _looked(application, painted, card.get_reveal_child, "the card for no renderer at all")
        texts = shown_texts(card)
        assert setup_check.NOTICE_TITLE in texts
        assert any(text.startswith("No wallpaper renderer is installed. ") for text in texts)
        assert not card.link.get_visible(), "nothing it shows needs the companion"

        application.status_model.mark_unavailable(forget=True)
        spin_until(lambda: card.link.get_visible(), what="the service finding, with the link")
        assert [finding.key for finding in card.shown] == ["service", "renderers"]
        assert card.link.get_uri() == setup_check.COMPANION_URL

        card.details.emit("clicked")
        assert details == ["details"]
    finally:
        window.destroy()


def test_not_now_holds_for_the_session_until_a_finding_changes(
    machine: _Machine, application: _Application
) -> None:
    machine.local = _machine("noctalia")
    card = SetupCard(application, lambda: None)  # type: ignore[arg-type]
    window, painted = _present(card)
    try:
        card.start()
        _looked(application, painted, card.get_reveal_child, "the card for Noctalia")
        card.not_now.emit("clicked")
        assert not card.get_reveal_child()
        assert len(application.setup_dismissed) == 1

        # The same findings again (a status poll, another look): still hidden.
        application.status_model.adopt({"status_version": 2})
        card.watch.look()
        _looked(application, painted, lambda: not card.watch.looking, "the second look")
        assert not card.get_reveal_child()

        # A second window of the same application remembers it too.
        other = SetupCard(application, lambda: None)  # type: ignore[arg-type]
        other_window, other_painted = _present(other)
        try:
            other.start()
            _looked(
                application, other_painted, lambda: other.watch.findings is not None, "its look"
            )
            assert not other.get_reveal_child()
        finally:
            other_window.destroy()

        # Something else goes missing: that one shows, the dismissed one does not.
        application.status_model.mark_unavailable(forget=True)
        spin_until(card.get_reveal_child, what="the new finding")
        assert [finding.key for finding in card.shown] == ["service"]
    finally:
        window.destroy()


def test_the_setup_lane_never_waits_for_a_look_that_hangs() -> None:
    """A look stuck on a dead mount must not hold the app's quit."""
    lane = SetupLane()
    release = threading.Event()
    delivered: list[object] = []
    assert lane.submit(lambda: release.wait(30), lambda result, error: delivered.append(result))
    started = time.monotonic()
    lane.shutdown()
    assert time.monotonic() - started < 1
    assert not lane.submit(lambda: None, lambda result, error: delivered.append(result))
    release.set()
    spin_until(lambda: not lane.busy, what="the released look ending")
    assert delivered == [], "nothing is delivered once shut down"
