"""The startup repair of dangling playlist references leaves the built-in All media alone.

The generated ``all-media`` playlist is in no ``playlists.json``, yet a rule, a
display assignment or a saved default may name it, by id or by its exact name:
the new window offers it as "All wallpapers", and the compiler accepts both
spellings. The repair judged it dangling and deleted the rule or assignment
(and the display's ``beats_global_rules`` opt-in with it). Those that say so
fail on 39a7356.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, playlists, schedules
from wall_in_one.ui.app import Application, _playlist_namer

pytestmark = pytest.mark.gui

BUILT_IN = ("all-media", "All media")


@pytest.fixture(autouse=True)
def _profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config.save(config.Settings(display_mode=config.DISPLAY_MODE_INDEPENDENT))


def _rules() -> list[str]:
    return [rule.playlist for rule in schedules.Store.open().rules]


def _assigned() -> list[str]:
    return [playlist for _connector, playlist in displays.Store.open().all()]


def test_the_compilers_spellings_are_the_ones_recognised() -> None:
    assert BUILT_IN == (runtime_config.FALLBACK_PLAYLIST_ID, runtime_config.FALLBACK_PLAYLIST_NAME)
    assert all(runtime_config.is_fallback_reference(spelling) for spelling in BUILT_IN)
    assert not runtime_config.is_fallback_reference("all media")  # the compiler is exact
    assert not runtime_config.is_fallback_reference("quick-choice")


def _stored(reference: str) -> str:
    """What the repair leaves: the id, whichever spelling was saved (the window keys by id)."""
    return runtime_config.FALLBACK_PLAYLIST_ID


@pytest.mark.parametrize("reference", BUILT_IN)
def test_a_schedule_rule_naming_the_built_in_survives(reference: str) -> None:
    playlists.Store.open().create("Evening")
    schedules.Store.open().add(reference, rule_id="saved-rule", start="08:00", end="09:00")

    result = Application._repair_dangling_playlist_references()

    assert result.failures == ()
    assert _rules() == [_stored(reference)]


@pytest.mark.parametrize("reference", BUILT_IN)
def test_a_display_assignment_naming_the_built_in_survives_with_its_priority(
    reference: str,
) -> None:
    playlists.Store.open().create("Evening")
    store = displays.Store.open()
    store.assign("DP-1", reference)
    assert store.set_beats_global_rules("DP-1", True)

    result = Application._repair_dangling_playlist_references()

    assert result.failures == ()
    kept = displays.Store.open()
    assert _assigned() == [_stored(reference)]
    assert kept.beats_global_rules("DP-1")


@pytest.mark.parametrize("reference", BUILT_IN)
def test_a_saved_default_naming_the_built_in_survives(reference: str) -> None:
    playlists.Store.open().create("Evening")
    config.save(config.Settings(active_playlist=reference))

    result = Application._repair_dangling_playlist_references()

    assert result.failures == ()
    assert config.load_strict().active_playlist == _stored(reference)


def test_a_default_set_to_the_built_in_during_the_repair_is_not_a_change_to_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another program saves ``all-media`` just as the repair clears a gone default."""
    playlists.Store.open().create("Evening")
    config.save(config.Settings(active_playlist="gone"))

    def raced(*_args: object, **_kwargs: object) -> None:
        config.save(config.Settings(active_playlist="all-media"))

    monkeypatch.setattr(config, "forget_playlist_default", raced)

    result = Application._repair_dangling_playlist_references()

    assert result.failures == ()
    assert config.load_strict().active_playlist == "all-media"


def test_deleting_or_renaming_a_playlist_never_claims_the_built_in_references() -> None:
    """A hand-edited authored playlist named "All media" loses to the generated one."""
    mine = playlists.Store.open().create("Evening")
    document = json.loads(playlists.state_path().read_text(encoding="utf-8"))
    (record,) = (
        item
        for value in document.values()
        if isinstance(value, list)
        for item in value
        if isinstance(item, dict) and item.get("id") == mine.id
    )
    record["name"] = "All media"
    playlists.state_path().write_text(json.dumps(document), encoding="utf-8")
    listing = playlists.Store.open()
    assert listing.get(mine.id) is not None, "the hand-edited file was read"

    names = _playlist_namer(listing, mine.id)

    assert names(mine.id)
    assert not names("All media")
    assert not names("all-media")


def test_a_reference_to_the_built_in_survives_with_no_authored_playlist() -> None:
    schedules.Store.open().add("all-media", rule_id="saved-rule", start="08:00", end="09:00")
    displays.Store.open().assign("DP-1", "All media")

    assert Application._repair_dangling_playlist_references().failures == ()

    assert _rules() == ["all-media"]
    assert _assigned() == ["all-media"]


# -- controls: what the repair still does --------------------------------------------------------


def test_an_authored_name_is_pointed_at_its_id() -> None:
    evening = playlists.Store.open().create("Evening")
    schedules.Store.open().add("evening", rule_id="saved-rule", start="08:00", end="09:00")
    displays.Store.open().assign("DP-1", "EVENING")

    result = Application._repair_dangling_playlist_references()

    assert result.failures == ()
    assert _rules() == [evening.id]
    assert _assigned() == [evening.id]


def test_a_reference_to_no_playlist_is_still_removed_with_its_priority() -> None:
    playlists.Store.open().create("Evening")
    schedules.Store.open().add("gone", rule_id="saved-rule", start="08:00", end="09:00")
    store = displays.Store.open()
    store.assign("DP-1", "gone")
    store.set_beats_global_rules("DP-1", True)
    config.save(config.Settings(active_playlist="gone"))

    result = Application._repair_dangling_playlist_references()

    assert result.failures == ()
    assert _rules() == []
    assert _assigned() == []
    assert not displays.Store.open().beats_global_rules("DP-1")
    assert config.load_strict().active_playlist == ""


def test_unknown_ids_and_quick_choice_records_get_no_exemption() -> None:
    playlists.Store.open().create("Evening")
    schedules.Store.open().add("quick-choice", rule_id="r1", start="08:00", end="09:00")
    displays.Store.open().assign("DP-1", "quick-choice:DP-1")
    displays.Store.open().assign("DP-2", "all-media-2")

    assert Application._repair_dangling_playlist_references().failures == ()

    assert _rules() == []
    assert _assigned() == []


def test_a_newer_schedule_store_is_left_untouched() -> None:
    playlists.Store.open().create("Evening")
    schedules.Store.open().add("all-media", rule_id="saved-rule", start="08:00", end="09:00")
    target = schedules.state_path()
    newer = {"version": schedules.FORMAT_VERSION + 1, "rules": [{"playlist": "gone"}]}
    target.write_text(json.dumps(newer), encoding="utf-8")
    before = target.read_bytes()

    result = Application._repair_dangling_playlist_references()

    assert result.failures == ()
    assert target.read_bytes() == before


def test_a_newer_display_store_is_left_untouched() -> None:
    playlists.Store.open().create("Evening")
    displays.Store.open().assign("DP-1", "all-media")
    target = displays.state_path()
    target.write_text(json.dumps({"version": 99, "displays": {"DP-1": "gone"}}), encoding="utf-8")
    before = target.read_bytes()

    result = Application._repair_dangling_playlist_references()

    assert result.failures == ()
    assert target.read_bytes() == before


def test_a_newer_playlists_store_skips_the_whole_repair() -> None:
    schedules.Store.open().add("gone", rule_id="saved-rule", start="08:00", end="09:00")
    displays.Store.open().assign("DP-1", "gone")
    newer = {"version": playlists.FORMAT_VERSION + 1, "lists": []}
    playlists.state_path().write_text(json.dumps(newer), encoding="utf-8")

    result = Application._repair_dangling_playlist_references()

    assert result.failures == ()
    assert _rules() == ["gone"]
    assert _assigned() == ["gone"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_an_unreadable_playlists_store_removes_nothing() -> None:
    schedules.Store.open().add("gone", rule_id="saved-rule", start="08:00", end="09:00")
    displays.Store.open().assign("DP-1", "gone")
    playlists.Store.open().create("Evening")
    target = playlists.state_path()
    target.chmod(0o000)
    try:
        result = Application._repair_dangling_playlist_references()
    finally:
        target.chmod(0o600)

    (failure,) = result.failures
    assert failure.startswith("playlists: ")
    assert _rules() == ["gone"]
    assert _assigned() == ["gone"]
