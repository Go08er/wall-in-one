"""A store this version holds open, rewritten by something else meanwhile.

It loaded the store; something else saves a change to the file (the released
0.2.2 in a process of its own, standing for any other program, or a second
writer of this version); and the held store then makes a relative edit (add,
rename, reorder, remove). The external change survives the edit: a relative
edit is applied to what is read under the lock, never to what was held
(review question 5).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests import released
from tests import released_faults as faults
from tests import test_released_leftovers_stores as stores
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import ui_prefs
from wall_in_one.library import displays, favourites, pairings, playlists, schedules
from wall_in_one.library.model import Kind, MediaItem

#: The saves the released producer makes, by name and store: the same table
#: for both versions (it is the released API, which this version kept).
_TABLE = stores.PRODUCER.split("def operation")[0].replace(
    'trace_filesystem((file_io, "_rename_noreplace"))', ""
)
#: The released side: one named save of one store, in its own process.
RELEASED_SAVE = (
    _TABLE
    + r"""
import sys

try:
    SAVES[sys.argv[2]][sys.argv[1]]()
    print("DONE", flush=True)
except Exception as error:
    print("REFUSED", type(error).__name__, error, flush=True)
"""
)
NOON = MediaItem(Path("/media/noon.png"), Kind.STILL, 5, 1)
STORES = ("playlists", "displays", "schedules", "favourites", "pairings", "ui_prefs")
#: What each save leaves in its store's file, to know it by.
MARKS = {
    "first": {
        "playlists": "evening",
        "displays": "DP-1",
        "schedules": "first",
        "favourites": "dawn.png",
        "pairings": "Nord",
    },
    "second": {
        "playlists": "morning",
        "displays": "HDMI-A-1",
        "schedules": "second",
        "favourites": "dusk.png",
        "pairings": "dark",
        "ui_prefs": "library",
    },
    "third": {
        "playlists": "night",
        "displays": "DP-2",
        "schedules": "third",
        "favourites": "noon.png",
        "pairings": "noon.png",
        "ui_prefs": "next",
    },
}


def _table() -> dict[str, dict[str, Callable[[], object]]]:
    """The released producer's saves, made with this checkout's modules."""
    namespace: dict[str, Any] = {}
    exec(_TABLE, namespace)
    saves: dict[str, dict[str, Callable[[], object]]] = namespace["SAVES"]
    saves["third"] = {
        "playlists": lambda: playlists.Store.open().create("Night", entry_id="night"),
        "displays": lambda: displays.Store.open().assign("DP-2", "evening"),
        "schedules": lambda: schedules.Store.open().add(
            "evening", weekdays=("thu",), rule_id="third"
        ),
        "favourites": lambda: favourites.Store.open().add(NOON.path),
        "pairings": lambda: pairings.Store.open().choose_mode(NOON, pairings.Mode.DARK),
        "ui_prefs": lambda: ui_prefs.update({"interface": "next"}),
    }
    return saves


def _file(store: str) -> Path:
    namespace: dict[str, Any] = {}
    exec(_TABLE, namespace)
    path: Path = namespace["FILES"][store]()
    return path


def _fault(store: str) -> object:
    """What this checkout says of the store as it is on disk: None if it reads."""
    if store == "ui_prefs":
        try:
            ui_prefs.load()
        except Exception as error:
            return error
        return None
    module = {
        "playlists": playlists,
        "displays": displays,
        "schedules": schedules,
        "favourites": favourites,
        "pairings": pairings,
    }[store]
    return module.Store.open().fault


def _missing(store: str, *saves: str) -> list[str]:
    text = _file(store).read_text(encoding="utf-8").lower()
    return [
        f"{store}: the {save} save's {MARKS[save][store]!r}"
        for save in saves
        if store in MARKS[save] and MARKS[save][store].lower() not in text
    ]


def _released(source: Path, store: str, save: str) -> str:
    done = released.run(source, RELEASED_SAVE, store, save)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout.strip()


#: The relative edits a held store makes, each naming what it must leave alone.
def _edits(store: str) -> list[tuple[str, Callable[[Any], object]]]:
    return {
        "playlists": [
            ("add", lambda held: held.create("Night", entry_id="night")),
            ("rename", lambda held: held.rename("evening", "Dusk")),
            ("remove", lambda held: held.delete("night")),
        ],
        "displays": [
            ("add", lambda held: held.assign("DP-2", "evening")),
            ("remove", lambda held: held.unassign("DP-1")),
        ],
        "schedules": [
            ("add", lambda held: held.add("evening", weekdays=("thu",), rule_id="third")),
            ("rename", lambda held: held.set_name("third", "Late")),
            ("reorder", lambda held: held.move_relative("third", -1)),
            ("remove", lambda held: held.remove("first")),
        ],
        "favourites": [
            ("add", lambda held: held.add(NOON.path)),
            ("remove", lambda held: held.discard(Path("/media/dawn.png"))),
        ],
        "pairings": [("add", lambda held: held.choose_mode(NOON, pairings.Mode.DARK))],
    }[store]


@pytest.mark.parametrize("writer", ["0.2.2", "a second writer"])
def test_an_external_rewrite_survives_a_held_stores_relative_edit(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    """This version holds each store open; something else saves a change to its
    file; the held store then adds, renames, reorders and removes. Whatever
    the edit, the other writer's change is still in the file."""
    faults.scratch_profile(monkeypatch, tmp_path)
    saves = _table()
    problems: list[str] = []
    for store in STORES[:-1]:
        module = {
            "playlists": playlists,
            "displays": displays,
            "schedules": schedules,
            "favourites": favourites,
            "pairings": pairings,
        }[store]
        saves["first"][store]()
        held = module.Store.open()
        if writer == "0.2.2":
            assert _released(released_source, store, "second") == "DONE"
        else:
            saves["second"][store]()
        assert not _missing(store, "second"), "the external change is in the file"
        done = 0
        for name, edit in _edits(store):
            try:
                edit(held)
                done += 1
            except Exception as error:
                print(f"{store}: the held store's {name} was refused: {error}")
            problems += [f"after {name}, LOST {gone}" for gone in _missing(store, "second")]
            if (fault := _fault(store)) is not None:
                problems.append(f"{store}: unreadable after {name}: {fault}")
        assert done, f"{store}: none of the held store's edits went through"
    # The window preferences have no held object: every update reads the file.
    saves["first"]["ui_prefs"]()
    ui_prefs.load()
    if writer == "0.2.2":
        assert _released(released_source, "ui_prefs", "second") == "DONE"
    else:
        saves["second"]["ui_prefs"]()
    saves["third"]["ui_prefs"]()
    problems += [
        f"after an update, LOST {gone}" for gone in _missing("ui_prefs", "second", "third")
    ]
    assert not problems, "\n".join(problems)
