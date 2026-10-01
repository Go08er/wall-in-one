"""A newer build's files survive this one: one store at a time, then settings.

0.2.0's forward-compatibility guard, which 0.1.4 lacks: a store file whose
version is newer than this build knows (0.2.0 opening a 0.3.0 file) is
refused with ``kind == "newer-version"``, never rewritten or moved aside;
unknown keys, top-level and per record, are carried through every save. A
store this build once bumped keeps its one-time backup untouched when a newer
release bumps it again, and while one is newer, neither ``runtime.toml`` nor
``runtime-overrides.toml`` is republished. ``settings.toml`` is frozen and
fails safe: with a key this build does not know, every write is refused and
the headless path compiles the known keys. Each case is its own test, so a
regression names the store and the aspect.
"""

from __future__ import annotations

import re
import tomllib
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
from wall_in_one import cli, config, runtime_config
from wall_in_one.library import (
    displays,
    favourites,
    pairings,
    playlists,
    removals,
    schedules,
    state_file,
)
from wall_in_one.library.model import Kind, MediaItem


@dataclass(frozen=True, slots=True)
class StoreCase:
    """One authoring store: its file, its records, and a typical edit."""

    name: str
    filename: str
    #: The newest version of the file this build understands; one above it
    #: is what a newer build writes.
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
    StoreCase(
        "playlists", "playlists.json", playlists.FORMAT_VERSION, "playlists", _edit_playlists, 0, 1
    ),
    StoreCase(
        "schedules", "schedules.json", schedules.FORMAT_VERSION, "rules", _edit_schedules, 0, 1
    ),
    StoreCase(
        "pairings", "pairings.json", pairings.FORMAT_VERSION, "pairings", _edit_pairings, 0, 1
    ),
    StoreCase("displays", "displays.json", displays.FORMAT_VERSION, "displays", _edit_displays),
    StoreCase(
        "favourites", "favourites.json", favourites.FORMAT_VERSION, "paths", _edit_favourites
    ),
    StoreCase(
        "pending-removals",
        "pending-removals.json",
        removals.FORMAT_VERSION,
        "removals",
        _edit_removals,
    ),
)
RECORD_STORES: Final = tuple(case for case in STORES if case.untouched_record is not None)


def _cases(cases: Iterable[StoreCase]) -> list[Any]:
    return [pytest.param(case, id=case.name) for case in cases]


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


@pytest.mark.parametrize("case", _cases(STORES))
def test_a_newer_store_file_is_never_rewritten(golden: Golden, case: StoreCase) -> None:
    """The typical edit is refused, saying why; the file keeps every byte."""
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

    refusal = _attempt(case.edit, profile)

    assert target.read_bytes() == original, "a newer-version file was rewritten"
    assert broken_copies(target) == [], "a newer-version file was moved aside"
    assert refusal is not None, "the edit of a newer-version file was not refused"
    assert getattr(refusal, "kind", None) == state_file.NEWER_VERSION, repr(refusal)
    assert state_file.newer_version_refusal(target) in str(refusal)


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


# -- a newer release's bump of a file this build bumped -----------------------------------


@dataclass(frozen=True, slots=True)
class BumpCase:
    """A store this build moves to a new version the first time a new field is used."""

    store: StoreCase
    #: The one-time backup of the version before this build's.
    backup: str
    #: Use the new field; ``turn`` picks a different value each time.
    use: Callable[[Profile, int], object]


def _name_a_rule(profile: Profile, turn: int) -> object:
    rule = read_json(profile.app_state / "schedules.json")["rules"][0]
    return schedules.Store.open().set_name(rule["id"], f"Frog day {turn}")


def _give_a_playlist_its_interval(profile: Profile, turn: int) -> object:
    return playlists.Store.open().set_rotation(playlist_ids(profile)[0], cycle_interval=60 * turn)


def _opt_a_display_in(profile: Profile, turn: int) -> object:
    return displays.Store.open().set_beats_global_rules(("DP-1", "HDMI-A-1")[turn - 1], True)


_BY_NAME: Final = {case.name: case for case in STORES}
BUMPS: Final = (
    BumpCase(_BY_NAME["schedules"], "schedules.json.v2-backup", _name_a_rule),
    BumpCase(_BY_NAME["playlists"], "playlists.json.v1-backup", _give_a_playlist_its_interval),
    BumpCase(_BY_NAME["displays"], "displays.json.v1-backup", _opt_a_display_in),
)


def _backups(profile: Profile) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in profile.app_state.glob("*-backup")}


def _newer_than_this_build(target: Path, version: int) -> bytes:
    """What a later release (0.3.0, say) writes: one version up, with a key of its own."""
    document = read_json(target)
    document["version"] = version + 1
    document[UNKNOWN_TOP] = {"written-by": "a newer build"}
    return write_json(target, document)


@pytest.mark.parametrize("case", [pytest.param(case, id=case.store.name) for case in BUMPS])
def test_a_newer_bump_of_a_file_this_build_bumped_keeps_the_file_and_its_backup(
    golden: Golden, case: BumpCase
) -> None:
    """This build bumped the file once; a newer release bumped it again.

    Both the typical edit and another use of the new field are refused,
    saying why. The file, its one-time backup and the set of backups keep
    every byte, and nothing is moved aside.
    """
    profile = golden.profile
    target = profile.app_state / case.store.filename
    case.use(profile, 1)
    assert read_json(target)["version"] == case.store.version
    backups = _backups(profile)
    assert case.backup in backups
    original = _newer_than_this_build(target, case.store.version)

    refusals = [_attempt(case.store.edit, profile), _attempt(lambda p: case.use(p, 2), profile)]

    for refusal in refusals:
        assert getattr(refusal, "kind", None) == state_file.NEWER_VERSION, repr(refusal)
    assert target.read_bytes() == original
    assert _backups(profile) == backups
    assert broken_copies(target) == []


@pytest.mark.parametrize("case", _cases(_BY_NAME[name] for name in ("playlists", "displays")))
def test_a_newer_store_leaves_runtime_toml_and_the_overrides_file_alone(
    golden: Golden, case: StoreCase, capsys: pytest.CaptureFixture[str]
) -> None:
    """With per-playlist rotation and the display opt-in in use, one store turns newer.

    This build's compile refuses it and publishes neither runtime file:
    ``runtime.toml`` and ``runtime-overrides.toml`` keep every byte (a
    refused publication must not drop the overrides as if none were in
    use), the service unit still starts on them, and the store, its backup
    and the other store are left as they were.
    """
    profile = golden.profile
    state = profile.app_state
    config.update({"display_mode": "independent", "theme_source_connector": "DP-1"})
    playable = next(
        playlist["id"]
        for playlist in read_json(state / "playlists.json")["playlists"]
        if playlist["entries"]
    )
    playlists.Store.open().set_rotation(playable, cycle_interval=120, shuffle=True)
    displays.Store.open().set_beats_global_rules("DP-1", True)
    assert cli.main(["--write-config"]) == 0
    runtime = state / "runtime.toml"
    sidecar = state / runtime_config.OVERRIDES_FILENAME
    published = (runtime.read_bytes(), sidecar.read_bytes())
    overrides = tomllib.loads(sidecar.read_text())
    assert overrides["playlists"] and overrides["displays"], "both features reach the file"
    backups = _backups(profile)
    target = state / case.filename
    original = _newer_than_this_build(target, case.version)
    stores = {name: (state / name).read_bytes() for name in ("playlists.json", "displays.json")}
    capsys.readouterr()

    compiled = cli.main(["--write-config"])
    refused = capsys.readouterr().err
    prepared = cli.main(["--service-startup-prepare"])
    edited = _attempt(case.edit, profile)

    assert compiled != 0
    assert "newer version" in refused, refused
    assert prepared == 0, "the service must still start on the last-known-good config"
    assert (runtime.read_bytes(), sidecar.read_bytes()) == published
    assert getattr(edited, "kind", None) == state_file.NEWER_VERSION, repr(edited)
    assert target.read_bytes() == original
    assert {name: (state / name).read_bytes() for name in stores} == stores
    assert _backups(profile) == backups
    assert broken_copies(target) == []


# -- settings.toml fails safe -------------------------------------------------------------

SETTINGS_ATTEMPTS: Final[dict[str, Callable[[], object]]] = {
    "update": lambda: config.update({"opacity": 0.6}),
    "mutate": lambda: config.mutate(lambda current: current),
    "forget-default": lambda: config.forget_playlist_default(config.load().active_playlist),
}


@pytest.mark.parametrize("attempt", SETTINGS_ATTEMPTS, ids=list(SETTINGS_ATTEMPTS))
def test_a_settings_change_never_rewrites_unknown_keys(golden: Golden, attempt: str) -> None:
    """Every writer refuses, read-only, and the file keeps every byte."""
    target = golden.profile.app_config / "settings.toml"
    original = target.read_bytes() + b"ui_glass_frost = 0.3\n"
    target.write_bytes(original)

    refusal = _attempt(lambda _profile: SETTINGS_ATTEMPTS[attempt](), golden.profile)

    assert target.read_bytes() == original
    assert list(target.parent.glob("settings.toml.broken*")) == []
    assert isinstance(refusal, config.SettingsReadOnlyError), repr(refusal)
    assert refusal.unknown_keys == ("ui_glass_frost",)


def test_headless_publication_runs_on_the_settings_it_knows(golden: Golden) -> None:
    """A newer settings key must not stop the service from compiling the rest."""
    target = golden.profile.app_config / "settings.toml"
    text = re.sub(r"(?m)^cycle_interval = \d+$", "cycle_interval = 600", target.read_text())
    original = (text + "ui_glass_frost = 0.3\n").encode()
    target.write_bytes(original)

    assert cli.main(["--write-config"]) == 0
    assert target.read_bytes() == original
    assert runtime_document(golden.profile)["settings"]["cycle_interval_seconds"] == 600
