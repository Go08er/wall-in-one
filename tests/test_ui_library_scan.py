"""Graphical library scans leave GTK responsive and preserve interaction state."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from wall_in_one import config  # noqa: E402
from wall_in_one.library import favourites, pairings, playlists, removals, scan  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import LibraryRefreshCancelledError, Session  # noqa: E402
from wall_in_one.theme import noctalia, source  # noqa: E402
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

    def show_runtime_delayed(self) -> None: ...

    def show_runtime_unavailable(self) -> None: ...

    def show_runtime_protocol_error(self, message: str) -> None:
        self.reports.append(message)


def _wire_scan_application(
    application: Application,
    session: Session,
    window: _ScanWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install the common no-side-effect seams for one focused scan test."""
    application._session.shutdown()
    application._settings = session.settings
    application._settings_requested = session.settings
    application._session = session
    application._window = cast(MainWindow, window)
    application._authoring_migration_ready = True
    monkeypatch.setattr(config, "save", lambda _settings: None)
    monkeypatch.setattr(noctalia, "current_wallpaper", lambda **_keywords: None)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    monkeypatch.setattr(application, "refresh_runtime_status_async", lambda *a, **k: True)
    monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
    monkeypatch.setattr(application, "sync_cycle_timer", lambda: None)


def _stop_scan_application(application: Application, session: Session) -> None:
    application._shutdown_library_scan_jobs(wait=True)
    application._shutdown_theme_jobs(wait=True)
    application._shutdown_authoring_jobs()
    application._window = None
    if application._runtime_publication_held:
        application._runtime_publication_held = False
        application.release()
    if application._runtime_library_scan_held:
        application._runtime_library_scan_held = False
        application.release()
    if application._removal_convergence_held:
        application._removal_convergence_held = False
        application.release()
    while application._authoring_lifetime_holds:
        application._authoring_lifetime_holds -= 1
        application.release()
    session.shutdown()


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
    application._settings_requested = settings
    application._session = session
    application._window = cast(MainWindow, window)
    application._authoring_migration_ready = True
    monkeypatch.setattr(config, "save", lambda _settings: None)
    monkeypatch.setattr(noctalia, "current_wallpaper", lambda **_keywords: None)
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
    application._shutdown_theme_jobs(wait=True)
    application._window = None
    session.shutdown()


def test_pre_delete_scan_cannot_land_or_clear_the_removal_taboo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    item = _item(root)
    item.path.write_bytes(b"image")
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    scans = 0

    def scanner(roots: Sequence[Path] | None) -> Library:
        nonlocal scans
        scans += 1
        resolved = tuple(roots or ())
        if scans == 1:
            first_started.set()
            assert release_first.wait(10)
            return Library(resolved, (item,))
        second_started.set()
        assert release_second.wait(10)
        return Library(resolved, ())

    settings = config.Settings(roots=(root,), scan_workshop=False).validated()
    session = Session(settings, scanner=scanner)
    session.adopt_library(Library((root,), (item,)))
    application = Application()
    window = _ScanWindow()
    _wire_scan_application(application, session, window, monkeypatch)
    removed: list[bool] = []
    try:
        application.refresh_library()
        assert first_started.wait(10)
        assert application.remove_item_async(
            item,
            trash=True,
            finish=lambda result: removed.append(result.committed),
        )
        # This verifies ordering, not a two-second removal SLA.  The detached
        # transaction performs real lock/fsync work and shares a loaded Nix
        # release gate with the GUI suite, so retain a finite deadlock bound
        # without racing the held scanner's own deadline.
        _spin_until(lambda: removed == [True], timeout=10)
        assert item.path in application._removed_item_paths
        # The committed delete synchronously redraws the filtered immutable
        # Library; this is not the held pre-delete scan landing.
        assert session.library.items == ()
        assert window.libraries == [(root,)]
        window.libraries.clear()

        release_first.set()
        _spin_until(second_started.is_set, timeout=10)
        assert window.libraries == []
        assert item.path in application._removed_item_paths

        release_second.set()
        _spin_until(lambda: len(window.libraries) == 1, timeout=10)
        assert session.library.items == ()
        assert item.path in application._removed_item_paths
    finally:
        release_first.set()
        release_second.set()
        _stop_scan_application(application, session)


def test_held_removal_replay_yields_and_stale_cleanup_cannot_resurrect_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery I/O stays off GTK and its durable delta survives supersession."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    removed = MediaItem(first / "removed.mp4", Kind.VIDEO, 5, 1)
    replacement = _item(second)
    pairing_store = pairings.Store(path=tmp_path / "pairings.json")
    favourite_store = favourites.Store(path=tmp_path / "favourites.json")
    playlist_store = playlists.Store(path=tmp_path / "playlists.json")
    journal = removals.Store.open(tmp_path / "pending-removals.json")
    pairing_store.mark_borked(removed, "renderer crashed", "renderer-crash")
    favourite_store.add(removed.path)
    authored = playlist_store.create("Saved")
    playlist_store.add(authored.id, removed.path)

    settings = config.Settings(roots=(first,), scan_workshop=False).validated()

    def scanner(roots: Sequence[Path] | None) -> Library:
        resolved = tuple(roots or ())
        items = (removed,) if resolved == (first,) else (replacement,)
        return Library(resolved, items)

    session = Session(
        settings,
        scanner=scanner,
        favourite_store=favourite_store,
        pairing_store=pairing_store,
        playlist_store=playlist_store,
        removal_store=journal,
    )
    session.adopt_library(Library((first,), (removed,)))
    journal.record_external(removed, (first,))
    application = Application()
    window = _ScanWindow()
    _wire_scan_application(application, session, window, monkeypatch)

    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    gtk_thread = threading.get_ident()
    cleanup_threads: list[int] = []
    original_forget = pairings.Store.forget_item

    def held_forget(
        store: pairings.Store,
        item: MediaItem,
        *,
        removed_stills: Sequence[Path] = (),
    ) -> bool:
        if item.path == removed.path:
            cleanup_threads.append(threading.get_ident())
            cleanup_started.set()
            assert release_cleanup.wait(2)
        return original_forget(store, item, removed_stills=removed_stills)

    monkeypatch.setattr(pairings.Store, "forget_item", held_forget)
    application.refresh_library()
    _spin_until(cleanup_started.is_set)

    heartbeat: list[bool] = []

    def beat() -> bool:
        heartbeat.append(True)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(beat)
    _spin_until(lambda: bool(heartbeat))
    assert window.libraries == []

    application.update_settings(roots=(second,))
    release_cleanup.set()
    _spin_until(lambda: window.libraries == [(second,)])

    assert cleanup_threads and all(identifier != gtk_thread for identifier in cleanup_threads)
    assert session.library.roots == (second,)
    assert window.libraries == [(second,)], "the stale first Library must never land"
    assert removed.path not in session.favourites.paths
    assert session.pairings.get(pairings.Identity.of(removed)) is None
    saved = session.playlists.get(authored.id)
    assert saved is not None and saved.entries == ()
    assert removals.Store.open(tmp_path / "pending-removals.json").records == ()

    _stop_scan_application(application, session)


def test_held_workshop_uninstall_cleanup_yields_and_lands_only_explicit_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    content = tmp_path / "steamapps" / "workshop" / "content" / "431960"
    installation = content / "42"
    installation.mkdir(parents=True)
    scene = MediaItem(
        path=installation,
        kind=Kind.SCENE,
        size=1,
        mtime=1,
        provider=scan.WORKSHOP_PROVIDER,
        scene="42",
    )
    chosen = tmp_path / "chosen-still.png"
    chosen.write_bytes(b"user-owned")
    pairing_store = pairings.Store(path=tmp_path / "pairings.json")
    favourite_store = favourites.Store(path=tmp_path / "favourites.json")
    playlist_store = playlists.Store(path=tmp_path / "playlists.json")
    pairing_store.choose_still(scene, chosen)
    pairing_store.mark_borked(scene, "engine crashed", "renderer-crash")
    favourite_store.add(scene.path)
    authored = playlist_store.create("Scenes")
    playlist_store.add(authored.id, scene.path)
    settings = config.Settings(roots=(root,), scan_workshop=True).validated()
    session = Session(
        settings,
        scanner=lambda roots: Library(tuple(roots or ()), ()),
        favourite_store=favourite_store,
        pairing_store=pairing_store,
        playlist_store=playlist_store,
        removal_store=removals.Store.open(tmp_path / "pending-removals.json"),
    )
    session.adopt_library(Library((root,), (scene,)))
    installation.rmdir()

    application = Application()
    window = _ScanWindow()
    _wire_scan_application(application, session, window, monkeypatch)
    authoring_started = threading.Event()
    release_authoring = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    gtk_thread = threading.get_ident()
    cleanup_thread = 0
    original_forget = pairings.Store.forget_item

    def held_forget(
        store: pairings.Store,
        item: MediaItem,
        *,
        removed_stills: Sequence[Path] = (),
    ) -> bool:
        nonlocal cleanup_thread
        if item.path == scene.path:
            cleanup_thread = threading.get_ident()
            cleanup_started.set()
            assert release_cleanup.wait(2)
        return original_forget(store, item, removed_stills=removed_stills)

    monkeypatch.setattr(pairings.Store, "forget_item", held_forget)

    def held_authoring() -> pairings.Pairing:
        authoring_started.set()
        assert release_authoring.wait(2)
        return session.pairings.choose_palette(
            scene,
            pairings.PalettePolicy("builtin", "Nord"),
        )

    assert application.authoring_action_async(held_authoring, lambda _record: None)
    assert authoring_started.wait(1)
    application.refresh_library()
    _spin_until(lambda: bool(application._authoring_queue))
    assert not cleanup_started.is_set()
    release_authoring.set()
    _spin_until(cleanup_started.is_set)
    heartbeat: list[bool] = []

    def beat() -> bool:
        heartbeat.append(True)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(beat)
    _spin_until(lambda: bool(heartbeat))
    assert window.libraries == []

    release_cleanup.set()
    _spin_until(lambda: window.libraries == [(root,)])

    assert cleanup_thread != gtk_thread
    assert session.removed_workshop == (scene,)
    assert scene.path not in session.favourites.paths
    assert session.pairings.get(pairings.Identity.of(scene)) is None
    saved = session.playlists.get(authored.id)
    assert saved is not None and saved.entries == ()
    assert chosen.is_file(), "a manually selected still is an independent library item"

    release_authoring.set()
    _stop_scan_application(application, session)


def test_scan_shutdown_cancels_active_plan_and_discards_late_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    started = threading.Event()
    release = threading.Event()
    scanned = Library((root,), (_item(root),))

    def scanner(_roots: Sequence[Path] | None) -> Library:
        started.set()
        assert release.wait(2)
        return scanned

    settings = config.Settings(roots=(root,), scan_workshop=False).validated()
    session = Session(settings, scanner=scanner)
    application = Application()
    window = _ScanWindow()
    _wire_scan_application(application, session, window, monkeypatch)

    application.refresh_library()
    assert started.wait(1)
    pending = application._library_scan_future
    assert pending is not None
    application._shutdown_library_scan_jobs()
    release.set()
    with pytest.raises(LibraryRefreshCancelledError):
        pending.result(timeout=2)
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)

    assert session.library.items == ()
    assert window.libraries == []
    application._shutdown_theme_jobs(wait=True)
    application._window = None
    session.shutdown()


def test_worker_rebase_treats_open_pairings_deleted_after_prepare_as_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    video = MediaItem(root / "clip.mp4", Kind.VIDEO, 5, 1)
    video.path.write_bytes(b"video")
    chosen = root / "chosen.png"
    chosen.write_bytes(b"still")
    pairing_path = tmp_path / "pairings.json"
    store = pairings.Store.open(pairing_path)
    store.choose_still(video, chosen)
    settings = config.Settings(roots=(root,), scan_workshop=False).validated()
    session = Session(settings, pairing_store=store)

    plan = session.prepare_library_refresh()
    pairing_path.unlink()
    result = plan.run()

    scanned = result.library.find(video.path)
    assert scanned is not None and scanned.paired_still is None
    assert not pairing_path.exists(), "a stale worker snapshot must not recreate an external reset"
    session.shutdown()


def test_worker_rebase_preserves_an_intentional_unsaved_pairing_seed(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    video = MediaItem(root / "clip.mp4", Kind.VIDEO, 5, 1)
    video.path.write_bytes(b"video")
    chosen = root / "chosen.png"
    chosen.write_bytes(b"still")
    identity = pairings.Identity.of(video)
    seeded = pairings.Store(
        {identity.key: pairings.Pairing(identity=identity, still=chosen)},
        tmp_path / "never-written.json",
    )
    session = Session(
        config.Settings(roots=(root,), scan_workshop=False).validated(),
        pairing_store=seeded,
    )

    result = session.prepare_library_refresh().run()

    scanned = result.library.find(video.path)
    assert scanned is not None and scanned.paired_still == chosen
    session.shutdown()


def test_successful_worker_cleanup_clears_only_a_proven_repaired_store_fault(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    removed = MediaItem(root / "removed.png", Kind.STILL, 1, 1)
    pairing_path = tmp_path / "pairings.json"
    pairing_path.write_text("not json", encoding="utf-8")
    pairing_store = pairings.Store.open(pairing_path)
    assert pairing_store.fault is not None
    journal = removals.Store.open(tmp_path / "pending-removals.json")
    journal.record_external(removed, (root,))
    settings = config.Settings(roots=(root,), scan_workshop=False).validated()
    session = Session(
        settings,
        scanner=lambda roots: Library(tuple(roots or ()), ()),
        pairing_store=pairing_store,
        removal_store=journal,
    )
    plan = session.prepare_library_refresh()

    identity = pairings.Identity.of(removed)
    pairings.save({identity.key: pairings.Pairing(identity=identity)}, pairing_path)
    result = plan.run()
    session.adopt_library_refresh(result)

    assert result.replay_failures == ()
    assert session.pairings.fault is None
    assert session.pairings.get(identity) is None
    session.shutdown()


def test_successful_worker_rebase_clears_unchanged_removal_journal_fault(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    journal_path = tmp_path / "pending-removals.json"
    journal_path.write_text("not json", encoding="utf-8")
    journal = removals.Store.open(journal_path)
    assert journal.fault is not None
    session = Session(
        config.Settings(roots=(root,), scan_workshop=False).validated(),
        removal_store=journal,
    )
    plan = session.prepare_library_refresh()
    journal_path.unlink()

    result = plan.run()
    session.adopt_library_refresh(result)

    assert result.replay_failures == ()
    assert session.removal_journal.fault is None
    session.shutdown()


def test_live_wallpaper_query_is_off_gtk_and_cannot_land_in_a_newer_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_item = _item(first)
    second_item = _item(second)
    query_started = threading.Event()
    release_query = threading.Event()
    query_threads: list[int] = []
    query_count = 0
    gtk_thread = threading.get_ident()

    def scanner(roots: Sequence[Path] | None) -> Library:
        resolved = tuple(roots or ())
        item = first_item if resolved == (first,) else second_item
        return Library(roots=resolved, items=(item,))

    def current_wallpaper(**_keywords: object) -> Path:
        nonlocal query_count
        query_count += 1
        query_threads.append(threading.get_ident())
        if query_count == 1:
            query_started.set()
            assert release_query.wait(2.0)
            return first_item.path
        return second_item.path

    application = Application()
    application._session.shutdown()
    settings = config.Settings(roots=(first,), scan_workshop=False).validated()
    session = Session(settings, scanner=scanner)
    window = _ScanWindow()
    application._settings = settings
    application._settings_requested = settings
    application._session = session
    application._window = cast(MainWindow, window)
    application._authoring_migration_ready = True
    adopted: list[Path | None] = []
    sync_to_wallpaper = session.sync_to_wallpaper

    def adopt(active: Path | None) -> bool:
        adopted.append(active)
        return sync_to_wallpaper(active)

    monkeypatch.setattr(noctalia, "current_wallpaper", current_wallpaper)
    monkeypatch.setattr(session, "sync_to_wallpaper", adopt)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    monkeypatch.setattr(application, "refresh_runtime_status_async", lambda *a, **k: True)
    monkeypatch.setattr(application, "_make_missing_stills", lambda: None)

    application.refresh_library()
    _spin_until(query_started.is_set)
    assert session.library.roots == (first,)

    newer = replace(settings, roots=(second,))
    application._settings = newer
    session.update_settings(newer, rescan_library=False)
    application.refresh_library()
    _spin_until(lambda: session.library.roots == (second,))
    release_query.set()

    _spin_until(lambda: query_count == 2)
    _spin_until(lambda: session.cursor is not None and session.cursor.path == second_item.path)
    _spin_until(lambda: adopted == [second_item.path])
    assert query_threads and all(identifier != gtk_thread for identifier in query_threads)

    application._library_scan_shutdown = True
    application._library_scan_generation += 1
    assert application._library_scan_jobs is not None
    application._library_scan_jobs.shutdown(wait=True, cancel_futures=True)
    application._library_scan_jobs = None
    application._shutdown_theme_jobs(wait=True)
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
