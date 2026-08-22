"""Graphical library scans leave GTK responsive and preserve interaction state."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from wall_in_one import config  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.theme import source  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _spin_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not predicate():
        while context.pending():
            context.iteration(False)
        if time.monotonic() >= deadline:
            raise AssertionError("GLib callback did not arrive before the test deadline")
        time.sleep(0.002)


def _item(root: Path) -> MediaItem:
    return MediaItem(
        path=root / "wallpaper.png",
        kind=Kind.STILL,
        size=1,
        mtime=1,
    )


class _ScanWindow:
    """Only the surface touched by Application's scan and settings paths."""

    def __init__(self) -> None:
        self.scanning: list[bool] = []
        self.libraries: list[tuple[Path, ...]] = []
        self.reports: list[str] = []
        self.settings: list[config.Settings] = []

    def show_library_scanning(self, scanning: bool) -> None:
        self.scanning.append(scanning)

    def show_library(self, session: Session) -> None:
        self.libraries.append(session.library.roots)

    def report(self, message: str) -> None:
        self.reports.append(message)

    def apply_settings(self, settings: config.Settings) -> None:
        self.settings.append(settings)

    def show_current(self, _session: Session) -> None: ...


def test_gui_scan_yields_to_gtk_and_only_the_newest_generation_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    started = threading.Event()
    release = threading.Event()
    scans: list[tuple[Path, ...] | None] = []
    worker_threads: list[int] = []
    gtk_thread = threading.get_ident()

    def scanner(roots: Sequence[Path] | None) -> Library:
        worker_threads.append(threading.get_ident())
        snapshot = tuple(roots) if roots is not None else None
        scans.append(snapshot)
        if snapshot == (first,):
            started.set()
            assert release.wait(2)
        resolved = tuple(roots or ())
        return Library(roots=resolved, items=(_item(resolved[0]),))

    application = Application()
    application._session.shutdown()
    settings = config.Settings(roots=(first,), scan_workshop=False).validated()
    session = Session(settings, scanner=scanner)
    window = _ScanWindow()
    application._settings = settings
    application._session = session
    application._window = cast(MainWindow, window)
    monkeypatch.setattr(config, "save", lambda _settings: None)
    monkeypatch.setattr(session, "sync_with_noctalia", lambda: False)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    monkeypatch.setattr(application, "refresh_runtime_status_async", lambda *a, **k: True)
    monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
    monkeypatch.setattr(application, "sync_cycle_timer", lambda: None)

    application.refresh_library()
    assert window.scanning == [True], "busy state must be visible before the scan finishes"
    assert started.wait(1)

    heartbeat: list[bool] = []

    def beat() -> bool:
        heartbeat.append(True)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(beat)
    _spin_until(lambda: bool(heartbeat))
    assert window.libraries == [], "the blocked worker must not block or mutate GTK"

    application.update_settings(roots=(second,))
    release.set()
    _spin_until(lambda: window.libraries == [(second,)])

    assert scans == [(first,), (second,)]
    assert worker_threads and all(identifier != gtk_thread for identifier in worker_threads)
    assert session.library.roots == (second,)
    assert window.libraries == [(second,)], "the stale first generation must never land"
    assert window.scanning == [True, True, False]
    assert window.reports == []

    application._library_scan_shutdown = True
    application._library_scan_generation += 1
    assert application._library_scan_jobs is not None
    application._library_scan_jobs.shutdown(wait=True, cancel_futures=True)
    application._library_scan_jobs = None
    application._window = None
    session.shutdown()


def test_scanning_indicator_preserves_the_grid_search_scroll_and_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    library = Library(roots=(root,), items=(_item(root),))
    monkeypatch.setattr(
        "wall_in_one.ui.thumbnails.ThumbnailLoader.request",
        lambda *_arguments: None,
    )

    class FakeApp(Adw.Application):
        def __init__(self) -> None:
            super().__init__(application_id="dev.goober.AsyncScanPermanenceTest")
            self.settings = config.Settings(roots=(root,), scan_workshop=False)
            self.resolved_palette = source.resolve()
            self.session = Session(self.settings, scanner=lambda _roots: library)
            self.session.refresh()

        def refresh_library(self) -> None: ...

    application = FakeApp()
    window = MainWindow(application, application.settings)  # type: ignore[arg-type]
    window.show_library(application.session)
    search = window._search
    tile = window._grid._tiles[root / "wallpaper.png"]
    search.set_text("wall")
    search.set_position(2)
    window.set_focus(search)
    focused = window.get_focus()
    assert focused is not None
    adjustment = window._grid.get_vadjustment()
    adjustment.configure(37, 0, 100, 1, 10, 10)

    window.show_library_scanning(True)

    assert "scanning library" in window._subtitle.get_subtitle()
    assert not window._refresh_button.get_sensitive()
    assert window._grid._tiles[root / "wallpaper.png"] is tile
    assert window._search is search
    assert search.get_text() == "wall"
    assert search.get_position() == 2
    assert adjustment.get_value() == 37
    assert window.get_focus() is focused

    window.show_library_scanning(False)
    assert "scanning library" not in window._subtitle.get_subtitle()
    assert window._refresh_button.get_sensitive()
    assert window._grid._tiles[root / "wallpaper.png"] is tile
    assert search.get_text() == "wall"
    assert search.get_position() == 2
    assert adjustment.get_value() == 37
    assert window.get_focus() is focused

    window.destroy()
    application.session.shutdown()
