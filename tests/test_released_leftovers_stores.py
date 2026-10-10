"""What the released 0.2.2 leaves of a state-file save, and this version after it.

Review rule 4, run rather than read (AB-1's class, outside downloads). The
released stores are the producer, in a child (`tests.released`): playlists,
display assignments, schedules, favourites, pairings and ``ui.toml``, each
through its own ordinary write: a first save, a save over what is there, a
save over a file a person's editor left damaged (the released code moves it to
``.broken`` and publishes without replacing), and the first use of a newer
format (it keeps ``.vN-backup`` first). Each run takes one fault
(`tests.released_faults`), and then a person may edit any one file it left:
the store's own file, a temporary, a ``.broken`` copy, a backup.

This checkout is the consumer, doing what it ordinarily does next: it opens
the store, saves, uses the newer format, runs Tidy up, and opens it again.
Nothing that was ever public or that a person edited may lose a byte, whatever
its name; and the store's own file, which a save rewrites, keeps everything
it held (or is moved aside whole if it can't be read).
"""

from __future__ import annotations

import json
import os
import time
import tomllib
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests import released_faults as faults
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import paths, tidy, ui_prefs
from wall_in_one.library import displays, favourites, pairings, playlists, schedules
from wall_in_one.library.model import Kind, MediaItem

#: The released producer. Its job's arguments: the store, what is there
#: before (``absent``, ``valid``: what the released store itself saved, or
#: ``damaged``: that, cut short as a crashed editor leaves it), and the save.
PRODUCER = r"""
from pathlib import Path

from wall_in_one import file_io, paths, ui_prefs
from wall_in_one.library import displays, favourites, pairings, playlists, schedules
from wall_in_one.library.model import Kind, MediaItem

trace_filesystem((file_io, "_rename_noreplace"))
ITEM = MediaItem(Path("/media/dawn.png"), Kind.STILL, 5, 1)
FILES = {
    "playlists": playlists.state_path,
    "displays": displays.state_path,
    "schedules": schedules.state_path,
    "favourites": favourites.state_path,
    "pairings": pairings.state_path,
    "ui_prefs": paths.ui_prefs_path,
}
SAVES = {
    "first": {
        "playlists": lambda: playlists.Store.open().create("Evening", entry_id="evening"),
        "displays": lambda: displays.Store.open().assign("DP-1", "evening"),
        "schedules": lambda: schedules.Store.open().add(
            "evening", weekdays=("mon",), rule_id="first"
        ),
        "favourites": lambda: favourites.Store.open().add(Path("/media/dawn.png")),
        "pairings": lambda: pairings.Store.open().choose_palette(
            ITEM, pairings.PalettePolicy("builtin", "Nord")
        ),
        "ui_prefs": lambda: ui_prefs.update({"last_page": "store"}),
    },
    "second": {
        "playlists": lambda: playlists.Store.open().create("Morning", entry_id="morning"),
        "displays": lambda: displays.Store.open().assign("HDMI-A-1", "evening"),
        "schedules": lambda: schedules.Store.open().add(
            "evening", weekdays=("tue",), rule_id="second"
        ),
        "favourites": lambda: favourites.Store.open().add(Path("/media/dusk.png")),
        "pairings": lambda: pairings.Store.open().choose_mode(ITEM, pairings.Mode.DARK),
        "ui_prefs": lambda: ui_prefs.update({"last_page": "library"}),
    },
    "newer": {
        "playlists": lambda: playlists.Store.open().set_rotation("evening", shuffle=True),
        "displays": lambda: displays.Store.open().set_beats_global_rules("DP-1", True),
        "schedules": lambda: schedules.Store.open().add(
            "evening", weekdays=("wed",), name="Work", rule_id="named"
        ),
        "ui_prefs": lambda: ui_prefs.update({"interface": "next"}),
    },
}


def operation(store, before, save):
    disarm()
    if before != "absent":
        SAVES["first"][store]()
    if before == "damaged":
        target = FILES[store]()
        data = target.read_bytes()
        target.write_bytes(data[: len(data) // 2])
    arm()
    try:
        SAVES[save][store]()
        print("DONE", flush=True)
    except Exception as error:
        print("REFUSED", type(error).__name__, error, flush=True)


serve(operation)
"""

#: A day and an hour: past every wait Tidy up has.
LATER_SECONDS = 25 * 3600
OTHER = MediaItem(Path("/media/dusk.png"), Kind.STILL, 5, 1)
FILES: dict[str, Callable[[], Path]] = {
    "playlists": playlists.state_path,
    "displays": displays.state_path,
    "schedules": schedules.state_path,
    "favourites": favourites.state_path,
    "pairings": pairings.state_path,
    "ui_prefs": paths.ui_prefs_path,
}
#: This version's saves, each adding to what the released code saved (other ids,
#: another wallpaper, another preference), so the file must still hold all of that.
SAVES: dict[str, dict[str, Callable[[], object]]] = {
    "open": {
        "playlists": lambda: playlists.Store.open().fault,
        "displays": lambda: displays.Store.open().fault,
        "schedules": lambda: schedules.Store.open().fault,
        "favourites": lambda: favourites.Store.open().fault,
        "pairings": lambda: pairings.Store.open().fault,
        "ui_prefs": lambda: ui_prefs.load().read_only,
    },
    "save": {
        "playlists": lambda: playlists.Store.open().create("Night", entry_id="night").name,
        "displays": lambda: displays.Store.open().assign("eDP-1", "evening"),
        "schedules": lambda: (
            schedules.Store.open().add("evening", weekdays=("thu",), rule_id="third").id
        ),
        "favourites": lambda: favourites.Store.open().add(Path("/media/night.png")),
        "pairings": lambda: (
            pairings.Store.open()
            .choose_palette(OTHER, pairings.PalettePolicy("builtin", "Nord"))
            .palette.name
        ),
        "ui_prefs": lambda: ui_prefs.update({"thumbnail_size": "small"}).read_only,
    },
    "newer format": {
        "playlists": lambda: playlists.Store.open().set_rotation("night", shuffle=True).name,
        "displays": lambda: displays.Store.open().set_beats_global_rules("eDP-1", True),
        "schedules": lambda: (
            schedules.Store.open().add("evening", weekdays=("fri",), name="Late", rule_id="late").id
        ),
        "ui_prefs": lambda: ui_prefs.update({"interface": "next"}).read_only,
    },
}


#: ui.toml holds every preference, so a save there changes one rather than
#: adding to the file: the one each step means to change.
MEANT: dict[tuple[str, str], str] = {
    ("ui_prefs", "save"): "thumbnail_size",
    ("ui_prefs", "newer format"): "interface",
}


def _parsed(name: str, data: bytes) -> dict[str, Any] | None:
    """The store's own file as a document, or None if it can't be read as one."""
    try:
        if name.endswith(".toml"):
            return tomllib.loads(data.decode("utf-8"))
        found = json.loads(data.decode("utf-8"))
    except ValueError, UnicodeDecodeError:
        return None
    return found if isinstance(found, dict) else None


def _missing(before: object, after: object, where: str = "") -> list[str]:
    """What ``before`` holds that ``after`` no longer does (``version`` aside)."""
    if isinstance(before, dict):
        if not isinstance(after, dict):
            return [where or "the document"]
        gone: list[str] = []
        for key, value in before.items():
            if key == "version" and not where:
                continue
            if key not in after:
                gone.append(f"{where}.{key}" if where else key)
            else:
                gone += _missing(value, after[key], f"{where}.{key}" if where else key)
        return gone
    if isinstance(before, list):
        if not isinstance(after, list):
            return [where]
        return [
            f"{where}[{index}]"
            for index, value in enumerate(before)
            if not any(not _missing(value, other, where) for other in after)
        ]
    return [] if before == after else [where]


def _reads_cleanly(store: str) -> bool:
    """Whether this version reads the store's own file without a fault, as it is now."""
    if store == "ui_prefs":
        return not ui_prefs.load().read_only
    return SAVES["open"][store]() is None


class _Consumer:
    """This version's steps on one store, and which of its files it read cleanly.

    A file the store reads cleanly is rewritten by a save, keeping what it
    held (checked here, step by step). One it can't read is never rewritten:
    its bytes must still be somewhere, like any other leftover's.
    """

    def __init__(self, store: str) -> None:
        self.store = store
        self.clean: set[bytes] = set()

    def superseded(self, relative: Path, data: bytes) -> bool:
        return relative.name == FILES[self.store]().name and data in self.clean

    def consume(self, base: Path) -> Iterator[tuple[str, str]]:
        store = self.store
        later = time.time() + LATER_SECONDS
        target = FILES[store]()

        def tidy_up() -> object:
            said = []
            for action in tidy.plan(roots=(), now=later).actions:
                if action.action in tidy.OFFERED and action.ready:
                    said.append(tidy.apply(action.action, action, roots=()).message)
            return " / ".join(said)

        steps: list[tuple[str, Callable[[], object]]] = [
            ("open", SAVES["open"][store]),
            ("save", SAVES["save"][store]),
        ]
        if store in SAVES["newer format"]:
            steps.append(("newer format", SAVES["newer format"][store]))
        steps += [("tidy up", tidy_up), ("open again", SAVES["open"][store])]
        for name, step in steps:
            held = None
            if target.is_file() and _reads_cleanly(store):
                data = target.read_bytes()
                self.clean.add(data)
                held = _parsed(target.name, data)
            try:
                said = f"{step()}"
            except Exception as error:
                said = f"REFUSED {type(error).__name__}: {error}"
            if held is not None:
                held.pop(MEANT.get((store, name), ""), None)
                now = _parsed(target.name, target.read_bytes()) if target.is_file() else None
                gone = ["everything: it no longer reads"] if now is None else _missing(held, now)
                if gone:
                    said = f"FINDING {target.name} lost {', '.join(gone[:6])} ({said})"
            yield name, said


def _prepare(base: Path) -> faults.Tree:
    for value in faults.profile_environment(base).values():
        Path(value).mkdir(parents=True, exist_ok=True)
    return faults.tree(base)


EVERY_CASE = [
    (store, before, save)
    for store in FILES
    for before, save in (("absent", "first"), ("valid", "second"), ("damaged", "second"))
] + [(store, "valid", "newer") for store in ("playlists", "displays", "schedules", "ui_prefs")]
#: What a gate runs: one JSON store over a damaged file (``.broken``, and the
#: publication that replaces nothing) and into a newer format (``.vN-backup``);
#: and the one store with a writer of its own. The JSON stores share theirs
#: (`state_file`).
USUAL_CASES = [
    ("playlists", "damaged", "second"),
    ("playlists", "valid", "newer"),
    ("ui_prefs", "valid", "newer"),
]


def _campaign(
    case: tuple[str, str, str],
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> None:
    store, before, save = case
    # Hundreds of profiles: durability isn't what this probes.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    later = time.time() + LATER_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )

    consumer = _Consumer(store)
    found = faults.campaign(
        monkeypatch,
        released_source,
        PRODUCER,
        case,
        tmp_path,
        prepare=_prepare,
        consume=consumer.consume,
        superseded=consumer.superseded,
        full=full,
    )

    print("\n" + found.summary(f"{store}, {before} then {save}"))
    assert not found.findings, "\n".join(sorted(set(found.findings))[:40])


@pytest.mark.parametrize("case", USUAL_CASES, ids="-".join)
def test_nothing_a_released_store_left_is_lost(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, str],
) -> None:
    """What a gate runs, under the bounded plans."""
    _campaign(case, released_source, tmp_path, monkeypatch, full=False)


@faults.full_campaign
@pytest.mark.parametrize("case", EVERY_CASE, ids="-".join)
def test_nothing_a_released_store_left_is_lost_under_any_plan(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, str],
) -> None:
    """Every store, every kind of save, every plan."""
    _campaign(case, released_source, tmp_path, monkeypatch, full=True)
