"""Closing the classic window stops the workers it owns, at once.

Final 0.2.0 review F-5: `MainWindow` cleaned up only on GTK's "destroy",
which GTK 4 emits at disposal. A palette preview still waiting in the
pairing editor's pool keeps a callback bound to the window's pages, so the
window was never disposed: its thumbnail loaders, preview pool, Browse page,
playlists page and palette catalog stayed open after the window had closed,
and in resident ``--service`` mode for as long as the process ran.

These drive the real application (see `tests.test_ui_next_window`), with
`noctalia.generate` gated so the previews are genuinely pending when the
window closes. Nothing here starts a ``noctalia`` process.
(Kept apart from the other window tests because these run whole applications.)
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from tests.test_ui_next_window import (  # noqa: E402
    STEP_SECONDS,
    Step,
    application_lanes,
    run_application,
    sandboxed_runtime,
    settled,
)
from wall_in_one.theme import noctalia  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.browse_dialog import BrowsePage  # noqa: E402
from wall_in_one.ui.pairings_page import PairingsPage  # noqa: E402
from wall_in_one.ui.palette_browser import SchemePreview, SchemePreviewLoader  # noqa: E402
from wall_in_one.ui.palette_catalog import PaletteCatalog  # noqa: E402
from wall_in_one.ui.playlists_page import PlaylistsPage  # noqa: E402
from wall_in_one.ui.thumbnails import ThumbnailLoader  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402

SCHEMES = ("m3-tonal-spot", "m3-content", "vibrant")


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class _Gate:
    """`noctalia.generate` held until released, counting the previews it holds."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.released = threading.Event()
        self._lock = threading.Lock()
        self.held: list[str] = []

        def generate(_image: Path, scheme: str = "", **_keywords: object) -> object:
            with self._lock:
                self.held.append(scheme)
            # Bounded even if a test fails before releasing it.
            self.released.wait(timeout=STEP_SECONDS * 6)
            raise noctalia.NoctaliaError("gated preview")

        monkeypatch.setattr(noctalia, "generate", generate)

    def count(self) -> int:
        with self._lock:
            return len(self.held)


class _Shutdowns:
    """Which window-owned components had ``shutdown()`` called, by identity."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._lock = threading.Lock()
        self._stopped: set[int] = set()
        for owner in (
            ThumbnailLoader,
            SchemePreviewLoader,
            PairingsPage,
            BrowsePage,
            PlaylistsPage,
            PaletteCatalog,
        ):
            original = owner.shutdown

            def recorded(self_: object, *, _original: object = original) -> None:
                with self._lock:
                    self._stopped.add(id(self_))
                _original(self_)  # type: ignore[operator]

            monkeypatch.setattr(owner, "shutdown", recorded)

    def all_stopped(self, components: dict[str, int]) -> bool:
        with self._lock:
            return set(components.values()) <= self._stopped

    def missing(self, components: dict[str, int]) -> list[str]:
        with self._lock:
            return [name for name, ident in components.items() if ident not in self._stopped]


def _components(window: MainWindow) -> dict[str, int]:
    """The workers a classic window owns, by identity (holding none of them)."""
    page = window._pairings_page
    return {
        "window thumbnails": id(window._loader),
        "pairing page": id(page),
        "pairing previews": id(page._preview_loader),
        "pairing thumbnails": id(page._thumbnail_loader),
        "Browse page": id(window._browse_page),
        "playlists page": id(window._playlists_page),
        "palette catalog": id(window._palette_catalog),
    }


def _shown(application: Application) -> object:
    """The application's current window, read afresh each time."""
    return application._window


def _request_gated_previews(window: MainWindow, image: Path) -> list[Future[SchemePreview]]:
    """Three previews through the editor's own loader: two running, one queued."""
    page = window._pairings_page
    loader = page._preview_loader
    for scheme in SCHEMES:
        loader.request(image, scheme, page._on_adaptive_preview)
    return [loader._pending[(image, scheme)] for scheme in SCHEMES]


def test_closing_with_previews_pending_stops_the_windows_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    gate = _Gate(monkeypatch)
    shutdowns = _Shutdowns(monkeypatch)
    image = tmp_path / "wallpapers" / "dawn.png"
    application = Application()
    lanes: list[ThreadPoolExecutor] = []
    pools: list[ThreadPoolExecutor] = []
    components: dict[str, int] = {}
    futures: list[Future[SchemePreview]] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, MainWindow)
        yield "the first scan to settle", lambda: settled(application)
        futures.extend(_request_gated_previews(window, image))
        pools.append(window._pairings_page._preview_loader._pool)
        yield "two previews running and one queued", lambda: gate.count() == 2
        components.update(_components(window))
        lanes.extend(application_lanes(application))
        window.close()

    try:
        assert run_application(application, scenario()) == 0
        assert shutdowns.missing(components) == [], "the closed window left workers open"
        assert futures[2].cancelled(), "the queued preview was not cancelled"
        assert pools[0]._shutdown
    finally:
        gate.released.set()
        for pool in (*pools, *lanes):
            pool.shutdown(wait=True)


def test_a_resident_window_is_torn_down_on_close_and_reopens_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    gate = _Gate(monkeypatch)
    shutdowns = _Shutdowns(monkeypatch)
    image = tmp_path / "wallpapers" / "dawn.png"
    application = Application(service=True)
    lanes: list[ThreadPoolExecutor] = []
    pools: list[ThreadPoolExecutor] = []

    def scenario() -> Iterator[Step]:
        first = application._window
        assert isinstance(first, MainWindow)
        yield "the first scan to settle", lambda: settled(application)
        futures = _request_gated_previews(first, image)
        pools.append(first._pairings_page._preview_loader._pool)
        yield "two previews running and one queued", lambda: gate.count() == 2
        closed = _components(first)
        first_id = id(first)
        # Only identities from here on, as nothing else would hold the window.
        first.close()
        del first
        yield (
            "the closed window's workers to stop, with the process still resident",
            lambda: shutdowns.all_stopped(closed),
        )
        assert _shown(application) is None
        assert futures[2].cancelled(), "the queued preview was not cancelled"
        gate.released.set()

        application.activate()
        second = _shown(application)
        assert isinstance(second, MainWindow) and id(second) != first_id
        reopened = _components(second)
        yield "the reopened window's scan to settle", lambda: settled(application)
        assert not set(reopened.values()) & set(closed.values())
        assert shutdowns.missing(reopened) == list(reopened), "the new window starts open"
        delivered: list[SchemePreview] = []
        second._pairings_page._preview_loader.request(image, "muted", delivered.append)
        pools.append(second._pairings_page._preview_loader._pool)
        yield "a preview in the reopened window", lambda: bool(delivered)

        second.close()
        del second
        yield "the second window's workers to stop", lambda: shutdowns.all_stopped(reopened)
        lanes.extend(application_lanes(application))
        application.quit()

    def launch() -> bool:
        # A resident application isn't activated by starting it; a launch does that.
        application.activate()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(launch)
    try:
        assert run_application(application, scenario()) == 0
    finally:
        gate.released.set()
        for pool in (*pools, *lanes):
            pool.shutdown(wait=True)
