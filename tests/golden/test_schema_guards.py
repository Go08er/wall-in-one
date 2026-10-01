"""Release 2's format-change guards on a whole profile, starting with rule names.

Guard 1, lazy bump on use: an edit that uses no new field writes the version
the file already has, byte for byte what 0.1.4 and 0.1.5 write, so most
profiles never leave the formats older builds read. Guard 2, one-time backup:
the first save that does use one keeps the old bytes beside the file as
``<file>.v<old>-backup``, once, and without it the save is refused.

Every check diffs the whole sandbox home, so a stray backup, temporary or
``.broken`` copy fails as an unexpected write. Idle never bumps or backs up
anything, and names never reach ``runtime.toml``. What older builds do with a
version-3 file (0.1.5 refuses it, 0.1.4 narrows it) is in ``test_downgrade``.
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
BACKUP: Final = f"{STATE}/schedules.json.v2-backup"


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


def test_an_unnamed_edit_keeps_version_two_byte_for_byte(golden: Golden) -> None:
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
        "version": 2,
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


def test_naming_a_rule_bumps_once_and_keeps_the_released_bytes(golden: Golden) -> None:
    profile = golden.profile
    target = profile.app_state / "schedules.json"
    released = target.read_bytes()
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

    def the_released_bytes(change: Change) -> None:
        assert change.after is not None
        assert change.after.content == released, "the backup is not the released file"
        assert change.after.mode == mode

    after = harness.snapshot(profile.home)
    harness.check_changes(
        harness.diff(before, after),
        [
            Allowance(
                SCHEDULES,
                frozenset({"modified"}),
                "the first rule name moves the file to version 3",
                named_one_rule,
            ),
            Allowance(
                BACKUP,
                frozenset({"created"}),
                "guard 2: the bytes before the bump",
                the_released_bytes,
            ),
        ],
    )
    assert BACKUP in after, "the bump made no backup"

    # More names, and clearing them all, never touch the backup again; the
    # file stays at version 3 (no flip-flop back to 2).
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
    assert (profile.app_state / "schedules.json.v2-backup").read_bytes() == released


def test_a_refused_backup_leaves_every_byte_of_the_profile(golden: Golden) -> None:
    profile = golden.profile
    (profile.app_state / "schedules.json.v2-backup").mkdir()
    before = harness.snapshot(profile.home)

    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.Store.open().set_name(_rules(profile)[0]["id"], "Frog day")

    assert caught.value.kind == "no-backup"
    harness.check_changes(harness.diff(before, harness.snapshot(profile.home)), ())


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
def test_idle_never_bumps_or_backs_up_a_schedule(golden: Golden, scenario: str) -> None:
    """Idle writes only the whitelist whatever the schedule's names and version.

    ``hand-named-version-two`` is a ``name`` typed into a version-2 file: this
    build reads it, and only an edit (never idle) moves the file to version 3.
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
    backups = sorted(path.name for path in profile.app_state.glob("*-backup"))
    expected = ["schedules.json.v2-backup"] if scenario == "named-version-three" else []
    assert backups == expected
