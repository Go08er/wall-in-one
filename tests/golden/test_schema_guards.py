"""Every save writes a store's current version, on a whole profile, starting with rule names.

A save of the released fixture's schedule, named or not, writes version 3: the
version-2 file is read, and the same rules are written under the new version
as an ordinary save. No copy of the old bytes is kept (updates are one-way),
and whatever an earlier version left under a copy's name neither stops the
save nor is touched by it.

Every check diffs the whole sandbox home, so a stray copy, temporary or
``.broken`` file fails as an unexpected write. Idle never rewrites a store,
and names never reach ``runtime.toml``.
"""

from __future__ import annotations

import json
import stat
from typing import Any, Final

import pytest

from tests.golden import harness
from tests.golden.harness import Allowance, Change, Profile
from tests.golden.sandbox import STATE, Golden, read_json
from tests.golden.test_idle import first_start_writes
from wall_in_one import cli
from wall_in_one.library import schedules

SCHEDULES: Final = f"{STATE}/schedules.json"


def _serialized(document: dict[str, Any]) -> bytes:
    """How every schedules version so far has been written."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()


def _rules(profile: Profile) -> list[dict[str, Any]]:
    rules = read_json(profile.app_state / "schedules.json")["rules"]
    assert isinstance(rules, list)
    return rules


def _only(change: Change) -> tuple[dict[str, Any], dict[str, Any]]:
    assert change.before is not None and change.before.content is not None
    assert change.after is not None and change.after.content is not None
    return json.loads(change.before.content), json.loads(change.after.content)


def test_the_fixture_schedule_is_an_unnamed_version_two_file_as_released(golden: Golden) -> None:
    target = golden.profile.app_state / "schedules.json"
    document = read_json(target)
    assert document["version"] == 2
    assert target.read_bytes() == _serialized(document)
    assert not any("name" in rule for rule in document["rules"])


def test_an_unnamed_edit_writes_version_three_and_the_same_rules(golden: Golden) -> None:
    profile = golden.profile
    target = profile.app_state / "schedules.json"
    document = read_json(target)
    first, second, *rest = document["rules"]
    before = harness.snapshot(profile.home)

    store = schedules.Store.open()
    store.set_enabled(first["id"], True)
    store.update(second["id"], second["playlist"], start="20:30", end="04:00")
    store.add(second["playlist"], weekdays=["fri"], start="18:00", end="23:00", rule_id="friday")
    store.set_name(rest[0]["id"], "  ")

    expected = {
        "version": schedules.FORMAT_VERSION,
        "rules": [
            {key: value for key, value in first.items() if key != "enabled"},
            {**second, "start": "20:30", "end": "04:00"},
            *rest,
            {
                "id": "friday",
                "playlist": second["playlist"],
                "weekdays": ["fri"],
                "start": "18:00",
                "end": "23:00",
            },
        ],
    }
    assert target.read_bytes() == _serialized(expected)
    harness.check_changes(
        harness.diff(before, harness.snapshot(profile.home)),
        [Allowance(SCHEDULES, frozenset({"modified"}), "the edits themselves")],
    )


def test_naming_a_rule_keeps_no_copy(golden: Golden) -> None:
    profile = golden.profile
    target = profile.app_state / "schedules.json"
    mode = stat.S_IMODE(target.stat().st_mode)
    rules = _rules(profile)
    before = harness.snapshot(profile.home)

    schedules.Store.open().set_name(rules[2]["id"], "Late evenings")

    def named_one_rule(change: Change) -> None:
        old, new = _only(change)
        assert old["version"] == 2
        assert new == {
            "version": 3,
            "rules": [
                *old["rules"][:2],
                {**old["rules"][2], "name": "Late evenings"},
                *old["rules"][3:],
            ],
        }

    after = harness.snapshot(profile.home)
    # The one write: a copy of the old bytes beside it would be an unexpected one.
    harness.check_changes(
        harness.diff(before, after),
        [
            Allowance(
                SCHEDULES,
                frozenset({"modified"}),
                "the save writes the rule name at version 3",
                named_one_rule,
            ),
        ],
    )
    assert stat.S_IMODE(target.stat().st_mode) == mode

    # More names, and clearing them all: the file is version 3 throughout.
    store = schedules.Store.open()
    for index, rule in enumerate(rules):
        store.set_name(rule["id"], f"Rule {index}")
    for rule in rules:
        store.set_name(rule["id"], None)
    store.set_enabled(rules[0]["id"], True)
    harness.check_changes(
        harness.diff(after, harness.snapshot(profile.home)),
        [Allowance(SCHEDULES, frozenset({"modified"}), "later edits at version 3")],
    )
    cleared = read_json(target)
    assert cleared["version"] == 3
    assert not any("name" in rule for rule in cleared["rules"])


def test_something_at_an_old_copys_name_does_not_stop_the_save(golden: Golden) -> None:
    """Earlier versions refused the save when they could not keep their copy
    there. Nothing writes to that name now: the save goes through and whatever
    holds the name is not touched."""
    profile = golden.profile
    (profile.app_state / "schedules.json.v2-backup").mkdir()
    before = harness.snapshot(profile.home)

    schedules.Store.open().set_name(_rules(profile)[0]["id"], "Frog day")

    assert read_json(profile.app_state / "schedules.json")["version"] == 3
    harness.check_changes(
        harness.diff(before, harness.snapshot(profile.home)),
        [Allowance(SCHEDULES, frozenset({"modified"}), "the name, at version 3")],
    )


def test_names_never_reach_the_runtime(golden: Golden, capsys: pytest.CaptureFixture[str]) -> None:
    """Rust keys rules by id. A name changes runtime.toml by not one byte."""
    profile = golden.profile
    runtime = profile.app_state / "runtime.toml"
    assert cli.main(["--write-config"]) == 0
    compiled = runtime.read_bytes()
    capsys.readouterr()

    store = schedules.Store.open()
    for index, rule in enumerate(_rules(profile)):
        store.set_name(rule["id"], f"Frog rule {index}")
    assert cli.main(["--write-config"]) == 0

    assert "already current" in capsys.readouterr().out
    assert runtime.read_bytes() == compiled
    assert b"Frog rule" not in compiled


@pytest.mark.parametrize("scenario", ["named-version-three", "hand-named-version-two"])
def test_idle_never_rewrites_a_schedule(golden: Golden, scenario: str) -> None:
    """Idle writes only the whitelist whatever the schedule's names and version.

    ``hand-named-version-two`` is a ``name`` typed into a version-2 file: this
    build reads it, and only an edit (never idle) rewrites the file as version 3.
    """
    profile = golden.profile
    target = profile.app_state / "schedules.json"
    rules = _rules(profile)
    if scenario == "named-version-three":
        schedules.Store.open().set_name(rules[0]["id"], "Your pick, parked")
        assert read_json(target)["version"] == 3
    else:
        document = read_json(target)
        document["rules"][0]["name"] = "Your pick, parked"
        target.write_bytes(_serialized(document))
    assert schedules.Store.open().rules[0].name == "Your pick, parked"

    before = harness.snapshot(profile.home)
    first = harness.run_idle()
    after_first = harness.snapshot(profile.home)
    harness.check_changes(harness.diff(before, after_first), first_start_writes(profile, None))
    assert first.newer_version_files == ()
    assert first.gui_compile in ("changed", "unchanged"), first.gui_compile
    assert first.service_prepare == 0

    second = harness.run_idle()
    harness.check_changes(harness.diff(after_first, harness.snapshot(profile.home)), ())
    assert second == first
    assert golden.processes == []
    assert sorted(path.name for path in profile.app_state.glob("*-backup")) == []
