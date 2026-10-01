"""A newer build's files survive this one: one store at a time, then settings.

Release 1's guard: a store file whose version is newer than this build knows
is refused, never rewritten or moved aside; unknown keys, top-level and per
record, are carried through every save. ``settings.toml`` is frozen and fails
safe. Each case is its own test so a merge shows exactly which flipped.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from tests.golden.harness import Profile
from tests.golden.sandbox import (
    UNKNOWN_RECORD,
    UNKNOWN_TOP,
    Golden,
    broken_copies,
    pairing_item,
    playlist_ids,
    read_json,
    runtime_document,
    write_json,
)
from wall_in_one import cli, config
from wall_in_one.library import displays, favourites, pairings, playlists, removals, schedules
from wall_in_one.library.model import Kind, MediaItem

GUARD: Final = "needs r1-store-guard"
UI_PREFS: Final = "needs r1-ui-prefs"
#: Strict, and only for a failed assertion: an error in the harness itself
#: must fail loudly instead of hiding behind an expected failure.
XFAIL_GUARD: Final = pytest.mark.xfail(strict=True, raises=AssertionError, reason=GUARD)


@dataclass(frozen=True, slots=True)
class StoreCase:
    """One authoring store: its file, its records, and a typical edit."""

    name: str
    filename: str
    version: int
    collection: str
    #: The edit a user makes most often, through the Store API.
    edit: Callable[[Profile], object]
    #: Index of the record :attr:`edit` modifies in place, if it does.
    edited_record: int | None = None
    #: Index of a record :attr:`edit` leaves alone, if records are objects.
    untouched_record: int | None = None


def _edit_playlists(profile: Profile) -> object:
    return playlists.Store.open().rename(playlist_ids(profile)[0], "Renamed by an edit")


def _edit_schedules(profile: Profile) -> object:
    rule = read_json(profile.app_state / "schedules.json")["rules"][0]
    return schedules.Store.open().set_enabled(rule["id"], True)


def _edit_pairings(profile: Profile) -> object:
    return pairings.Store.open().choose_palette(
        pairing_item(profile), pairings.PalettePolicy(kind="builtin", name="Nord")
    )


def _edit_displays(profile: Profile) -> object:
    displays.Store.open().assign("DP-9", playlist_ids(profile)[0])
    return None


def _edit_favourites(profile: Profile) -> object:
    candidates = sorted(profile.home.glob("Pictures/Wallpapers/*.png"))
    return favourites.Store.open().add(candidates[0])


def _record_scene_removal(profile: Profile, scene: str) -> object:
    """Journal a confirmed external uninstall of one Workshop scene."""
    folder = profile.data_home / "Steam/steamapps/workshop/content/431960" / scene
    item = MediaItem(path=folder, kind=Kind.SCENE, size=0, mtime=0, scene=scene)
    store = removals.Store.open()
    try:
        return store.record_external(item, (profile.home / "Pictures/Wallpapers",))
    finally:
        store.close()


def _edit_removals(profile: Profile) -> object:
    return _record_scene_removal(profile, "2910000004")


STORES: Final = (
    StoreCase("playlists", "playlists.json", 1, "playlists", _edit_playlists, 0, 1),
    StoreCase("schedules", "schedules.json", 2, "rules", _edit_schedules, 0, 1),
    StoreCase("pairings", "pairings.json", 2, "pairings", _edit_pairings, 0, 1),
    StoreCase("displays", "displays.json", 1, "displays", _edit_displays),
    StoreCase("favourites", "favourites.json", 1, "paths", _edit_favourites),
    StoreCase("pending-removals", "pending-removals.json", 1, "removals", _edit_removals),
)
RECORD_STORES: Final = tuple(case for case in STORES if case.untouched_record is not None)


def _cases(cases: Iterable[StoreCase], *, holds_today: Iterable[str] = ()) -> list[Any]:
    """One param per store; strict xfail unless today's code already holds."""
    exempt = set(holds_today)
    return [
        pytest.param(case, id=case.name, marks=() if case.name in exempt else XFAIL_GUARD)
        for case in cases
    ]


def _attempt(edit: Callable[[Profile], object], profile: Profile) -> BaseException | None:
    """Run one edit; a refusal is fine, a programming error is not."""
    try:
        edit(profile)
    except TypeError, AttributeError, NameError, KeyError, IndexError:
        raise
    except Exception as error:
        return error
    return None


def _ensure_one_removal(profile: Profile) -> None:
    """The fixture journal is empty, like the real one; give the record cases one."""
    _record_scene_removal(profile, "2910000099")


# pending-removals already refuses to touch a journal it cannot read.
@pytest.mark.parametrize("case", _cases(STORES, holds_today={"pending-removals"}))
def test_a_newer_store_file_is_never_rewritten(golden: Golden, case: StoreCase) -> None:
    profile = golden.profile
    target = profile.app_state / case.filename
    document = read_json(target)
    document["version"] = case.version + 1
    document[UNKNOWN_TOP] = {"written-by": "a newer build"}
    records = document[case.collection]
    if isinstance(records, list):
        for record in records:
            if isinstance(record, dict):
                record[UNKNOWN_RECORD] = "kept by the newer build"
    original = write_json(target, document)

    _attempt(case.edit, profile)

    assert target.read_bytes() == original, "a newer-version file was rewritten"
    assert broken_copies(target) == [], "a newer-version file was moved aside"


def _unknown_keys_case(profile: Profile, case: StoreCase) -> tuple[Path, dict[str, Any]]:
    if case.name == "pending-removals":
        _ensure_one_removal(profile)
    target = profile.app_state / case.filename
    return target, read_json(target)


@pytest.mark.parametrize("case", _cases(STORES))
def test_unknown_top_level_keys_survive_an_edit(golden: Golden, case: StoreCase) -> None:
    target, document = _unknown_keys_case(golden.profile, case)
    document[UNKNOWN_TOP] = {"written-by": "a newer build", "items": [1, 2]}
    write_json(target, document)

    assert _attempt(case.edit, golden.profile) is None
    assert read_json(target).get(UNKNOWN_TOP) == {"written-by": "a newer build", "items": [1, 2]}
    assert broken_copies(target) == []


@pytest.mark.parametrize("case", _cases(RECORD_STORES))
def test_unknown_keys_on_an_untouched_record_survive_an_edit(
    golden: Golden, case: StoreCase
) -> None:
    target, document = _unknown_keys_case(golden.profile, case)
    assert case.untouched_record is not None
    record = document[case.collection][case.untouched_record]
    record[UNKNOWN_RECORD] = "a description from a newer build"
    key = "id" if "id" in record else "identity"
    write_json(target, document)

    assert _attempt(case.edit, golden.profile) is None
    (survivor,) = [
        entry for entry in read_json(target)[case.collection] if entry[key] == record[key]
    ]
    assert survivor.get(UNKNOWN_RECORD) == "a description from a newer build"
    assert broken_copies(target) == []


@pytest.mark.parametrize(
    "case", _cases(case for case in RECORD_STORES if case.edited_record is not None)
)
def test_unknown_keys_on_the_edited_record_survive_its_edit(
    golden: Golden, case: StoreCase
) -> None:
    """A rule's ``name`` or a playlist's ``description`` must outlive editing it."""
    target, document = _unknown_keys_case(golden.profile, case)
    assert case.edited_record is not None
    record = document[case.collection][case.edited_record]
    record[UNKNOWN_RECORD] = "a description from a newer build"
    key = "id" if "id" in record else "identity"
    write_json(target, document)

    assert _attempt(case.edit, golden.profile) is None
    (edited,) = [entry for entry in read_json(target)[case.collection] if entry[key] == record[key]]
    assert edited.get(UNKNOWN_RECORD) == "a description from a newer build"


@XFAIL_GUARD
def test_unknown_keys_on_playlist_entries_survive_an_edit(golden: Golden) -> None:
    target = golden.profile.app_state / "playlists.json"
    document = read_json(target)
    entry = document["playlists"][1]["entries"][0]
    entry[UNKNOWN_RECORD] = "a note from a newer build"
    write_json(target, document)

    assert _attempt(_edit_playlists, golden.profile) is None
    (kept,) = [
        candidate
        for playlist in read_json(target)["playlists"]
        for candidate in playlist["entries"]
        if candidate["id"] == entry["id"]
    ]
    assert kept.get(UNKNOWN_RECORD) == "a note from a newer build"


# -- settings.toml fails safe -------------------------------------------------------------

SETTINGS_ATTEMPTS: Final[dict[str, Callable[[], object]]] = {
    "update": lambda: config.update({"opacity": 0.6}),
    "mutate": lambda: config.mutate(lambda current: current),
    "forget-default": lambda: config.forget_playlist_default(config.load().active_playlist),
}


@pytest.mark.parametrize("attempt", SETTINGS_ATTEMPTS, ids=list(SETTINGS_ATTEMPTS))
def test_a_settings_change_never_rewrites_unknown_keys(golden: Golden, attempt: str) -> None:
    """Today's strict loader refuses the edit. Whatever replaces it must not narrow."""
    target = golden.profile.app_config / "settings.toml"
    original = target.read_bytes() + b"ui_glass_frost = 0.3\n"
    target.write_bytes(original)

    _attempt(lambda _profile: SETTINGS_ATTEMPTS[attempt](), golden.profile)

    assert target.read_bytes() == original
    assert list(target.parent.glob("settings.toml.broken*")) == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=UI_PREFS)
def test_headless_publication_runs_on_the_settings_it_knows(golden: Golden) -> None:
    """A newer settings key must not stop the service from compiling the rest."""
    target = golden.profile.app_config / "settings.toml"
    text = re.sub(r"(?m)^cycle_interval = \d+$", "cycle_interval = 600", target.read_text())
    original = (text + "ui_glass_frost = 0.3\n").encode()
    target.write_bytes(original)

    assert cli.main(["--write-config"]) == 0
    assert target.read_bytes() == original
    assert runtime_document(golden.profile)["settings"]["cycle_interval_seconds"] == 600
