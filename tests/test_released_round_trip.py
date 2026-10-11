"""Going back to the released 0.2.2 and returning: what each version does with the other's files.

The startup probes run 0.2.2 as the producer and this checkout as the
consumer. Here the trip is the other way and back: this checkout produces,
under one fault at a time (`tests.released_faults`), the released 0.2.2 then
does what it ordinarily does on what was left (opens the download folder a
day later, which is its sweep; replays removals; scans; runs its Tidy up),
and this checkout takes its turn again. At each hop nothing that was ever
public loses a byte beyond what ``docs/updating.md`` ("Going back from
0.3.0") says 0.2.x does, and what that section claims is checked as it goes:

- 0.2.x doesn't see this version's ``.wall-in-one-v2-…`` and
  ``.wall-in-one-commit-…`` names: none changes during its turn, and none
  becomes a wallpaper.
- 0.2.x's own cleanup empties its older names once they are a day old.
- A removal this version stopped before the media claim is cancelled by
  0.2.x, which does not put the download record back; one stopped after the
  claim is finished by it.
"""

from __future__ import annotations

import json
import os
import time
import types
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_download as downloads
from tests import test_released_leftovers_removals as removals
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import tidy
from wall_in_one.library import scan
from wall_in_one.providers import download
from wall_in_one.providers.base import ProviderError

#: This checkout, as a producer: the same scripts the released code runs.
SOURCE = Path(__file__).resolve().parents[1] / "src"
_RUN = '    result = session.prepare_removal_plan(item, trash=case == "trash").run()\n'
assert _RUN in removals.PRODUCER
#: The removal, as this version's window runs it: its plan begins with a dry
#: run, and one that can't be made raises with nothing written (0.2.2 had no
#: dry run, and its plan always returned).
REMOVAL = removals.PRODUCER.replace(
    _RUN,
    "    try:\n    "
    + _RUN
    + "    except Exception as error:\n"
    + '        print("REFUSED before anything was touched:", type(error).__name__, flush=True)\n'
    + "        return\n",
)
LATER_SECONDS = 25 * 3600
STORES = {"favourites.json", "pairings.json", "playlists.json", "pending-removals.json"}

#: The released 0.2.2's turn. ``downloads``: every file is a day old, and it
#: opens the download folder (its sweep). ``removals``: it starts (replay,
#: then the scan). Then, either way, its scan's verdict and its Tidy up.
RELEASED_TURN = r"""
import os
import time
import types
from pathlib import Path

from wall_in_one import config, file_io, tidy
from wall_in_one.library import scan
from wall_in_one.providers import download
from wall_in_one.providers.base import ProviderError
from wall_in_one.session import Session

trace_filesystem((file_io, "_rename_noreplace"))
LATER = 25 * 3600


def _tidied(root, later):
    said = []
    found = tidy.plan(roots=(root,), now=later)
    for action in found.actions:
        if action.action in tidy.OFFERED and action.ready:
            said.append(tidy.apply(action.action, action, roots=(root,)).message)
    return " + ".join(said) or "nothing offered"


def operation(what):
    root = Path(os.environ["HOME"]) / "Wallpapers"
    said = []
    disarm()
    if what == "downloads":
        old = time.time() - LATER
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                os.utime(path, (old, old))
    arm()
    try:
        if what == "downloads":
            try:
                download.managed_directory(root, download.MOTIONBGS_LOCATION)
                said.append("opened")
            except ProviderError as error:
                said.append(f"REFUSED {error}")
            library = scan.scan((root,))
        else:
            session = Session(config.load_strict(require_present=True))
            failures = session.retry_removals()
            library = session.refresh()
            said.append("replay: " + ("; ".join(failures) or "nothing to say"))
        hidden = sorted(
            item.path.name
            for item in library.items
            if any(part.startswith(".") for part in item.path.relative_to(root).parts)
        )
        said.append(f"scan: {len(library.items)}" + (f" HIDDEN {hidden}" if hidden else ""))
        later = time.time() + LATER
        tidy.time = types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
        said.append("tidy: " + _tidied(root, later))
    except Exception as error:
        said.append(f"RAISED {type(error).__name__}: {error}")
    print(" / ".join(said), flush=True)


serve(operation)
"""


def _root(base: Path) -> Path:
    return Path(faults.profile_environment(base)["HOME"]) / "Wallpapers"


def _is_new_name(path: Path, base: Path) -> bool:
    return any(
        part.startswith((".wall-in-one-v2-", download.COMMIT_PREFIX))
        for part in path.relative_to(base).parts
    )


def _archived_holding_bytes(base: Path, before: faults.Tree, after: faults.Tree) -> str:
    """A finding if 0.2.2's Tidy up archived anything but an empty file.

    What it does archive in a profile this version wrote is empty: the
    tombstones under ``.wall-in-one-retained/`` (in a download folder, the
    stills folder, the state folder) and the emptied media of a finished
    claim (``.wall-in-one-removal-<token>/entry``). Never a withdrawn download
    record, a retained copy that holds bytes, or a pending claim: those this
    version would look for when it comes back.
    """
    was = {entry.inode: entry for entry in before.entries}
    moved = sorted(
        f"{was[entry.inode].path.relative_to(base)} ({entry.size} bytes)"
        for entry in after.entries
        if "tidy-archive" in entry.path.parts
        and entry.size
        and entry.inode in was
        and "tidy-archive" not in was[entry.inode].path.parts
    )
    return f"FINDING 0.2.2's Tidy up archived what still holds bytes: {moved}" if moved else ""


def _profile_of(path: Path) -> Path:
    return next(parent for parent in path.parents if parent.name.startswith(("a0", "b0")))


def _is_older_name(path: Path) -> bool:
    return path.name.startswith(download.LEGACY_STAGING_PREFIXES)


def _tidied(root: Path) -> str:
    later = time.time() + LATER_SECONDS
    said = []
    found = tidy.plan(roots=(root,), now=later)
    for action in found.actions:
        if action.action in tidy.OFFERED and action.ready:
            said.append(tidy.apply(action.action, action, roots=(root,)).message)
    kept = sorted({kept.reason for kept in found.action(tidy.LEFTOVERS).kept})
    return " / ".join(said) + (f" [kept: {'; '.join(kept)}]" if kept else "")


def _opened_a_day_later(base: Path, *, tidy_up: bool = True) -> Iterator[tuple[str, str]]:
    """This checkout's turn at the download folder: its sweep, its scan, its Tidy up.

    A day later: this version's sweep takes only what was left alone for a day
    by both of a file's times, and a change time can't be set back. So besides
    dating every file a day back (what the released sweep goes by), the sweep
    that opening the folder runs is asked what it would do a day and an hour on.
    """
    root = _root(base)
    old = time.time() - LATER_SECONDS
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            os.utime(path, (old, old))
    try:
        directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
        download.recover_abandoned(
            directory, download.MOTIONBGS_LOCATION, now=time.time() + LATER_SECONDS
        )
        yield "open the folder", "opened"
    except ProviderError as error:
        yield "open the folder", f"REFUSED {error}"
    library = scan.scan((root,))
    hidden = [
        item.path.name
        for item in library.items
        if any(part.startswith(".") for part in item.path.relative_to(root).parts)
    ]
    yield "scan", f"FINDING hidden names became wallpapers: {hidden}" if hidden else "scanned"
    if not tidy_up:
        return
    try:
        yield "tidy up", _tidied(root)
    except (tidy.TidyError, OSError) as error:
        yield "tidy up", f"REFUSED {type(error).__name__}: {error}"


def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hundreds of profiles: durability isn't what this probes.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    later = time.time() + LATER_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )


def _released_turn(source: Path, what: str, bases: Sequence[Path]) -> list[faults.Produced]:
    return faults.run(source, RELEASED_TURN, [faults.Job(base, {}, (what,)) for base in bases])


def _downloads(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> None:
    _quiet(monkeypatch)

    def judge(base: Path, before: faults.Tree, after: faults.Tree, said: str) -> str:
        if archived := _archived_holding_bytes(base, before, after):
            return archived
        touched = sorted(
            str(entry.path.relative_to(base))
            for entry in before.entries
            if _is_new_name(entry.path, base) and after.at(entry.path) != entry
        )
        if touched:
            return f"FINDING the docs say 0.2.x doesn't see these, and it changed {touched}: {said}"
        return (
            f"FINDING a hidden name became a wallpaper in 0.2.2: {said}" if "HIDDEN" in said else ""
        )

    found = faults.round_trip(
        monkeypatch,
        SOURCE,
        downloads.PRODUCER,
        (),
        tmp_path,
        prepare=downloads._prepare,
        between=lambda bases: _released_turn(released_source, "downloads", bases),
        consume=_opened_a_day_later,
        exempt_between=lambda entry, _before, _after: _is_older_name(entry.path),
        # Known, by design: this version's own day-old sweep reclaims its v2
        # and commit names even when a person edited one. Such a name is
        # never public by construction (AB-1), which is why the sweep may
        # reclaim it; the app keeps no record of what a crash-left partial
        # should hold, so it can't tell an edit from its own partial bytes.
        # The older names, which may have been public, it never touches.
        # `test_a_v2_stage_a_person_edited_is_reclaimed_a_day_after_the_edit`.
        exempt=lambda entry, _before: _is_new_name(entry.path, _profile_of(entry.path)),
        judge=judge,
        edited_after=2 if full else 5,
        full=full,
    )
    print("\n" + found.summary("a download: this version, 0.2.2 a day later, this version"))
    assert any(
        ".wall-in-one-v2-" in name or download.COMMIT_PREFIX in name
        for state in found.states
        for name, _size in state
    ), "no state had one of this version's temporary names for 0.2.2 to meet"
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


def test_a_download_survives_going_back_and_returning(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What a gate runs: a kill before, and a failure at, every change."""
    _downloads(released_source, tmp_path, monkeypatch, full=False)


@faults.full_campaign
def test_a_download_survives_going_back_and_returning_under_any_plan(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _downloads(released_source, tmp_path, monkeypatch, full=True)


def test_the_released_cleanup_empties_its_own_older_names_after_a_day(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.2.2 leaves a stage (killed before each change); this version keeps it,
    whatever its age; back in 0.2.2 a day later, its own cleanup empties it.
    That is the docs' second claim, and the reason they say to archive first."""
    _quiet(monkeypatch)
    plain = faults.Job(tmp_path / "plain", {}, ())
    downloads._prepare(plain.base)
    (trace,) = faults.run(released_source, downloads.PRODUCER, [plain])
    jobs = [
        faults.Job(tmp_path / f"k{number:03d}", {"kill": call.index}, ())
        for number, call in enumerate(trace.changes)
    ]
    for job in jobs:
        downloads._prepare(job.base)
    faults.run(released_source, downloads.PRODUCER, jobs)
    held: list[tuple[faults.Job, list[faults.Entry]]] = []
    for job in jobs:
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            # Its sweep and its scan; not its Tidy up, which would offer to
            # archive what the sweep kept.
            steps = dict(_opened_a_day_later(job.base, tidy_up=False))
        assert not any(said.startswith("FINDING") for said in steps.values()), steps
        older = [
            entry
            for entry in faults.tree(job.base).entries
            if _is_older_name(entry.path) and entry.size and "tidy-archive" not in str(entry.path)
        ]
        held.append((job, older))
    turns = _released_turn(released_source, "downloads", [job.base for job, _older in held])
    kept_by_this_version = sum(len(older) for _job, older in held)
    still_holding = [
        (str(entry.path.relative_to(job.base)), turn.output)
        for (job, older), turn in zip(held, turns, strict=True)
        for entry in older
        if entry.path.is_file() and entry.path.stat().st_size
    ]
    print(
        f"\nolder names this version kept: {kept_by_this_version}; 0.2.2 left holding bytes: "
        f"{len(still_holding)}"
    )
    assert kept_by_this_version, "this version kept at least one of 0.2.2's older names"
    assert not still_holding, still_holding[:5]


def _removals(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> faults.Campaign:
    _quiet(monkeypatch)
    consumer = removals._Consumer("remove")

    def facts(base: Path, found: faults.Tree) -> tuple[bool, bool, bool, bool]:
        media = consumer.media(base)
        record = media.with_name(media.name + ".motionbgs.json")
        journal = found.at(base / "state" / "wall-in-one" / "pending-removals.json")
        pending = False
        if journal is not None:
            try:
                pending = any(
                    not intent.get("committed")
                    for intent in json.loads(journal.data).get("removals", [])
                )
            except ValueError, AttributeError:
                pending = False
        claimed = any(
            entry.data == removals.MEDIA and ".wall-in-one-removal" in str(entry.path)
            for entry in found.entries
        )
        return found.at(media) is not None, claimed, found.at(record) is not None, pending

    def judge(base: Path, before: faults.Tree, after: faults.Tree, said: str) -> str:
        if archived := _archived_holding_bytes(base, before, after):
            return archived
        there, claimed, record, pending = facts(base, before)
        there_after, claimed_after, record_after, _pending = facts(base, after)
        if not pending:
            return f"{said} [no pending intent]"
        if there:
            if not there_after:
                return f"FINDING stopped before the claim, and 0.2.2 removed the media: {said}"
            if record_after and not record:
                return f"FINDING the docs say 0.2.x doesn't put the record back; it did: {said}"
            return (
                f"{said} [before the claim: media kept, record {'there' if record else 'not back'}]"
            )
        if claimed:
            if there_after or claimed_after:
                return f"FINDING stopped after the claim, and 0.2.2 did not finish it: {said}"
            return f"{said} [after the claim: finished]"
        return f"{said} [media already gone]"

    def exempt_between(entry: faults.Entry, _before: faults.Tree, after: faults.Tree) -> bool:
        # 0.2.2 finishing a removal takes the claimed media and what goes with
        # it; and it rewrites the journal and the stores it takes the entry out of.
        if entry.path.name in STORES:
            return True
        base = next(parent for parent in entry.path.parents if parent.name.startswith(("a0", "b0")))
        if after.at(consumer.media(base)) is not None:
            return False
        return entry.data == removals.MEDIA or entry in consumer._beside(base, _before)

    found = faults.round_trip(
        monkeypatch,
        SOURCE,
        REMOVAL,
        ("remove",),
        tmp_path,
        prepare=removals._prepare,
        between=lambda bases: _released_turn(released_source, "removals", bases),
        consume=consumer.consume,
        exempt_between=exempt_between,
        exempt=consumer.exempt,
        judge=judge,
        edited_after=2 if full else 5,
        full=full,
    )
    print("\n" + found.summary("a removal: this version, 0.2.2's start, this version's start"))
    return found


def test_a_removal_survives_going_back_and_returning(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What a gate runs: a kill before, and a failure at, every change."""
    found = _removals(released_source, tmp_path, monkeypatch, full=False)
    said = " ".join(found.outcomes)
    assert "before the claim: media kept" in said and "after the claim: finished" in said
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


@faults.full_campaign
def test_a_removal_survives_going_back_and_returning_under_any_plan(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = _removals(released_source, tmp_path, monkeypatch, full=True)
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


def test_a_v2_stage_a_person_edited_is_reclaimed_a_day_after_the_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterisation of a known case (see `_downloads`): this version's
    download dies leaving a ``.wall-in-one-v2-…-stage-*`` file, and a person
    appends a note to it. The sweep's age is the later of the file's two
    times, and nothing can set its change time back, so the edit restarts the
    day whatever its modification time is then set to: swept now, the edited
    stage is kept; with its mtime set back a day, it is still kept; a day
    after the edit it is reclaimed. Nothing at a public name changes."""
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    plain = faults.Job(tmp_path / "plain", {}, ())
    downloads._prepare(plain.base)
    (trace,) = faults.run(SOURCE, downloads.PRODUCER, [plain])
    jobs = [
        faults.Job(tmp_path / f"a{number:05d}", {"kill": call.index}, ())
        for number, call in enumerate(trace.changes)
    ]
    for job in jobs:
        downloads._prepare(job.base)
    faults.run(SOURCE, downloads.PRODUCER, jobs)
    tried = 0
    for job in jobs:
        root = _root(job.base)
        stages = [
            entry
            for entry in faults.tree(root).entries
            if entry.path.name.startswith(".wall-in-one-v2-") and entry.size
        ]
        if not stages:
            continue
        tried += 1
        stage = stages[0].path
        public = {
            entry.path: entry.data
            for entry in faults.tree(job.base).entries
            if not any(part.startswith(".") for part in entry.path.relative_to(job.base).parts)
        }
        faults.edit_in_place(stage, "extend")
        edited = stage.read_bytes()
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            download.managed_directory(root, download.MOTIONBGS_LOCATION)
            assert stage.read_bytes() == edited, "edited a moment ago: the sweep leaves it"
            old = time.time() - LATER_SECONDS
            os.utime(stage, (old, old))
            directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
            assert stage.read_bytes() == edited, "its mtime set back: still edited a moment ago"
            # A change time can't be set back, so the same sweep is asked what
            # it would do a day and an hour on.
            download.recover_abandoned(
                directory, download.MOTIONBGS_LOCATION, now=time.time() + LATER_SECONDS
            )
        assert not stage.exists() or not stage.stat().st_size, "a day after the edit: reclaimed"
        now = {path: path.read_bytes() for path in public if path.is_file()}
        assert now == public, "nothing at a public name changed"
    assert tried, "at least one kill left a v2 stage holding bytes"
