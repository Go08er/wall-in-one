"""``wall-in-one-rollback`` on the golden profile with every 0.2.0 field in use.

The tool narrows the current playlists.json, schedules.json and displays.json
to the versions 0.1.4 reads, keeping every record, removes
runtime-overrides.toml, and backs up what it changes first. That the real
v0.1.4 source then opens, edits and compiles the result is
``test_downgrade``'s (it needs the old build); this module needs none.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import select
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from tests.golden import harness, sandbox
from tests.golden.harness import Allowance, Change, Profile
from tests.golden.sandbox import Golden, decorate, read_json, write_json
from tests.golden.test_downgrade import V0_1_4_FORMATS
from wall_in_one import cli, config, legacy_migration, paths, rollback, runtime_config
from wall_in_one.library import displays, favourites, pairings, playlists, removals, schedules

RULE_NAME: Final = "Evening lights"
INTERVAL: Final = 120
OPT_IN: Final = "DP-1"
STATE: Final = sandbox.STATE
NARROWED: Final = ("playlists.json", "schedules.json", "displays.json")
OVERRIDES: Final = f"{STATE}/{runtime_config.OVERRIDES_FILENAME}"


def _use_every_new_field(profile: Profile) -> str:
    """Name a rule, give a playable playlist its own rotation, opt DP-1 in, compile.

    Returns that playlist's name. Independent routing, so the opt-in reaches
    the overrides file as well as its store.
    """
    state = profile.app_state
    config.update({"display_mode": "independent", "theme_source_connector": OPT_IN})
    playable = next(
        playlist
        for playlist in read_json(state / "playlists.json")["playlists"]
        if playlist["entries"]
    )
    rule = read_json(state / "schedules.json")["rules"][0]
    playlists.Store.open().set_rotation(playable["id"], cycle_interval=INTERVAL, shuffle=True)
    schedules.Store.open().set_name(rule["id"], RULE_NAME)
    assert displays.Store.open().set_beats_global_rules(OPT_IN, True)
    assert cli.main(["--write-config"]) == 0
    assert (state / runtime_config.OVERRIDES_FILENAME).is_file()
    for name, version in (("playlists.json", 2), ("schedules.json", 3), ("displays.json", 2)):
        assert read_json(state / name)["version"] == version, name
    return str(playable["name"])


@pytest.fixture
def in_use(golden: Golden) -> Iterator[tuple[Golden, str]]:
    yield golden, _use_every_new_field(golden.profile)


def _run(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, str, str]:
    capsys.readouterr()
    status = rollback.main(list(arguments))
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _records(document: dict[str, Any], key: str, dropped: frozenset[str]) -> list[Any]:
    """Every record of ``document[key]``, without the fields the rollback drops."""
    return [
        {name: value for name, value in record.items() if name not in dropped}
        for record in document[key]
    ]


# -- the plan ---------------------------------------------------------------------------


def test_the_dry_run_prints_the_plan_and_writes_nothing(
    in_use: tuple[Golden, str], capsys: pytest.CaptureFixture[str]
) -> None:
    golden, playlist = in_use
    before = harness.snapshot(golden.profile.home)

    status, out, err = _run(capsys)

    assert (status, err) == (0, "")
    lines = out.splitlines()
    for expected in (
        "Wall-in-One rollback to 0.1.4: a dry run; nothing is written.",
        "playlists.json: version 2 -> 1",
        f'  playlist "{playlist}" loses its interval ({INTERVAL} s)',
        f'  playlist "{playlist}" loses its own shuffle (on)',
        "schedules.json: version 3 -> 2",
        f'  rule "{RULE_NAME}" loses its name',
        "displays.json: version 2 -> 1",
        f"  display {OPT_IN} loses its own playlist beating global schedule rules",
        "Every playlist, entry, schedule rule and display assignment is kept.",
    ):
        assert expected in lines, (expected, out)
    assert any(line.startswith("runtime-overrides.toml: removed") for line in lines), out
    assert any("pairings.json, favourites.json, pending-removals.json" in line for line in lines)
    assert any("tidy-archive/" in line for line in lines), out
    assert harness.diff(before, harness.snapshot(golden.profile.home)) == []


def test_a_bumped_file_without_any_new_field_is_still_narrowed(
    golden: Golden, capsys: pytest.CaptureFixture[str]
) -> None:
    """0.1.4 refuses the version, not the content: a cleared field leaves version 2."""
    state = golden.profile.app_state
    first = read_json(state / "playlists.json")["playlists"][0]["id"]
    playlists.Store.open().set_rotation(first, shuffle=True)
    playlists.Store.open().set_rotation(first, shuffle=None)
    assert read_json(state / "playlists.json")["version"] == 2

    status, out, _err = _run(capsys)

    assert status == 0
    assert "playlists.json: version 2 -> 1; no field is lost" in out.splitlines(), out


def test_the_targets_are_the_versions_v0_1_4_reads() -> None:
    """Pinned to the downgrade suite's record of v0.1.4 (dbfbaa0), so they cannot drift.

    The files left alone are left alone because their formats did not change:
    this build writes pairings, favourites and pending removals at the
    versions v0.1.4 reads (settings.toml's keys are pinned by
    tests/test_settings_v0_1_4_keys.py).
    """
    assert {name: V0_1_4_FORMATS[name] for name in NARROWED} == rollback.V0_1_4_FORMATS
    assert V0_1_4_FORMATS["pairings.json"] == pairings.FORMAT_VERSION
    assert V0_1_4_FORMATS["favourites.json"] == favourites.FORMAT_VERSION
    assert removals.FORMAT_VERSION == 1, "v0.1.4's pending-removals.json version"


# -- apply -------------------------------------------------------------------------------


def _same_bytes(before: dict[str, harness.Node]) -> Callable[[Change], None]:
    def verify(change: Change) -> None:
        assert change.after is not None and change.after.content is not None
        name = Path(change.path).name
        original = before[f"{STATE}/{name}"]
        assert change.after.content == original.content, f"the backup of {name} differs"

    return verify


def _narrowed(before: dict[str, harness.Node]) -> Callable[[Change], None]:
    dropped = {
        "playlists.json": ("playlists", frozenset({"cycle_interval", "shuffle"})),
        "schedules.json": ("rules", frozenset({"name"})),
    }

    def verify(change: Change) -> None:
        assert change.after is not None and change.after.content is not None
        name = Path(change.path).name
        old_content = before[change.path].content
        assert old_content is not None
        old, new = json.loads(old_content), json.loads(change.after.content)
        assert new["version"] == rollback.V0_1_4_FORMATS[name], name
        if name == "displays.json":
            assert new == {"version": 1, "displays": old["displays"]}, new
            return
        key, fields = dropped[name]
        assert set(new) == {"version", key}, new
        assert _records(new, key, fields) == _records(old, key, fields), "a record changed"
        assert not any(field in record for record in new[key] for field in fields), name

    return verify


def test_apply_backs_up_then_narrows_keeping_every_record(
    in_use: tuple[Golden, str], capsys: pytest.CaptureFixture[str]
) -> None:
    golden, _playlist = in_use
    profile = golden.profile
    before = harness.snapshot(profile.home)

    status, out, err = _run(capsys, "--apply")

    assert (status, err) == (0, "")
    (backup,) = profile.app_state.glob(f"{rollback.BACKUP_PREFIX}*")
    assert f"The previous files are in {backup}. To put them back:" in out
    assert "  systemctl --user stop wall-in-one.service" in out.splitlines()
    assert f"  cp -p -- {backup}/* {profile.app_state}/" in out.splitlines()
    assert sorted(path.name for path in backup.iterdir()) == sorted(
        (*NARROWED, runtime_config.OVERRIDES_FILENAME)
    )
    folder = f"{STATE}/{backup.name}"
    harness.check_changes(
        harness.diff(before, harness.snapshot(profile.home)),
        [
            *(
                Allowance(
                    f"{STATE}/{name}",
                    frozenset({"modified"}),
                    "rewritten in 0.1.4's version with every record",
                    _narrowed(before),
                )
                for name in NARROWED
            ),
            Allowance(OVERRIDES, frozenset({"deleted"}), "0.1.4 never reads it"),
            Allowance(folder, frozenset({"created"}), "the one backup folder"),
            Allowance(
                f"{folder}/*",
                frozenset({"created"}),
                "each file as it was, before anything was written",
                _same_bytes(before),
            ),
        ],
    )

    # This build still reads the narrowed profile, and runtime.toml needed
    # nothing: compiled again it is the same document, without overrides.
    for store in (playlists, schedules, displays):
        assert store.Store.open().fault is None, store.__name__
    capsys.readouterr()
    assert cli.main(["--write-config"]) == 0
    assert "already current" in capsys.readouterr().out
    assert not (profile.app_state / runtime_config.OVERRIDES_FILENAME).exists()


def test_a_second_apply_writes_nothing(
    in_use: tuple[Golden, str], capsys: pytest.CaptureFixture[str]
) -> None:
    golden, _playlist = in_use
    assert _run(capsys, "--apply")[0] == 0
    after_first = harness.snapshot(golden.profile.home)

    status, out, err = _run(capsys, "--apply")

    assert (status, err) == (0, "")
    assert "Nothing to roll back" in out
    assert harness.diff(after_first, harness.snapshot(golden.profile.home)) == []
    assert len(list(golden.profile.app_state.glob(f"{rollback.BACKUP_PREFIX}*"))) == 1


# -- refusals ---------------------------------------------------------------------------


def _unknown_keys(profile: Profile) -> None:
    target = profile.app_state / "playlists.json"
    document = read_json(target)
    decorate(document)
    write_json(target, document)


def _newer_version(profile: Profile) -> None:
    target = profile.app_state / "schedules.json"
    document = read_json(target)
    document["version"] = schedules.FORMAT_VERSION + 1
    write_json(target, document)


def _unreadable(profile: Profile) -> None:
    (profile.app_state / "displays.json").write_text("{ not json", encoding="utf-8")


def _newer_overrides(profile: Profile) -> None:
    (profile.app_state / runtime_config.OVERRIDES_FILENAME).write_text(
        "schema_version = 2\n", encoding="utf-8"
    )


REFUSALS: Final[dict[str, tuple[Callable[[Profile], None], str]]] = {
    "unknown-keys": (_unknown_keys, "fields from a version newer than 0.2.0"),
    "newer-version": (_newer_version, "saved by a version newer than 0.2.0"),
    "unreadable": (_unreadable, "cannot be read"),
    "newer-overrides": (_newer_overrides, "written by a version newer than 0.2.0"),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_refuses_and_writes_nothing(
    in_use: tuple[Golden, str], capsys: pytest.CaptureFixture[str], case: str
) -> None:
    golden, _playlist = in_use
    damage, reason = REFUSALS[case]
    damage(golden.profile)
    before = harness.snapshot(golden.profile.home)

    for arguments in ((), ("--apply",)):
        status, out, err = _run(capsys, *arguments)
        assert status == rollback.EXIT_REFUSED, (arguments, out)
        assert "refused, nothing was written" in err and reason in err, err

    assert harness.diff(before, harness.snapshot(golden.profile.home)) == []


@contextlib.contextmanager
def _held_by_another_process(lock: Path) -> Iterator[None]:
    """A child process holds ``lock`` with flock, as a running app or service does."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    if not lock.exists():
        lock.write_bytes(b"")
    lock.chmod(0o600)
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child = sandbox.REAL_POPEN(
        (
            sys.executable,
            "-c",
            "import fcntl, os, sys; "
            "descriptor = os.open(sys.argv[1], os.O_RDWR); "
            "fcntl.flock(descriptor, fcntl.LOCK_EX); "
            "os.write(int(sys.argv[2]), b'x'); "
            "os.read(int(sys.argv[3]), 1)",
            str(lock),
            str(ready_write),
            str(release_read),
        ),
        close_fds=True,
        pass_fds=(ready_write, release_read),
    )
    os.close(ready_write)
    os.close(release_read)
    try:
        readable, _writable, _exceptional = select.select((ready_read,), (), (), 5)
        assert readable, "the child never took the lock"
        assert os.read(ready_read, 1) == b"x"
        yield
    finally:
        os.close(ready_read)
        with contextlib.suppress(BrokenPipeError):
            os.write(release_write, b"x")
        os.close(release_write)
        child.wait(timeout=5)


@pytest.mark.parametrize("held", ["runtime", "gui"])
def test_refuses_while_the_app_or_the_service_holds_its_lock(
    in_use: tuple[Golden, str], capsys: pytest.CaptureFixture[str], held: str
) -> None:
    golden, _playlist = in_use
    socket = paths.runtime_socket_path() if held == "runtime" else paths.socket_path()
    lock = socket.with_name(f"{socket.name}.lock")
    before = harness.snapshot(golden.profile.home)

    with _held_by_another_process(lock):
        status, _out, err = _run(capsys, "--apply")

    assert status == rollback.EXIT_REFUSED
    assert "Close Wall-in-One and stop its service first" in err, err
    assert harness.diff(before, harness.snapshot(golden.profile.home)) == []
    assert not list(golden.profile.app_state.glob(f"{rollback.BACKUP_PREFIX}*"))


# -- the profile lock --------------------------------------------------------------------


def _profile_lock() -> Path:
    """The lock companion-rework's keep-alive waits for (its lib/keepalive.luau lockPaths)."""
    marker = legacy_migration.marker_path()
    lock = marker.with_name(f".{marker.name}.mutation.lock")
    assert lock.name == ".legacy-migration-v1.json.mutation.lock"
    return lock


def _held_elsewhere(lock: Path) -> bool:
    """Whether another process holds ``lock``: a child's non-blocking try fails.

    The child never waits, and a lock it does get goes with it, so asking
    cannot disturb the holder.
    """
    child = sandbox.REAL_POPEN(
        (
            sys.executable,
            "-c",
            "import fcntl, os, sys\n"
            "descriptor = os.open(sys.argv[1], os.O_RDWR)\n"
            "try:\n"
            "    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except BlockingIOError:\n"
            "    sys.exit(3)\n",
            str(lock),
        ),
        close_fds=True,
    )
    status = child.wait(timeout=10)
    assert status in (0, 3), status
    return status == 3


def test_the_profile_lock_is_held_from_the_plan_to_the_last_write(
    in_use: tuple[Golden, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _profile_lock()
    held: dict[str, bool] = {}

    def watched(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*arguments: Any, **keywords: Any) -> Any:
            held[name] = _held_elsewhere(lock)
            return real(*arguments, **keywords)

        return call

    monkeypatch.setattr(rollback, "make_plan", watched("plan", rollback.make_plan))
    for module in (playlists, schedules, displays):
        name = module.__name__.rpartition(".")[2]
        monkeypatch.setattr(module, "save", watched(name, module.save))
    monkeypatch.setattr(
        runtime_config,
        "_publish_overrides",
        watched("overrides", runtime_config._publish_overrides),
    )
    assert not _held_elsewhere(lock)

    status, _out, err = _run(capsys, "--apply")

    assert (status, err) == (0, "")
    assert held == dict.fromkeys(("plan", "playlists", "schedules", "displays", "overrides"), True)
    assert not _held_elsewhere(lock), "released when the run ends"


def test_refuses_while_another_operation_holds_the_profile(
    in_use: tuple[Golden, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden, _playlist = in_use
    monkeypatch.setattr(rollback, "PROFILE_LOCK_TIMEOUT_SECONDS", 0.2)

    with _held_by_another_process(_profile_lock()):
        before = harness.snapshot(golden.profile.home)
        status, _out, err = _run(capsys, "--apply")
        after = harness.snapshot(golden.profile.home)

    assert status == rollback.EXIT_REFUSED
    assert "refused, nothing was written" in err, err
    assert "another Wall-in-One operation holds the profile" in err, err
    assert harness.diff(before, after) == []
    assert not list(golden.profile.app_state.glob(f"{rollback.BACKUP_PREFIX}*"))


# -- failures part way ---------------------------------------------------------------------


def test_a_write_that_fails_after_the_backup_says_what_was_rewritten_and_how_to_undo_it(
    in_use: tuple[Golden, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden, _playlist = in_use
    state = golden.profile.app_state
    touched = (*NARROWED, runtime_config.OVERRIDES_FILENAME)
    before = {name: (state / name).read_bytes() for name in touched}

    def full_disk(*_arguments: object, **_keywords: object) -> Path:
        raise schedules.ScheduleError("local-io", "injected: no space left on device")

    monkeypatch.setattr(schedules, "save", full_disk)

    status, out, err = _run(capsys, "--apply")

    assert status == rollback.EXIT_INCOMPLETE, (out, err)
    (backup,) = state.glob(f"{rollback.BACKUP_PREFIX}*")
    lines = err.splitlines()
    assert lines[0] == (
        "wall-in-one-rollback: stopped part way: local-io: injected: no space left on device"
    )
    assert "Already rewritten: playlists.json." in lines
    assert "Not rewritten: schedules.json, displays.json, runtime-overrides.toml." in lines
    assert f"Every one of them, as it was before this run, is in {backup}." in lines
    assert "  systemctl --user stop wall-in-one.service" in lines
    assert f"  cp -p -- {backup}/* {state}/" in lines
    assert read_json(state / "playlists.json")["version"] == 1, "the first file was rewritten"
    assert (state / "schedules.json").read_bytes() == before["schedules.json"]
    assert {path.name: path.read_bytes() for path in backup.iterdir()} == before

    # What the printed cp does puts the profile back as it was.
    for copy in backup.iterdir():
        shutil.copy2(copy, state / copy.name)
    assert {name: (state / name).read_bytes() for name in touched} == before


def test_a_backup_that_fails_refuses_before_anything_is_rewritten(
    in_use: tuple[Golden, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden, _playlist = in_use
    state = golden.profile.app_state
    copy = shutil.copy2

    def full_disk(source: Path, destination: Path) -> object:
        if Path(source).name == "schedules.json":
            raise OSError(errno.ENOSPC, "No space left on device")
        return copy(source, destination)

    monkeypatch.setattr(shutil, "copy2", full_disk)
    before = harness.snapshot(golden.profile.home)

    status, _out, err = _run(capsys, "--apply")

    assert status == rollback.EXIT_REFUSED
    assert "refused, nothing was written: could not back up the files" in err, err
    assert "No space left on device" in err and "that folder was left as it is" in err, err
    (backup,) = state.glob(f"{rollback.BACKUP_PREFIX}*")
    folder = f"{STATE}/{backup.name}"
    harness.check_changes(
        harness.diff(before, harness.snapshot(golden.profile.home)),
        [
            Allowance(folder, frozenset({"created"}), "the backup folder, never deleted"),
            Allowance(
                f"{folder}/playlists.json",
                frozenset({"created"}),
                "the one copy made before the failure",
            ),
        ],
    )
