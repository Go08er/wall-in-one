"""Astra's seventh full review of 0.3.0, AD-3: the new window's Add and Undo ask the guard.

The new window's Add, and the removal toast's Undo, saved their entries
without the saved-state guard the classic window and ``ctl`` ask
(`runtime_config.checked_entries_addition`). Another program removes a
library folder; a tile from it is then added to an empty playlist, or put
back by Undo, as its only entry: an entry the next scan can't resolve.

Both now ask it, on the worker, of what is saved at that moment (``the guard
asks``; those cases fail on a5999e2). And each gesture is also run as it went
before the fix (``the guard doesn't ask``: the write lands), ending in a
publication: the compiler builds without a playlist that can't play, so even
a path no guard covers no longer stops what follows it. Those cases fail on
a5999e2 too, where the compile refused "playlist ... has no playable
entries" and kept the last document.

The real application behind the new window, real scans, real stores.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1c import (  # noqa: E402, F401
    Shown,
    shown,
    toolkit,
)
from tests.whole_document import not_in_the_document  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.library import pairings, playlists, scan, workshop  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui.next.real_playlists import RealPlaylists  # noqa: E402
from wall_in_one.ui.next.real_state import RealAppState  # noqa: E402

GUARD = ("the guard asks", "the guard doesn't ask")
ONLY_UNUSABLE = "would hold only wallpapers playback can\u2019t use"


def _editing(shown: Shown) -> RealPlaylists:  # noqa: F811
    """The new window's Playlists part, showing the library the application holds now."""
    state = shown.opened.window.state
    assert isinstance(state, RealAppState)
    state.reload()
    editing = state.playlist_editing
    assert isinstance(editing, RealPlaylists)
    return editing


def _as_before_the_fix(monkeypatch: pytest.MonkeyPatch, guard: str) -> None:
    """``the guard doesn't ask``: the worker-side guards let every write through."""
    if guard == GUARD[0]:
        return

    def unasked(
        _reference: str, _what: object, work: Callable[[], Any], *_a: Any, **_k: Any
    ) -> Any:
        return work

    monkeypatch.setattr(runtime_config, "checked_entries_addition", unasked)
    monkeypatch.setattr(runtime_config, "checked_entries_removal", unasked)


def _caught_up(shown: Shown) -> None:  # noqa: F811
    """The application as its next scan leaves it: what is saved, scanned from disk."""
    application = shown.opened.application
    settings = config.load_strict()
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    library = scan.scan(
        settings.roots,
        dict(pairings.Store.open().records),
        include_workshop=settings.scan_workshop,
        workshop_roots=workshop.steam_roots(),
    )
    application.session.adopt_library(library, reconcile_workshop=False)
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)


def _published(shown: Shown) -> dict[str, Any]:  # noqa: F811
    """One publication through the application's own lane; the document it left."""
    application = shown.opened.application
    assert application._publish_runtime_async()
    shown.opened.wait(
        "the publication",
        lambda: (
            not application._runtime_compile_pending and not application._runtime_publication_held
        ),
    )
    assert application.publication_problem == "", "nothing stops a publication any more"
    return tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))


def _names(document: dict[str, Any]) -> set[str]:
    return {playlist["name"] for playlist in document["playlists"]}


def _as_saved() -> list[str]:
    """What a compile of the profile on disk is built without (`tests.whole_document`)."""
    settings = config.load_strict()
    fresh = Session(settings, owns_playback=False)
    try:
        fresh.adopt_library(fresh.prepare_scan().run(), reconcile_workshop=False)
        return not_in_the_document(settings, fresh)
    finally:
        fresh.shutdown()


# -- AD-3: Add and Undo in the new window ------------------------------------------------------


@pytest.mark.parametrize("guard", GUARD)
@pytest.mark.parametrize("gesture", ["Add", "Undo"])
def test_ad3_a_tile_from_a_folder_another_program_removed_is_not_made_a_sole_entry(
    shown: Shown,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    gesture: str,
    guard: str,
) -> None:
    """The external edit has finished before the gesture, and nothing here was told.

    An empty target, so no other entry that plays can hide it. With the
    guard, nothing is saved and the person reads why. Without it (a5999e2),
    the stale tile is saved as the only entry; the next scan can't resolve
    it, and the publication after that still gets through.
    """
    _as_before_the_fix(monkeypatch, guard)
    opened = shown.opened
    application = opened.application
    rain = shown.rain  # the one picture in the second folder
    toasts: list[tuple[str, Any]] = []
    opened.window.state.connect("toast", lambda _s, text, button: toasts.append((text, button)))
    spare = application.session.playlists.create("Spare")
    if gesture == "Undo":
        entry = application.session.playlists.add(spare.id, rain.path).entries[0]
    editing = _editing(shown)
    if gesture == "Undo":
        editing.remove_entries(spare.id, [entry.id])
        opened.wait("the removal", lambda: bool(toasts))
        assert playlists.Store.open().find(spare.id).entries == ()

    # Another program saves the library folders without the second one. It
    # has finished; this application holds the settings it had.
    config.update({"roots": (shown.walls,)})
    assert application.settings.roots == (shown.walls, shown.walls2)

    if gesture == "Add":
        editing.add_entries(spare.id, [str(rain.path)])
    else:
        _text, (label, undo) = toasts[-1]
        assert label == "Undo"
        undo()
    opened.wait(f"the {gesture}", lambda: bool(opened.reports) or guard == GUARD[1])
    saved = playlists.Store.open().find(spare.id)

    if guard == GUARD[0]:
        # Failed on a5999e2: the entry was saved.
        assert saved.entries == (), "nothing was saved"
        what = (
            "\u201crain\u201d was not added to \u201cSpare\u201d"
            if gesture == "Add"
            else "The removal from \u201cSpare\u201d was not undone"
        )
        assert opened.reports[-1] == (f"{what}; nothing changed: \u201cSpare\u201d {ONLY_UNUSABLE}")
        assert _as_saved() == []
    else:
        assert [entry.path for entry in saved.entries] == [rain.path], "as it went on a5999e2"
        assert _as_saved() == ["playlist 'Spare' holds 1 entries, none of which resolves"]

    # Either way the application catches up with what is saved, and publishes.
    # Failed on a5999e2 without the guard: "playlist 'Spare' has no playable
    # entries", and the last document was kept.
    _caught_up(shown)
    document = _published(shown)
    assert "Spare" not in _names(document), "empty, or holding nothing that resolves"
    assert str(rain.path) not in paths.runtime_config_path().read_text(encoding="utf-8")
    assert application.left_out_notice == "", "nothing refers to it"


@pytest.mark.parametrize("gesture", ["Add", "Undo"])
def test_ad3_add_and_undo_still_save_in_order_when_nothing_changed_elsewhere(
    shown: Shown,  # noqa: F811
    gesture: str,
) -> None:
    """The guard is asked, and lets the ordinary gesture through, where it was put."""
    opened = shown.opened
    application = opened.application
    toasts: list[tuple[str, Any]] = []
    opened.window.state.connect("toast", lambda _s, text, button: toasts.append((text, button)))
    store = application.session.playlists
    mixed = store.create("Mixed")
    dusk = shown.walls / "dusk.jpg"
    for source in (shown.dawn.path, dusk):
        store.add(mixed.id, source)
    if gesture == "Undo":
        store.add(mixed.id, shown.rain.path)
    editing = _editing(shown)

    if gesture == "Add":
        editing.add_entries(mixed.id, [str(shown.rain.path)], 1)
        opened.wait("the add", lambda: bool(toasts))
    else:
        middle = store.find(mixed.id).entries[0]
        editing.remove_entries(mixed.id, [middle.id])
        opened.wait("the removal", lambda: bool(toasts))
        _text, (_label, undo) = toasts[-1]
        undo()
        opened.wait("Undo", lambda: len(playlists.Store.open().find(mixed.id).entries) == 3)
        restored = playlists.Store.open().find(mixed.id).entries
        assert restored[0] == middle, "the same entry, back where it was"

    assert opened.reports == []
    order = [entry.path for entry in playlists.Store.open().find(mixed.id).entries]
    if gesture == "Add":
        assert order == [shown.dawn.path, shown.rain.path, dusk], "at the place asked for"
    else:
        assert order == [shown.dawn.path, dusk, shown.rain.path]
