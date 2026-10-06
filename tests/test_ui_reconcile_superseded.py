"""A superseded library reconciliation still says what it couldn't clean up.

A scan's reconciliation replays pending removals and Workshop cleanup on the
authoring lane. When a newer scan supersedes it while it runs, its durable
cleanups are adopted without its Library (`_queue_library_reconciliation`),
and what it couldn't finish was dropped: the replay failure was never said,
by it or by anyone. It is said once now, and the stale Library still never
lands.

The fault is real: the pairings file can't be read (mode 000) while the
replay forgets the removed wallpaper's pairing, and can be again by the
time the newer scan's reconciliation retries it, which then finishes.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_library_scan import (  # noqa: E402,F401
    _item,
    _ScanWindow,
    _stop_scan_application,
    _wire_scan_application,
    toolkit,
)
from wall_in_one import config  # noqa: E402
from wall_in_one.library import favourites, pairings, playlists, removals  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402


def test_a_superseded_replay_says_once_what_it_could_not_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    removed = MediaItem(first / "removed.mp4", Kind.VIDEO, 5, 1)
    replacement = _item(second)
    pairing_path = tmp_path / "pairings.json"
    pairing_store = pairings.Store(path=pairing_path)
    pairing_store.mark_borked(removed, "renderer crashed", "renderer-crash")
    journal = removals.Store.open(tmp_path / "pending-removals.json")
    settings = config.Settings(roots=(first,), scan_workshop=False).validated()

    def scanner(roots: Sequence[Path] | None) -> Library:
        resolved = tuple(roots or ())
        return Library(resolved, (removed,) if resolved == (first,) else (replacement,))

    session = Session(
        settings,
        scanner=scanner,
        favourite_store=favourites.Store(path=tmp_path / "favourites.json"),
        pairing_store=pairing_store,
        playlist_store=playlists.Store(path=tmp_path / "playlists.json"),
        removal_store=journal,
    )
    session.adopt_library(Library((first,), (removed,)))
    journal.record_external(removed, (first,))
    application = Application(ui="classic")
    window = _ScanWindow()
    _wire_scan_application(application, session, window, monkeypatch)

    started = threading.Event()
    release = threading.Event()
    attempts: list[str] = []
    original_forget = pairings.Store.forget_item

    def held_forget(
        store: pairings.Store, item: MediaItem, *, removed_stills: Sequence[Path] = ()
    ) -> bool:
        if item.path != removed.path:
            return original_forget(store, item, removed_stills=removed_stills)
        attempts.append("forget")
        if len(attempts) > 1:
            return original_forget(store, item, removed_stills=removed_stills)
        started.set()
        assert release.wait(5)
        mode = pairing_path.stat().st_mode
        pairing_path.chmod(0)  # the first replay meets a file it can't read
        try:
            return original_forget(store, item, removed_stills=removed_stills)
        finally:
            pairing_path.chmod(mode)

    monkeypatch.setattr(pairings.Store, "forget_item", held_forget)
    try:
        application.refresh_library()
        spin_until(started.is_set)
        application.update_settings(roots=(second,))  # a newer scan supersedes it
        release.set()
        spin_until(lambda: window.libraries == [(second,)], timeout=5)
        spin_until(lambda: not application._authoring_active and not application._authoring_queue)

        assert window.libraries == [(second,)], "the stale first Library never lands"
        assert len(attempts) == 2, "the newer scan's reconciliation retried it"
        unfinished = [r for r in window.reports if "Pending removal cleanup" in r]
        # Before: nothing at all, though the first replay stopped.
        assert len(unfinished) == 1, window.reports
        assert "unreadable" in unfinished[0] or "Permission denied" in unfinished[0], unfinished
        assert removals.Store.open(tmp_path / "pending-removals.json").records == ()
        assert session.pairings.get(pairings.Identity.of(removed)) is None
    finally:
        release.set()
        _stop_scan_application(application, session)
