"""What a finished pending removal left on purpose is said in the window, once.

A library scan replays pending removals. One that finishes a removal's media
claim may leave the wallpaper's own files where they are (a download finished
at the same path meanwhile; `tests/test_removal_replay_companions.py`). That
is not a cleanup that failed: nothing is retried, and the pending record is
gone. So the window says it as what it is, in a message of its own without
"could not finish" or advice to fix anything, and the next scan, which
replays again, has nothing more to say of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests import test_removal_replay_companions as companions  # noqa: E402
from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_library_scan import (  # noqa: E402,F401
    _ScanWindow,
    _stop_scan_application,
    _wire_scan_application,
    toolkit,
)
from wall_in_one import config  # noqa: E402
from wall_in_one.library import favourites, pairings, playlists, removals  # noqa: E402
from wall_in_one.library.model import Library  # noqa: E402
from wall_in_one.session import LibraryRefreshResult, Session  # noqa: E402
from wall_in_one.ui import app  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402


def _about_a_removal(reports: Sequence[str]) -> list[str]:
    return [said for said in reports if "ending removal" in said]


def test_the_window_says_once_what_a_finished_removal_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drawn = companions.stub_capture(monkeypatch)
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    wallpaper = companions._stopped("sync-failed", tmp_path, root, journal, monkeypatch)
    mine = companions._snapshot(*companions._replacement(wallpaper, drawn, recaptured=True))

    def scanner(roots: Sequence[Path] | None) -> Library:
        return Library(tuple(roots or ()), ())

    session = Session(
        replace(config.Settings(), roots=(root,), scan_workshop=False).validated(),
        scanner=scanner,
        favourite_store=favourites.Store.open(tmp_path / "favourites.json"),
        pairing_store=pairings.Store.open(tmp_path / "pairings.json"),
        playlist_store=playlists.Store.open(tmp_path / "playlists.json"),
        removal_store=removals.Store.open(journal),
    )
    application = Application(ui="classic")
    window = _ScanWindow()
    _wire_scan_application(application, session, window, monkeypatch)

    def scanned(times: int) -> None:
        application.refresh_library()
        spin_until(lambda: len(window.libraries) == times, timeout=5)
        spin_until(lambda: not application._authoring_active and not application._authoring_queue)

    try:
        scanned(1)

        (said,) = _about_a_removal(window.reports)
        assert said.startswith(
            "A pending removal was finished. aurora: another file is at the wallpaper's "
            "path now, so what is there was left as it is: "
        )
        assert str(wallpaper.selector) in said and str(wallpaper.still) in said
        # Not a cleanup that failed: nothing to fix, nothing to retry.
        assert "could not finish" not in said and "Fix the state-directory" not in said
        assert removals.Store.open(journal).records == ()

        scanned(2)

        assert _about_a_removal(window.reports) == [said], "the next scan says nothing more"
        assert companions._snapshot(*mine) == mine
    finally:
        _stop_scan_application(application, session)


def test_a_superseded_scan_says_it_too() -> None:
    """A reconciliation a newer scan superseded says what it did in one message
    (`_unfinished_library_cleanup`): what it left on purpose is part of it,
    each in its own sentence, and more than three are counted."""
    left = tuple(f"wallpaper {number}: left as it is" for number in range(5))
    result = LibraryRefreshResult(
        library=Library((), ()),
        replay_failures=("aurora: favourites: could not write",),
        cleanups=(),
        removed_workshop=(),
        workshop_cleanup_failures=(),
        known_workshop=(),
        pending_workshop_cleanup=(),
        repaired_faults=(),
        replay_left=left,
    )

    said = app._unfinished_library_cleanup(result)

    assert said == (
        "Pending removal cleanup could not finish: aurora: favourites: could not write. "
        "Fix the state-directory problem and refresh to retry. "
        "A pending removal was finished. wallpaper 0: left as it is; wallpaper 1: left as "
        "it is; wallpaper 2: left as it is; and 2 more."
    )
    assert (
        app._unfinished_library_cleanup(replace(result, replay_failures=(), replay_left=())) == ""
    )
