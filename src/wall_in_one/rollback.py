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
back, also when a write fails part way, together with which files were
already rewritten.

Every file is read, checked, backed up and narrowed as one generation
(:class:`Generation`): it is read once, and its store's own parser is given
exactly those bytes, never the path again; the copy must hold those bytes,
and, under that store's writer lock, the file must still be them. A file
another writer replaced in between, say with a newer version's, is refused,
never narrowed over.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import stat
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
#: A write failed after the backup was made: some files may be rewritten.
EXIT_INCOMPLETE: Final = 2
#: How long ``--apply`` waits for the profile lock before refusing. Another
#: Wall-in-One operation (a migration, an upgrade, a health sync) holds it
#: only briefly; a person can simply run the tool again.
PROFILE_LOCK_TIMEOUT_SECONDS: Final = 5.0
STOP_FIRST: Final = (
    "Close Wall-in-One and stop its service first "
    "(systemctl --user stop wall-in-one.service), then run this again."
)


class RollbackRefusedError(Exception):
    """The profile cannot be narrowed safely; nothing was written."""


class RollbackIncompleteError(Exception):
    """A write failed after the backup was made.

    ``rewritten`` were written (or removed) before it, or by it: a file that
    was replaced but whose folder could not be synced is rewritten, and is
    also in ``not_durable``. ``untouched`` were not. Every one of them is in
    ``backup`` as it was before the run.
    """

    def __init__(
        self,
        reason: str,
        *,
        backup: Path,
        rewritten: tuple[Path, ...],
        untouched: tuple[Path, ...],
        not_durable: tuple[Path, ...] = (),
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.backup = backup
        self.rewritten = rewritten
        self.untouched = untouched
        self.not_durable = not_durable


def _published_not_durable(error: BaseException) -> BaseException | None:
    """The publication-without-folder-sync behind ``error``, if that is what failed.

    A store's writer wraps :class:`state_file.PublishedNotDurableError` in its
    own local-io error, so the chain of causes is searched.
    """
    found: BaseException | None = error
    while found is not None:
        if isinstance(
            found,
            state_file.PublishedNotDurableError | runtime_config.RuntimeConfigNotDurableError,
        ):
            return found
        found = found.__cause__
    return None


@dataclass(frozen=True, slots=True)
class Generation:
    """One file exactly as this run read it.

    The plan is built from this generation, the backup must hold its bytes,
    and the file is rewritten only while it is still this generation: the
    same inode, size and modification time, and the same bytes.
    """

    path: Path
    identity: tuple[int, int, int, int]
    digest: str


class GenerationChangedError(Exception):
    """A file is no longer the generation the plan was made from."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"{path.name} changed after this run read it, so it was left as it is, "
            "not narrowed; run this again to plan from the file as it is now"
        )
        self.path = path


#: Far larger than any store or overrides file; a bigger one is refused, not read.
MAX_CAPTURE_BYTES: Final = 64 * 1024 * 1024


def _capture(path: Path) -> tuple[Generation, bytes] | None:
    """Read ``path`` once: its bytes, and the generation they are.

    One descriptor, opened without following a symbolic link, on a regular
    file. The identity is that descriptor's, and the digest is of exactly the
    bytes returned, so whatever is parsed from them is that generation; a
    file written in place while it is read is refused. ``None`` without a
    file.
    """
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RollbackRefusedError(f"cannot read {path.name} ({error}); repair it first") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RollbackRefusedError(f"{path.name} is not a regular file; repair it first")
        contents = bytearray()
        while chunk := os.read(descriptor, 1 << 16):
            contents += chunk
            if len(contents) > MAX_CAPTURE_BYTES:
                raise RollbackRefusedError(f"{path.name} is too large; repair it first")
        after = os.fstat(descriptor)
    except OSError as error:
        raise RollbackRefusedError(f"cannot read {path.name} ({error}); repair it first") from error
    finally:
        os.close(descriptor)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise GenerationChangedError(path)
    data = bytes(contents)
    generation = Generation(
        path,
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
        hashlib.sha256(data).hexdigest(),
    )
    return generation, data


def _generation(path: Path) -> Generation | None:
    """``path``'s current generation, or ``None`` when there is no file."""
    captured = _capture(path)
    return None if captured is None else captured[0]


def _read_generation[T](
    path: Path, parse: Callable[[Path, bytes], T]
) -> tuple[Generation, T] | None:
    """``parse`` of exactly the bytes one read returned, and their generation.

    ``None`` without a file. The path is never read a second time, so no
    other file put there for a moment can be what is parsed.
    """
    captured = _capture(path)
    if captured is None:
        return None
    generation, data = captured
    return generation, parse(path, data)


def _store_parser[T](read: Callable[[Path], T]) -> Callable[[Path, bytes], T]:
    """A store's own reader, given the captured bytes instead of the path."""

    def parse(path: Path, data: bytes) -> T:
        with state_file.read_snapshots({path: data}):
            return read(path)

    return parse


def _require_unchanged(generation: Generation) -> None:
    """Raise :class:`GenerationChangedError` unless the file is still ``generation``."""
    try:
        current = _generation(generation.path)
    except RollbackRefusedError, GenerationChangedError:
        current = None
    if current != generation:
        raise GenerationChangedError(generation.path)


@dataclass(frozen=True, slots=True)
class FileStep:
    """One file to narrow: what changes, and the write that does it."""

    path: Path
    version: int | None
    target: int
    dropped: tuple[str, ...]
    write: Callable[[], object]
    generation: Generation

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
    overrides_generation: Generation | None

    @property
    def overrides(self) -> Path | None:
        """``runtime-overrides.toml`` when it is to be removed."""
        found = self.overrides_generation
        return found.path if found is not None else None

    @property
    def empty(self) -> bool:
        return not self.steps and self.overrides is None

    def generations(self) -> tuple[Generation, ...]:
        """Every file this plan changes, as it was read."""
        found = [step.generation for step in self.steps]
        if self.overrides_generation is not None:
            found.append(self.overrides_generation)
        return tuple(found)

    def touched(self) -> tuple[Path, ...]:
        return tuple(generation.path for generation in self.generations())

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
    read = _read_generation(path, _store_parser(playlists._read))
    if read is None:
        return None
    generation, reading = read
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
        generation,
    )


def _schedules_step(path: Path) -> FileStep | None:
    read = _read_generation(path, _store_parser(schedules._read))
    if read is None:
        return None
    generation, reading = read
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
        generation,
    )


def _displays_step(path: Path) -> FileStep | None:
    read = _read_generation(path, _store_parser(displays._read))
    if read is None:
        return None
    generation, reading = read
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
        generation,
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
    sidecar = runtime_config.overrides_path(paths.runtime_config_path())

    def parse_overrides(path: Path, data: bytes) -> str:
        if len(data) > runtime_config.MAX_RUNTIME_OVERRIDES_BYTES:
            raise RollbackRefusedError(f"{path.name} is too large; repair it first")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RollbackRefusedError(f"cannot read {path}: {error}; repair it first") from error

    read = _read_generation(sidecar, parse_overrides)
    if read is None:
        return Plan(tuple(steps), None)
    generation, text = read
    if runtime_config.overrides_from_a_newer_build(text):
        raise RollbackRefusedError(
            f"{sidecar.name} was written by a version newer than 0.2.0; "
            "roll back with that version first"
        )
    return Plan(tuple(steps), generation)


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
        except OSError as error:
            raise RollbackRefusedError(
                f"could not create a backup folder in {state} ({error})"
            ) from error
        return candidate
    raise RollbackRefusedError(f"could not create a backup folder in {state}")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _back_up(generations: Sequence[Generation], backup: Path) -> None:
    """Copy each file byte for byte, flushed, before anything is written.

    Each copy must hold exactly the generation the plan was made from, and
    the file must still be that generation once it is copied; otherwise the
    run is refused. A failure refuses the run before any file is rewritten.
    Whatever was already copied stays in the folder: a backup is never deleted.
    """
    try:
        for generation in generations:
            source = generation.path
            destination = backup / source.name
            shutil.copy2(source, destination)
            copied = _generation(destination)
            if copied is None or copied.digest != generation.digest:
                raise GenerationChangedError(source)
            _require_unchanged(generation)
            with destination.open("rb") as handle:
                os.fsync(handle.fileno())
        _fsync_directory(backup)
        _fsync_directory(backup.parent)
    except GenerationChangedError as error:
        raise RollbackRefusedError(
            f"{error}. Nothing was rewritten; the backup folder {backup} was left as it is"
        ) from error
    except OSError as error:
        raise RollbackRefusedError(
            f"could not back up the files into {backup} ({error}); that folder was left as it is"
        ) from error


def apply(*, when: datetime | None = None) -> tuple[Plan, Path | None]:
    """Narrow the profile; return the plan carried out and the backup folder.

    The plan is made again under the profile lock, which is held until the
    last write, so it is what was on disk then. A plan with nothing to do
    writes nothing, not even a backup folder. Raises
    :class:`RollbackRefusedError` before anything is rewritten, and
    :class:`RollbackIncompleteError` when a write fails after the backup.
    """
    with ExitStack() as stack:
        try:
            stack.enter_context(
                legacy_migration.profile_transaction(timeout=PROFILE_LOCK_TIMEOUT_SECONDS)
            )
        except legacy_migration.MigrationError as error:
            raise RollbackRefusedError(
                f"another Wall-in-One operation holds the profile ({error}); try again"
            ) from error
        _refuse_running_writers()
        plan = make_plan()
        if plan.empty:
            return plan, None
        state = paths.app_state_dir()
        backup = _backup_directory(state, when or datetime.now())
        _back_up(plan.generations(), backup)
        rewritten: list[Path] = []
        writing: Path | None = None
        try:
            stack.enter_context(runtime_config.compiler_lock())
            for step in plan.steps:
                writing = step.path
                # Under the store's own writer lock, so a cooperating writer
                # cannot slip in between this check and the narrowing.
                with state_file.mutation_lock(step.path, description=step.path.stem):
                    _require_unchanged(step.generation)
                    step.write()
                rewritten.append(step.path)
            if plan.overrides_generation is not None:
                writing = plan.overrides_generation.path
                # The compiler lock above is the overrides file's writer lock.
                _require_unchanged(plan.overrides_generation)
                runtime_config._publish_overrides(None, paths.runtime_config_path())
                rewritten.append(writing)
        except GenerationChangedError as error:
            if not rewritten:
                raise RollbackRefusedError(
                    f"{error}. Nothing was rewritten; the backup folder {backup} was left as it is"
                ) from error
            raise RollbackIncompleteError(
                str(error),
                backup=backup,
                rewritten=tuple(rewritten),
                untouched=tuple(path for path in plan.touched() if path not in rewritten),
            ) from error
        except Exception as error:
            # Whatever failed (a store's local-io refusal, a lock timeout, a
            # full disk), the person has to learn where the backup is.
            reason = str(error) or type(error).__name__
            not_durable: tuple[Path, ...] = ()
            uncertain = _published_not_durable(error)
            if uncertain is not None and writing is not None:
                # Replaced, so rewritten: only its survival of a power loss
                # is unconfirmed. Stop anyway; the disk is misbehaving.
                rewritten.append(writing)
                not_durable = (writing,)
                reason = str(uncertain)
            raise RollbackIncompleteError(
                reason,
                backup=backup,
                rewritten=tuple(rewritten),
                untouched=tuple(path for path in plan.touched() if path not in rewritten),
                not_durable=not_durable,
            ) from error
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


def _report_incomplete(error: RollbackIncompleteError) -> None:
    def names(found: tuple[Path, ...]) -> str:
        return (
            ", ".join(
                path.name
                + (
                    " (saved, but not yet safe from a power loss)"
                    if path in error.not_durable
                    else ""
                )
                for path in found
            )
            or "none"
        )

    lines = [
        f"wall-in-one-rollback: stopped part way: {error.reason}",
        f"Already rewritten: {names(error.rewritten)}.",
        f"Not rewritten: {names(error.untouched)}.",
        f"Every one of them, as it was before this run, is in {error.backup}.",
        "Fix the problem and run this again, or put them back:",
        *(f"  {command}" for command in _restore_commands(error.backup)),
    ]
    print("\n".join(lines), file=sys.stderr)


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
    except (RollbackRefusedError, GenerationChangedError) as error:
        print(f"wall-in-one-rollback: refused, nothing was written: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except RollbackIncompleteError as error:
        _report_incomplete(error)
        return EXIT_INCOMPLETE
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
