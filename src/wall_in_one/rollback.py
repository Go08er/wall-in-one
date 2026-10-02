"""``wall-in-one-rollback``: rewrite the files 0.1.4 reads in 0.1.4's formats.

0.2.0 is not designed to be rolled back. It reads every 0.1.4 file as it is,
but moves three of them to a newer version the first time a new field is used
(a rule name, a playlist's own interval or shuffle, a display's own playlist
beating global rules). 0.1.4 predates the guard that opens such a file
read-only: it pauses every edit and shows an empty library until it is
updated again, though nothing is lost and the wallpaper keeps running.

This optional tool is the way back. It narrows the *current* contents of
``playlists.json``, ``schedules.json`` and ``displays.json`` to the versions
0.1.4 reads (1, 2 and 1), dropping only the new fields and keeping every
record and entry. It never restores a ``.v<n>-backup``: that would discard
every edit made since the bump. It also removes ``runtime-overrides.toml``,
which 0.1.4 never reads. ``pairings.json``, ``favourites.json``,
``pending-removals.json`` and ``settings.toml`` are already in formats 0.1.4
reads, and ``ui.toml`` and Tidy up's archives are files it ignores; all of
them are left as they are. ``runtime.toml`` needs nothing either: 0.1.4's
service start compiles it again from the narrowed files.

By default it only prints the plan. ``--apply`` refuses while the app or the
service is running, holds the profile lock for the whole run (the lock the
companion's keep-alive waits for), copies every file it will change into one
dated folder under the state directory, and then writes each file with its
store's own atomic writer. It prints the two commands that put the copies
back.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from wall_in_one import legacy_migration, paths, predecessor_process, runtime_config
from wall_in_one.library import displays, playlists, schedules, state_file

#: The newest version of each file 0.1.4 (dbfbaa0) reads and writes: its
#: ``FORMAT_VERSION`` in ``src/wall_in_one/library``.
V0_1_4_FORMATS: Final = {
    playlists.STATE_FILENAME: 1,
    schedules.STATE_FILENAME: 2,
    displays.STATE_FILENAME: 1,
}
#: Files this tool leaves alone because 0.1.4 reads them as they are.
UNCHANGED_FORMATS: Final = (
    "pairings.json",
    "favourites.json",
    "pending-removals.json",
    "settings.toml",
)
BACKUP_PREFIX: Final = "rollback-to-0.1.4-"
EXIT_REFUSED: Final = 1
STOP_FIRST: Final = (
    "Close Wall-in-One and stop its service first "
    "(systemctl --user stop wall-in-one.service), then run this again."
)


class RollbackRefusedError(Exception):
    """The profile cannot be narrowed safely; nothing was written."""


@dataclass(frozen=True, slots=True)
class FileStep:
    """One file to narrow: what changes, and the write that does it."""

    path: Path
    version: int | None
    target: int
    dropped: tuple[str, ...]
    write: Callable[[], object]

    def describe(self) -> list[str]:
        if self.version is None or self.version <= self.target:
            heading = f"{self.path.name}: written again as version {self.target}"
        else:
            heading = f"{self.path.name}: version {self.version} -> {self.target}"
        if not self.dropped:
            return [f"{heading}; no field is lost"]
        return [heading, *(f"  {line}" for line in self.dropped)]


@dataclass(frozen=True, slots=True)
class Plan:
    steps: tuple[FileStep, ...]
    overrides: Path | None

    @property
    def empty(self) -> bool:
        return not self.steps and self.overrides is None

    def touched(self) -> tuple[Path, ...]:
        found = [step.path for step in self.steps]
        if self.overrides is not None:
            found.append(self.overrides)
        return tuple(found)

    def describe(self) -> list[str]:
        if self.empty:
            return ["Nothing to roll back: every file is already in a format 0.1.4 reads."]
        lines: list[str] = []
        for step in self.steps:
            lines.extend(step.describe())
        if self.overrides is not None:
            lines.append(
                f"{self.overrides.name}: removed (0.1.4 never reads it; per-playlist "
                "intervals and shuffle and the display opt-in stop applying)"
            )
        lines.append("Every playlist, entry, schedule rule and display assignment is kept.")
        return lines


def _readable(name: str, reading: state_file.Reading[Any]) -> None:
    if reading.newer_version:
        raise RollbackRefusedError(
            f"{name} was saved by a version newer than 0.2.0 ({reading.fault}); "
            "roll back with that version's own tool first"
        )
    if reading.fault is not None:
        raise RollbackRefusedError(f"{name} cannot be read ({reading.fault}); repair it first")
    if reading.unknown:
        raise RollbackRefusedError(
            f"{name} has fields from a version newer than 0.2.0, which narrowing would "
            "drop without saying what they were; roll back with that version first"
        )


def _playlists_step(path: Path) -> FileStep | None:
    if not path.exists():
        return None
    reading = playlists._read(path)
    _readable(path.name, reading)
    target = V0_1_4_FORMATS[path.name]
    if reading.version is not None and reading.version <= target:
        return None
    dropped: list[str] = []
    narrowed: dict[str, playlists.Playlist] = {}
    for identifier, playlist in sorted(reading.value.items()):
        if playlist.cycle_interval is not None:
            dropped.append(
                f'playlist "{playlist.name}" loses its interval ({playlist.cycle_interval} s)'
            )
        if playlist.shuffle is not None:
            setting = "on" if playlist.shuffle else "off"
            dropped.append(f'playlist "{playlist.name}" loses its own shuffle ({setting})')
        narrowed[identifier] = replace(playlist, cycle_interval=None, shuffle=None)
    return FileStep(
        path,
        reading.version,
        target,
        tuple(dropped),
        lambda: playlists.save(narrowed, path, version=target),
    )


def _schedules_step(path: Path) -> FileStep | None:
    if not path.exists():
        return None
    reading = schedules._read(path)
    _readable(path.name, reading)
    target = V0_1_4_FORMATS[path.name]
    if reading.version is not None and reading.version <= target:
        return None
    dropped = tuple(f'rule "{rule.name}" loses its name' for rule in reading.value if rule.name)
    narrowed = tuple(replace(rule, name=None) for rule in reading.value)
    return FileStep(
        path,
        reading.version,
        target,
        dropped,
        lambda: schedules.save(narrowed, path, version=target),
    )


def _displays_step(path: Path) -> FileStep | None:
    if not path.exists():
        return None
    reading = displays._read(path)
    _readable(path.name, reading)
    target = V0_1_4_FORMATS[path.name]
    if reading.version is not None and reading.version <= target:
        return None
    arrangement = reading.value
    dropped = tuple(
        f"display {connector} loses its own playlist beating global schedule rules"
        for connector in sorted(arrangement.beats_global_rules)
    )
    assignments = dict(arrangement.assignments)
    return FileStep(
        path,
        reading.version,
        target,
        dropped,
        lambda: displays.save(assignments, path, version=target),
    )


def make_plan() -> Plan:
    """What ``--apply`` would do now. Reads only; raises :class:`RollbackRefusedError`."""
    state = paths.app_state_dir()
    steps = [
        step
        for step in (
            _playlists_step(state / playlists.STATE_FILENAME),
            _schedules_step(state / schedules.STATE_FILENAME),
            _displays_step(state / displays.STATE_FILENAME),
        )
        if step is not None
    ]
    runtime = paths.runtime_config_path()
    sidecar = runtime_config.overrides_path(runtime)
    try:
        text = runtime_config._read_overrides(runtime)
    except runtime_config.RuntimeConfigError as error:
        raise RollbackRefusedError(f"{error}; repair it first") from error
    if text is not None and runtime_config.overrides_from_a_newer_build(text):
        raise RollbackRefusedError(
            f"{sidecar.name} was written by a version newer than 0.2.0; "
            "roll back with that version first"
        )
    return Plan(tuple(steps), sidecar if text is not None else None)


def _refuse_running_writers() -> None:
    """Observe the app's and the service's singleton guards without taking them."""
    try:
        predecessor_process.refuse_live_writer_status()
        predecessor_process.refuse_live_predecessor_runtime()
    except predecessor_process.PredecessorProcessError as error:
        raise RollbackRefusedError(f"{STOP_FIRST} ({error})") from error


def _backup_directory(state: Path, when: datetime) -> Path:
    stamp = when.strftime("%Y%m%d-%H%M%S")
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt}"
        candidate = state / f"{BACKUP_PREFIX}{stamp}{suffix}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate
    raise RollbackRefusedError(f"could not create a backup folder in {state}")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _back_up(files: Sequence[Path], backup: Path) -> None:
    """Copy each file byte for byte, flushed, before anything is written."""
    for source in files:
        destination = backup / source.name
        shutil.copy2(source, destination)
        with destination.open("rb") as handle:
            if handle.read() != source.read_bytes():
                raise RollbackRefusedError(f"the backup of {source.name} does not match it")
            os.fsync(handle.fileno())
    _fsync_directory(backup)
    _fsync_directory(backup.parent)


def apply(*, when: datetime | None = None) -> tuple[Plan, Path | None]:
    """Narrow the profile; return the plan carried out and the backup folder.

    The plan is made again under the profile lock, so it is what was on disk
    then. A plan with nothing to do writes nothing, not even a backup folder.
    """
    with ExitStack() as stack:
        try:
            stack.enter_context(legacy_migration.profile_transaction())
        except (OSError, TimeoutError) as error:
            raise RollbackRefusedError(
                f"another Wall-in-One operation holds the profile ({error}); try again"
            ) from error
        _refuse_running_writers()
        plan = make_plan()
        if plan.empty:
            return plan, None
        state = paths.app_state_dir()
        backup = _backup_directory(state, when or datetime.now())
        _back_up(plan.touched(), backup)
        stack.enter_context(runtime_config.compiler_lock())
        for step in plan.steps:
            with state_file.mutation_lock(step.path, description=step.path.stem):
                step.write()
        if plan.overrides is not None:
            runtime_config._publish_overrides(None, paths.runtime_config_path())
        return plan, backup


def _restore_commands(backup: Path) -> list[str]:
    state = paths.app_state_dir()
    return [
        "systemctl --user stop wall-in-one.service",
        f"cp -p -- {shlex.quote(str(backup))}/* {shlex.quote(str(state))}/",
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wall-in-one-rollback",
        description=(
            "Rewrite playlists.json, schedules.json and displays.json in the formats "
            "Wall-in-One 0.1.4 reads, keeping every record, and remove "
            "runtime-overrides.toml. Without --apply it only prints what it would do."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="do it: back up the files first, then rewrite them (the app must be closed)",
    )
    return parser


def _left_alone() -> list[str]:
    return [
        f"{', '.join(UNCHANGED_FORMATS)}: already in formats 0.1.4 reads; left as they are.",
        "ui.toml and tidy-archive/: 0.1.4 ignores them; left as they are.",
        "runtime.toml: 0.1.4's service start compiles it again from these files.",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    try:
        if not options.apply:
            plan = make_plan()
            print("Wall-in-One rollback to 0.1.4: a dry run; nothing is written.\n")
            print("\n".join(plan.describe()))
            if not plan.empty:
                print("\n" + "\n".join(_left_alone()))
                print(
                    "\nRun with --apply to do it, with the app closed and the service "
                    "stopped. The files are backed up first."
                )
            return 0
        plan, backup = apply()
    except RollbackRefusedError as error:
        print(f"wall-in-one-rollback: refused, nothing was written: {error}", file=sys.stderr)
        return EXIT_REFUSED
    if backup is None:
        print("\n".join(plan.describe()))
        return 0
    print("Wall-in-One rollback to 0.1.4: done.\n")
    print("\n".join(plan.describe()))
    print("\n" + "\n".join(_left_alone()))
    print(f"\nThe previous files are in {backup}. To put them back:")
    for command in _restore_commands(backup):
        print(f"  {command}")
    print("\nInstall 0.1.4 and start its service when you are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
