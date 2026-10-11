"""The new interface's playlist editing, over the real store, without a display.

`RealPlaylists` (``AppState.playlist_editing``) behind a real `RealAppState`, a
real `Session` with its stores in the test's own XDG dirs, and the
`FakeApplication` of ``test_next_state``: authoring tasks wait in its queue
until `run_authoring`, so each test can see what is shown before and after the
store says so.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_next_state import FakeApplication, _adapter, _library, _session, _topics
from wall_in_one import paths
from wall_in_one.library import playlists
from wall_in_one.library.model import MediaItem
from wall_in_one.runtime_config import Playability, leaves_nothing_to_play
from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME
from wall_in_one.ui.next.playlists_state import (
    EDITS_TOPIC,
    PlaylistEditing,
    interval_words,
    usage_words,
)
from wall_in_one.ui.next.real_playlists import (
    RealPlaylists,
    hidden,
    rule_words,
)
from wall_in_one.ui.next.real_state import RealAppState


@pytest.fixture
def items(tmp_path: Path) -> tuple[MediaItem, ...]:
    return _library(tmp_path / "home" / "Pictures" / "Wallpapers")


@pytest.fixture
def backend(items: tuple[MediaItem, ...]) -> Iterator[FakeApplication]:
    session = _session(items)
    try:
        yield FakeApplication(session)
    finally:
        session.shutdown()


Toasts = list[tuple[str, Any]]


def _editor(backend: FakeApplication) -> tuple[RealAppState, PlaylistEditing, Toasts]:
    adapter, _keeper = _adapter(backend)
    editing = adapter.playlist_editing
    assert isinstance(editing, RealPlaylists)
    toasts: Toasts = []
    adapter.connect("toast", lambda _state, text, button: toasts.append((text, button)))
    return adapter, editing, toasts


def _evening(backend: FakeApplication, count: int = 3) -> str:
    """A saved playlist "Evening" with the first ``count`` wallpapers, entries e0, e1, …"""
    store = backend.session.playlists
    store.create("Evening", entry_id="evening")
    for index, item in enumerate(backend.session.library.items[:count]):
        store.add("evening", item.path, entry_id=f"e{index}")
    return "evening"


def _ids(backend: FakeApplication, pid: str = "evening") -> list[str]:
    return [entry.id for entry in backend.session.playlists.find(pid).entries]


def _file() -> Path:
    return paths.app_state_dir() / playlists.STATE_FILENAME


# -- the store's batch transactions -------------------------------------------------------


def test_insert_entries_is_one_transaction_that_keeps_entry_ids(tmp_path: Path) -> None:
    store = playlists.Store.open(tmp_path / "playlists.json")
    store.create("Evening", entry_id="evening")
    a, b, c = (playlists.Entry(f"x{index}", f"/media/{index}.png") for index in range(3))
    updated = store.insert_entries("evening", [(0, a), (1, b)])
    assert [entry.id for entry in updated.entries] == ["x0", "x1"]
    # Positions count in the list as it stands after the pairs before them.
    updated = store.insert_entries("evening", [(1, c)])
    assert [entry.id for entry in updated.entries] == ["x0", "x2", "x1"]
    before = (tmp_path / "playlists.json").read_bytes()
    with pytest.raises(playlists.PlaylistError) as refused:
        store.insert_entries("evening", [(0, playlists.Entry("new", "/media/n.png")), (0, a)])
    assert refused.value.kind == "identity-conflict"
    with pytest.raises(playlists.PlaylistError) as relative:
        store.insert_entries("evening", [(0, playlists.Entry("rel", "media/n.png"))])
    assert relative.value.kind == "validation"
    assert (tmp_path / "playlists.json").read_bytes() == before, "all of them or none"
    assert playlists.Store.open(tmp_path / "playlists.json").find("evening") == updated


def test_insert_entries_refuses_past_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists, "MAX_ENTRIES", 2)
    store = playlists.Store.open(tmp_path / "playlists.json")
    store.create("Evening", entry_id="evening")
    entries = [(index, playlists.Entry(f"x{index}", f"/media/{index}.png")) for index in range(3)]
    with pytest.raises(playlists.PlaylistError) as full:
        store.insert_entries("evening", entries)
    assert full.value.kind == "full"
    assert store.find("evening").entries == ()


def test_remove_entries_is_all_or_nothing(tmp_path: Path) -> None:
    store = playlists.Store.open(tmp_path / "playlists.json")
    store.create("Evening", entry_id="evening")
    for index in range(4):
        store.add("evening", Path(f"/media/{index}.png"), entry_id=f"x{index}")
    before = (tmp_path / "playlists.json").read_bytes()
    with pytest.raises(playlists.PlaylistError) as missing:
        store.remove_entries("evening", ["x1", "gone"])
    assert missing.value.kind == "no-such-entry"
    assert (tmp_path / "playlists.json").read_bytes() == before
    updated = store.remove_entries("evening", ["x1", "x3"])
    assert [entry.id for entry in updated.entries] == ["x0", "x2"]


# -- create, rename, duplicate, delete -------------------------------------------------------


def test_create_rename_and_delete_show_what_is_saved(backend: FakeApplication) -> None:
    adapter, editing, toasts = _editor(backend)
    seen = _topics(adapter)
    pid = editing.create_playlist("Morning")
    assert editing.creating(pid) and not adapter.has_playlist(pid), "nothing until it is saved"
    backend.run_authoring()
    assert not editing.creating(pid) and adapter.has_playlist(pid)
    assert adapter.playlist(pid).name == "Morning" and "playlists" in seen

    editing.rename_playlist(pid, "  Early   morning ")
    detail = editing.detail(pid)
    assert detail is not None and detail.name == "Early morning", "the name asked for, meanwhile"
    assert backend.session.playlists.find(pid).name == "Morning"
    backend.run_authoring()
    assert backend.session.playlists.find(pid).name == "Early morning"

    backend.session.schedules.add(pid, weekdays=("mon",), start="07:00", end="09:00")
    backend.session.displays.assign("DP-1", pid)
    warning = editing.delete_warning(pid)
    assert warning.startswith("This removes 0 playlist entries, 1 schedule rule and 1 display")
    assert warning.endswith("Wallpaper files stay in the library. This cannot be undone.")
    editing.delete_playlist(pid)
    assert adapter.has_playlist(pid), "deleted once saved"
    backend.run_authoring()
    assert not adapter.has_playlist(pid)
    assert not backend.session.schedules.rules and not backend.session.displays.all()
    assert toasts[-1][0] == "Deleted \u201cEarly morning\u201d"


def test_a_failed_create_or_rename_goes_back_to_what_is_saved(backend: FakeApplication) -> None:
    pid = _evening(backend)
    adapter, editing, _toasts = _editor(backend)
    backend.session.playlists.create("Taken")
    made = editing.create_playlist("taken")  # names are unique, case-insensitively
    backend.run_authoring()
    assert not editing.creating(made) and not adapter.has_playlist(made)
    assert "\u201ctaken\u201d was not created; nothing changed" in backend.reports[-1]

    seen = _topics(adapter)
    editing.rename_playlist(pid, "Taken")
    detail = editing.detail(pid)
    assert detail is not None and detail.name == "Taken"
    backend.run_authoring()
    detail = editing.detail(pid)
    assert detail is not None and detail.name == "Evening", "back on the saved name"
    assert "\u201cEvening\u201d was not renamed; nothing changed" in backend.reports[-1]
    assert EDITS_TOPIC in seen


def test_duplicate_copies_entries_and_rotation_as_one_task(backend: FakeApplication) -> None:
    pid = _evening(backend)
    backend.session.playlists.set_rotation(pid, cycle_interval=900, shuffle=True)
    adapter, editing, _toasts = _editor(backend)
    copy = editing.duplicate_playlist(pid)
    assert len(backend.authoring) == 1
    backend.run_authoring()
    made = backend.session.playlists.find(copy)
    assert made.name == "Evening (copy)"
    assert [entry.source for entry in made.entries] == [
        entry.source for entry in backend.session.playlists.find(pid).entries
    ]
    assert set(_ids(backend, copy)).isdisjoint(_ids(backend, pid)), "its own entries"
    assert (made.cycle_interval, made.shuffle) == (900, True)
    every = editing.duplicate_playlist("all-media")
    backend.run_authoring()
    assert backend.session.playlists.find(every).name == "All wallpapers (copy)"
    assert len(backend.session.playlists.find(every).entries) == len(adapter.wallpapers)


# -- "Your pick" stays hidden ------------------------------------------------------------------


def test_your_pick_is_hidden_and_never_changed(backend: FakeApplication) -> None:
    store = backend.session.playlists
    item = backend.session.library.items[0]
    store.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, item.path)
    display = store.set_display_singleton("DP-1", item.path)
    _evening(backend)
    adapter, editing, _toasts = _editor(backend)
    before = _file().read_bytes()
    assert [playlist.id for playlist in adapter.playlists] == ["evening", "all-media"]
    for pid in (QUICK_CHOICE_ID, display.id, "all-media"):
        assert hidden(pid)
        assert editing.detail(pid) is None
        editing.rename_playlist(pid, "Mine now")
        editing.delete_playlist(pid)
        stored = store.get(pid)
        editing.remove_entries(pid, [entry.id for entry in stored.entries] if stored else ["x"])
        editing.move_entry(pid, "x", 1)
        editing.add_entries(pid, [str(item.path)])
        editing.set_playlist_interval(pid, 300)
        editing.make_default(pid)
    assert not backend.authoring and not backend.reports
    assert _file().read_bytes() == before
    assert store.get(QUICK_CHOICE_ID) is not None and store.get(display.id) is not None


# -- adding and moving -------------------------------------------------------------------------


def test_adding_many_is_one_task_and_one_save(
    backend: FakeApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _evening(backend, count=1)
    adapter, editing, toasts = _editor(backend)
    saves: list[int] = []
    insert = backend.session.playlists.insert_entries

    def counted(identifier: str, placed: Any) -> playlists.Playlist:
        saves.append(len(placed))
        return insert(identifier, placed)

    monkeypatch.setattr(backend.session.playlists, "insert_entries", counted)
    wids = [view.id for view in adapter.wallpapers[1:3]]
    editing.add_entries(pid, wids, position=0)
    assert len(backend.authoring) == 1
    detail = editing.detail(pid)
    assert detail is not None and detail.busy and len(detail.entries) == 1
    backend.run_authoring()
    assert saves == [2]
    entries = backend.session.playlists.find(pid).entries
    assert [entry.source for entry in entries] == [
        *wids,
        str(backend.session.library.items[0].path),
    ]
    assert toasts[-1][0] == "Added 2 wallpapers to \u201cEvening\u201d"
    detail = editing.detail(pid)
    assert detail is not None and not detail.busy


def test_a_wallpaper_playback_skips_is_not_added(backend: FakeApplication) -> None:
    pid = _evening(backend, count=1)
    items = backend.session.library.items
    backend.session.pairings.mark_borked(items[3], "codec missing", "runtime")
    adapter, editing, _toasts = _editor(backend)
    adapter.reload()
    editing.add_entries(pid, [str(items[2].path), str(items[3].path)])
    assert not backend.authoring, "refused before anything is queued"
    assert backend.reports[-1] == (
        "Playback unavailable for rain; it can\u2019t be added: codec missing"
    )

    # Turned bad while the add waited in the queue: refused at its head, whole.
    editing.add_entries(pid, [str(items[1].path), str(items[2].path)])
    backend.session.pairings.mark_borked(items[2], "renderer stopped", "runtime")
    backend.run_authoring()
    assert len(backend.session.playlists.find(pid).entries) == 1
    assert "nothing changed: playback became unavailable for lily-pond" in backend.reports[-1]


def test_queued_relative_moves_apply_in_order_to_what_each_saved(
    backend: FakeApplication,
) -> None:
    pid = _evening(backend, count=4)
    editing = _editor(backend)[1]
    editing.move_entry(pid, "e0", 1)
    editing.move_entry(pid, "e0", 1)
    editing.move_entry(pid, "e3", -3)
    assert len(backend.authoring) == 3
    detail = editing.detail(pid)
    assert detail is not None and detail.busy
    assert [entry.id for entry in detail.entries] == ["e0", "e1", "e2", "e3"], "saved order"
    backend.run_authoring()
    assert _ids(backend) == ["e3", "e1", "e2", "e0"]
    assert backend.playlist_changes == 3
    detail = editing.detail(pid)
    assert detail is not None and not detail.busy


def test_a_move_past_either_end_writes_and_publishes_nothing(backend: FakeApplication) -> None:
    pid = _evening(backend)
    adapter, editing, _toasts = _editor(backend)
    before = (_file().read_bytes(), _file().stat().st_mtime_ns)
    seen = _topics(adapter)
    editing.move_entry(pid, "e0", -1)
    editing.move_entry(pid, "e2", 5)
    backend.run_authoring()
    assert (_file().read_bytes(), _file().stat().st_mtime_ns) == before
    assert backend.playlist_changes == 0 and "playlists" not in seen
    assert seen.count(EDITS_TOPIC) >= 2, "the page still hears that the moves settled"


def test_a_failed_move_invents_nothing_and_shows_what_is_saved(
    backend: FakeApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _evening(backend, count=4)
    adapter, editing, _toasts = _editor(backend)
    store = backend.session.playlists
    move = store.move_entry_relative
    calls = 0

    def second_fails(identifier: str, entry: str, step: int) -> playlists.Playlist:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise playlists.PlaylistError("local-io", "test disk is full")
        return move(identifier, entry, step)

    monkeypatch.setattr(store, "move_entry_relative", second_fails)
    for entry, step in (("e0", 1), ("e0", 1), ("e0", 1)):
        editing.move_entry(pid, entry, step)
    seen = _topics(adapter)
    backend.run_authoring()
    assert _ids(backend) == ["e1", "e2", "e0", "e3"], "the first and third, each from saved"
    assert len(backend.reports) == 1 and "test disk is full" in backend.reports[0]
    assert "nothing changed" in backend.reports[0]
    detail = editing.detail(pid)
    assert detail is not None and not detail.busy
    assert [entry.id for entry in detail.entries] == _ids(backend)
    assert EDITS_TOPIC in seen


# -- removing, and Undo -------------------------------------------------------------------------


def test_removal_offers_undo_that_restores_the_same_entries_in_place(
    backend: FakeApplication,
) -> None:
    pid = _evening(backend, count=4)
    _state, editing, toasts = _editor(backend)
    editing.remove_entries(pid, ["e0", "e2"])
    assert not toasts, "Undo is offered once the removal is saved"
    backend.run_authoring()
    assert _ids(backend) == ["e1", "e3"]
    text, button = toasts[-1]
    assert text == "Removed 2 wallpapers from \u201cEvening\u201d"
    label, undo = button
    assert label == "Undo"
    undo()
    assert _ids(backend) == ["e1", "e3"], "Undo is a task on the lane too"
    backend.run_authoring()
    assert _ids(backend) == ["e0", "e1", "e2", "e3"]


def test_undo_is_ordered_on_the_lane_and_expires_once_the_playlist_changed(
    backend: FakeApplication,
) -> None:
    pid = _evening(backend, count=3)
    _state, editing, toasts = _editor(backend)
    editing.remove_entries(pid, ["e1"])
    backend.run_authoring()
    _text, (_label, undo) = toasts[-1]

    # Undo queued before a move: it restores first, the move applies after.
    undo()
    editing.move_entry(pid, "e1", 1)
    backend.run_authoring()
    assert _ids(backend) == ["e0", "e2", "e1"]

    editing.remove_entries(pid, ["e0"])
    backend.run_authoring()
    _text, (_label, undo) = toasts[-1]
    # A move queued before the Undo changes the playlist: the Undo says so.
    editing.move_entry(pid, "e1", -1)
    undo()
    backend.run_authoring()
    assert _ids(backend) == ["e1", "e2"]
    assert "has changed since, so the removal can\u2019t be undone" in backend.reports[-1]


# -- nothing playable -----------------------------------------------------------------------------


def test_a_playlist_with_nothing_to_play_cannot_become_the_default(
    backend: FakeApplication,
) -> None:
    _evening(backend, count=0)
    adapter, editing, _toasts = _editor(backend)
    detail = editing.detail("evening")
    assert detail is not None and detail.default_blocked == "Add wallpapers to it first"
    editing.make_default("evening")
    assert not backend.authoring and backend.settings.active_playlist == ""
    assert backend.reports[-1].startswith("\u201cEvening\u201d can\u2019t be the default")

    # Every entry missing or skipped is the same as empty.
    store = backend.session.playlists
    store.add("evening", Path("/not/mounted.png"), entry_id="gone")
    items = backend.session.library.items
    store.add("evening", items[3].path, entry_id="skipped")
    backend.session.pairings.mark_borked(items[3], "codec missing", "runtime")
    adapter.reload()
    detail = editing.detail("evening")
    assert detail is not None and detail.usable == 0 and detail.unavailable == 2
    assert detail.entries[0].missing and detail.entries[1].problem == "codec missing"
    editing.make_default("evening")
    assert not backend.authoring


def test_a_used_playlist_keeps_something_to_play(backend: FakeApplication) -> None:
    pid = _evening(backend, count=2)
    editing = _editor(backend)[1]
    backend.session.schedules.add(pid, weekdays=("sat", "sun"))
    editing.remove_entries(pid, ["e0"])
    backend.run_authoring()
    assert _ids(backend) == ["e1"]
    editing.remove_entries(pid, ["e1"])
    backend.run_authoring()
    assert _ids(backend) == ["e1"], "refused at the head of the queue"
    assert (
        "\u201cEvening\u201d is used by a schedule rule and would have nothing left to play"
        in backend.reports[-1]
    )


def test_a_default_asked_for_counts_before_it_is_saved(backend: FakeApplication) -> None:
    pid = _evening(backend, count=1)
    editing = _editor(backend)[1]
    editing.make_default(pid)
    detail = editing.detail(pid)
    assert detail is not None and detail.is_default, "shown as asked"
    # Queued behind the default: by then it is the default, so it is refused.
    editing.remove_entries(pid, ["e0"])
    backend.run_authoring()
    assert backend.settings.active_playlist == pid and _ids(backend) == ["e0"]

    # A default asked for whose write is still behind a removal (a settings
    # batch was already running) guards that removal all the same.
    second = backend.session.playlists.create("Second", entry_id="second")
    backend.session.playlists.add(second.id, backend.session.library.items[1].path, entry_id="s0")
    editing.make_default("second")
    editing.remove_entries("second", ["s0"])
    settings_write = backend.authoring.popleft()
    backend.run_authoring()  # the removal, first
    assert [entry.id for entry in backend.session.playlists.find("second").entries] == ["s0"]
    assert "is used by the schedule default and would have nothing left" in backend.reports[-1]
    settings_write()
    assert backend.settings.active_playlist == "second"

    # A default that fails goes back to the saved one.
    backend.settings_failure = "settings are read-only"
    editing.make_default(pid)
    detail = editing.detail(pid)
    assert detail is not None and detail.is_default
    backend.run_authoring()
    detail = editing.detail(pid)
    assert detail is not None and not detail.is_default
    assert backend.settings.active_playlist == "second"
    assert backend.reports[-1] == (
        "\u201cEvening\u201d was not made the default; nothing changed: settings are read-only"
    )


def test_cleanup_of_an_already_unplayable_used_playlist_is_allowed() -> None:
    usable = Playability(compiles=True, plays=True)
    skipped = Playability(compiles=True, plays=False)
    missing = Playability(compiles=False, plays=False)
    assert leaves_nothing_to_play([usable, skipped], [skipped])
    assert not leaves_nothing_to_play([usable, usable], [usable])
    assert not leaves_nothing_to_play([skipped, skipped], [skipped]), "nothing usable to lose"
    assert leaves_nothing_to_play([skipped, missing], [missing]), "nothing it could compile"
    assert leaves_nothing_to_play([skipped], [])


# -- how it plays -------------------------------------------------------------------------------


def test_interval_and_shuffle_are_the_playlists_own_and_bump_the_file_once(
    backend: FakeApplication,
) -> None:
    pid = _evening(backend)
    editing = _editor(backend)[1]
    assert json.loads(_file().read_text())["version"] == 1
    editing.set_playlist_interval(pid, 900)
    detail = editing.detail(pid)
    assert detail is not None and detail.rotation.interval == 900, "the choice, meanwhile"
    assert backend.session.playlists.find(pid).cycle_interval is None
    backend.run_authoring()
    assert backend.session.playlists.find(pid).cycle_interval == 900
    assert json.loads(_file().read_text())["version"] == 2, "bumped on first use"
    assert not _file().with_name("playlists.json.v1-backup").exists(), "and no copy is kept"
    editing.set_playlist_shuffle(pid, True)
    backend.run_authoring()
    editing.set_playlist_interval(pid, None)
    editing.set_playlist_shuffle(pid, None)
    backend.run_authoring()
    saved = backend.session.playlists.find(pid)
    assert (saved.cycle_interval, saved.shuffle) == (None, None)
    detail = editing.detail(pid)
    assert detail is not None
    assert (detail.rotation.default_interval, detail.rotation.default_shuffle) == (300, False)


def test_a_failed_interval_goes_back_to_the_saved_one(
    backend: FakeApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _evening(backend)
    editing = _editor(backend)[1]

    def refuse(*_args: object, **_kwargs: object) -> playlists.Playlist:
        raise playlists.PlaylistError("local-io", "could not save playlists.json")

    monkeypatch.setattr(backend.session.playlists, "set_rotation", refuse)
    editing.set_playlist_interval(pid, 3600)
    editing.set_playlist_interval(pid, 900)
    backend.authoring.popleft()()  # the first fails; the latest choice still shows
    detail = editing.detail(pid)
    assert detail is not None and detail.rotation.interval == 900
    backend.run_authoring()
    detail = editing.detail(pid)
    assert detail is not None and detail.rotation.interval is None, "back on the saved value"
    assert len(backend.reports) == 2


# -- read-only ----------------------------------------------------------------------------------


def test_files_from_a_newer_version_turn_playlist_editing_off(items: tuple[MediaItem, ...]) -> None:
    target = _file()
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 99,
        "playlists": [
            {
                "id": "evening",
                "name": "Evening",
                "entries": [{"id": "e0", "source": str(items[0].path)}],
            }
        ],
    }
    target.write_text(json.dumps(document))
    before = target.read_bytes()
    session = _session(items)
    try:
        backend = FakeApplication(session)
        adapter, editing, _toasts = _editor(backend)
        blocked = editing.editing_blocked()
        assert blocked == (
            "Playlist changes are off while playlists.json is from a newer version of Wall-in-One"
        )
        assert "playlists.json" in adapter.read_only_notice()
        detail = editing.detail("evening")
        assert detail is not None
        assert detail.play_blocked == blocked and detail.default_blocked == blocked
        editing.create_playlist("Morning")
        editing.rename_playlist("evening", "Night")
        editing.remove_entries("evening", ["e0"])
        editing.move_entry("evening", "e0", 1)
        editing.set_playlist_interval("evening", 900)
        editing.delete_playlist("evening")
        assert not backend.authoring
        assert set(backend.reports) == {blocked}
        detail = editing.detail("evening")
        assert detail is not None and detail.name == "Evening" and detail.rotation.interval is None
        assert target.read_bytes() == before
    finally:
        session.shutdown()


# -- words ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "text"),
    [
        ({}, "Always"),
        (
            {"weekdays": ("mon", "tue", "wed", "thu", "fri"), "start": "07:00", "end": "18:00"},
            "Weekdays 07:00\u201318:00",
        ),
        ({"weekdays": ("sat", "sun")}, "Weekends, all day"),
        ({"months": (12,)}, "All of Dec"),
        (
            {"months": (6, 7, 8), "start": "20:00", "end": "23:00"},
            "Every day 20:00\u201323:00 in Jun\u2013Aug",
        ),
        ({"weekdays": ("mon", "wed"), "connector": "DP-1"}, "Mon, Wed, all day · DP-1"),
    ],
)
def test_rules_are_said_in_words(
    backend: FakeApplication, changes: dict[str, Any], text: str
) -> None:
    rule = backend.session.schedules.add("evening", **changes)
    assert rule_words(rule) == text


def test_usage_and_interval_words() -> None:
    from wall_in_one.ui.next.playlists_state import Usage

    assert interval_words(900) == "Every 15 minutes"
    assert interval_words(3600) == "Every hour"
    assert interval_words(7200) == "Every 2 hours"
    assert interval_words(86400) == "Once a day"
    assert interval_words(90) == "Every 90 seconds"
    used = (
        Usage("default", "When nothing is scheduled"),
        Usage("rule", "Weekends"),
        Usage("rule", "Evenings"),
        Usage("display", "DP-1", target="displays"),
    )
    assert usage_words(used) == "the schedule default, 2 schedule rules and DP-1"
    assert usage_words(()) == ""


def test_on_screen_says_where_and_why(backend: FakeApplication) -> None:
    from tests.test_next_status_line import two_display_status

    pid = _evening(backend)
    editing = _editor(backend)[1]
    status = two_display_status("playing")
    displays = status["displays"]
    assert isinstance(displays, list)
    for record in displays:
        record["playlist_id"] = pid
        record["playlist"] = "Evening"
        record["route_source"] = "manual"
        record["entry_id"] = "e1"
    backend.status_model.adopt(status)
    detail = editing.detail(pid)
    assert detail is not None
    assert detail.on_screen == "On screen now · your pick" and detail.playing
    assert {entry for entry, _connector in detail.showing} == {"e1"}
    displays[1]["playlist_id"] = "other"
    displays[1]["route_source"] = "schedule"
    backend.status_model.adopt(status)
    detail = editing.detail(pid)
    assert detail is not None
    assert detail.on_screen == f"On {displays[0]['connector']} now · your pick"
    assert not detail.playing
