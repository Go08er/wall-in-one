"""Undo of a Tidy up archive the released 0.2.2 made, or did not finish, by this version.

Both versions write the same manifest (kind and version) into
``tidy-archive/``, so this version offers Undo for an archive 0.2.2 made. The
leftovers are made by the real producer, never laid out by hand: the released
removal, stopped before each of its changes (`tests.released_faults`), which
0.2.2's own start then finishes and its Tidy up archives.

- 0.2.2 starts on what its removal left and archives; this version runs Undo,
  starts twice and tidies.
- 0.2.2 dies before each change of that start (its replay, then its archive);
  this version runs Undo on whatever manifest that left.
- 0.2.2 archives, and its own Undo dies before each of its changes; this
  version's Undo then puts everything back.

What must hold at every hop: no file loses a byte; an Undo either puts each
item back exactly where its manifest says it came from, or leaves it in the
archive with the manifest saying so, or refuses with a reason; this version
never raises on 0.2.2's archive; and what it says it put back is what moved.

Updates are one-way: 0.2.2 is only ever the producer here. It never runs over
what this version archived or undid.

`leftover_states`, `again`, `TIDY` and `_Probe` are also what
`tests/test_disk_full_gaps.py` builds its Undo-under-a-full-disk campaign from.
"""

from __future__ import annotations

import json
import os
import time
import types
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_download as downloads
from tests import test_released_leftovers_removals as removals
from tests.released import released_source  # noqa: F401 -- a fixture
from tests.turns import RELEASED_TURN, SOURCE, _released_turn
from wall_in_one import file_io, tidy
from wall_in_one.providers import download

LATER_SECONDS = 25 * 3600

#: A version's Tidy up, a day later: ``archive`` applies what its plan
#: offers of the leftovers; ``undo`` puts the latest archive back.
TIDY = r"""
import os
import time
import types
from pathlib import Path

from wall_in_one import file_io, tidy

trace_filesystem((file_io, "_rename_noreplace"))


def operation(what):
    root = Path(os.environ["HOME"]) / "Wallpapers"
    later = time.time() + 25 * 3600
    tidy.time = types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    try:
        if what == "archive":
            found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
            if found.ready:
                print("ARCHIVED", tidy.apply(tidy.LEFTOVERS, found, roots=(root,)).message)
            else:
                print("NOTHING", found.blocked or "nothing offered")
        else:
            result = tidy.undo(tidy.LEFTOVERS)
            print("UNDID" if result.changed else "NOTHING", result.message)
    except tidy.TidyError as error:
        print("REFUSED", error)
    except OSError as error:
        # The app's lane says an error's own words: a refusal, with its reason.
        print("REFUSED OSError:", error)
    except Exception as error:
        print("RAISED", type(error).__name__, error)
    sys.stdout.flush()


serve(operation)
"""


def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hundreds of profiles: durability isn't what this probes.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    later = time.time() + LATER_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )


def _root(base: Path) -> Path:
    return Path(faults.profile_environment(base)["HOME"]) / "Wallpapers"


def _in_archive(path: Path) -> bool:
    return tidy.ARCHIVE_DIRECTORY in path.parts


def _is_bookkeeping(entry: faults.Entry) -> bool:
    """An archive's own manifest or note: rewritten at every step, by design."""
    name = entry.path.name
    # (So are a manifest's own temporary, where a kill stopped its write, and
    # the empty record of one retired when its replacement failed: whatever
    # an archive holds outside ``items/`` is its own.)
    return _in_archive(entry.path) and (
        name in (tidy.MANIFEST, "README.txt")
        or name.startswith(f".{tidy.MANIFEST}.")
        or (file_io.RETAINED_ENTRY_DIRECTORY in entry.path.parts and not entry.size)
    )


def _kinds(found: faults.Tree, base: Path) -> Counter[str]:
    """What kind of entry each archived file was, by the name it had."""
    kinds: Counter[str] = Counter()
    for entry in found.entries:
        if not _in_archive(entry.path) or _is_bookkeeping(entry):
            continue
        name = entry.path.name
        if download.COMMIT_PREFIX.lstrip(".") in name:  # archived without its leading dot
            kinds["a commit name"] += 1
        elif any(prefix[1:] in name for prefix in download.LEGACY_STAGING_PREFIXES):
            kinds["an older stage"] += 1
        else:
            kinds["another file"] += 1
    return kinds


@dataclass(slots=True)
class _Probe:
    """One profile through its hops: what was found, and what each hop said."""

    findings: list[str] = field(default_factory=list)
    outcomes: Counter[str] = field(default_factory=Counter)
    archived: Counter[str] = field(default_factory=Counter)

    def said(self, hop: str, words: str, base: Path) -> None:
        told = faults._RANDOM.sub("*", words.replace(str(base), ""))
        self.outcomes[f"{hop}: {told[:200]}"] += 1

    def hop(
        self,
        label: str,
        name: str,
        before: faults.Tree,
        after: faults.Tree,
        base: Path,
        *,
        exempt: Sequence[tuple[Path, bytes]] = (),
    ) -> None:
        """Nothing ``before`` held has lost its bytes ``after`` (manifests aside)."""
        allowed = set(exempt)
        everything = frozenset(entry.inode for entry in before.entries)
        for entry in faults.lost(
            before,
            after,
            everything,
            lambda entry: _is_bookkeeping(entry) or (entry.path, entry.data) in allowed,
        ):
            self.findings.append(
                f"LOST {label}: after {name}, the {entry.size} bytes of "
                f"{entry.path.relative_to(base)} are nowhere (or in fewer places)"
            )

    def undone(
        self,
        label: str,
        who: str,
        words: str,
        before: faults.Tree,
        after: faults.Tree,
        base: Path,
    ) -> None:
        """An Undo's words against what moved: each archived item is back where its
        manifest says it came from, or still archived with the manifest saying why."""
        if words.startswith("RAISED") or not words.startswith(("UNDID", "NOTHING", "REFUSED")):
            self.findings.append(f"FINDING {label}: {who}'s Undo did not end cleanly: {words}")
            return
        was = {entry.inode: entry for entry in before.entries}
        still = [
            entry
            for entry in after.entries
            if _in_archive(entry.path) and not _is_bookkeeping(entry) and entry.inode in was
        ]
        back = [
            entry
            for entry in after.entries
            if not _in_archive(entry.path)
            and entry.inode in was
            and _in_archive(was[entry.inode].path)
            and not _is_bookkeeping(was[entry.inode])
        ]
        if words.startswith("UNDID") and not back and not self._folders_back(before, after):
            self.findings.append(f"SAID {label}: {who} says it put items back; none moved: {words}")
        nothing = words.startswith("NOTHING There") or "othing was changed" in words
        if nothing and back:
            self.findings.append(
                f"SAID {label}: {who} says nothing was put back; "
                f"{[str(entry.path.relative_to(base)) for entry in back]} moved: {words}"
            )
        for manifest in after.named(tidy.MANIFEST):
            try:
                document = json.loads(manifest.data)
            except ValueError:
                self.findings.append(
                    f"FINDING {label}: after {who}'s Undo, "
                    f"{manifest.path.relative_to(base)} does not parse"
                )
                continue
            for record in document.get("items", []):
                original = Path(record["original"])
                archived = manifest.path.parent / record["archived"]
                outcome = record.get("outcome")
                if outcome == "restored" and not os.path.lexists(original):
                    self.findings.append(
                        f"FINDING {label}: after {who}'s Undo the manifest says "
                        f"{original.relative_to(base)} was restored, and it isn't there"
                    )
                if outcome in ("moved", "planned") and document.get("state") == "undone":
                    self.findings.append(
                        f"FINDING {label}: after {who}'s Undo the archive is marked undone "
                        f"with {record['archived']} still recorded as archived"
                    )
                if outcome == "moved" and not (
                    os.path.lexists(archived) or os.path.lexists(original)
                ):
                    self.findings.append(
                        f"FINDING {label}: after {who}'s Undo, {record['archived']} is "
                        "recorded as archived and is in neither place"
                    )
        if words.startswith("UNDID") and "couldn't" not in words and "neither" not in words:
            left = [str(entry.path.relative_to(base)) for entry in still]
            # An archive from before this one (an older Undo's partial) may still hold items.
            latest = max(
                (entry.path.parent for entry in after.named(tidy.MANIFEST)),
                default=None,
                key=lambda directory: directory.name,
            )
            mine = [name for name in left if latest is not None and latest.name in name]
            if mine:
                self.findings.append(
                    f"SAID {label}: {who} says everything was put back; {mine} is still archived"
                )

    @staticmethod
    def _folders_back(before: faults.Tree, after: faults.Tree) -> bool:
        """An Undo that only put empty folders back moved no file: told by the manifests."""
        for manifest in after.named(tidy.MANIFEST):
            earlier = before.at(manifest.path)
            if earlier is None or earlier.data == manifest.data:
                continue
            try:
                now = json.loads(manifest.data)
                then = json.loads(earlier.data)
            except ValueError:
                continue
            restored = sum(record.get("outcome") == "restored" for record in now.get("items", []))
            if restored > sum(
                record.get("outcome") == "restored" for record in then.get("items", [])
            ):
                return True
        return False

    def summary(self, title: str) -> str:
        lines = [f"{title}: archived {dict(sorted(self.archived.items()))}"]
        lines += [f"  {count:5d}  {said}" for said, count in sorted(self.outcomes.items())]
        return "\n".join(lines)


def _tidy_here(base: Path, what: str) -> str:
    """This checkout's Tidy up in this process, a day later: archive, or undo."""
    root = _root(base)
    later = time.time() + LATER_SECONDS
    try:
        if what == "archive":
            found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
            if found.ready:
                return "ARCHIVED " + tidy.apply(tidy.LEFTOVERS, found, roots=(root,)).message
            return "NOTHING " + (found.blocked or "nothing offered")
        result = tidy.undo(tidy.LEFTOVERS)
        return ("UNDID " if result.changed else "NOTHING ") + result.message
    except tidy.TidyError as error:
        return f"REFUSED {error}"
    except OSError as error:
        return f"REFUSED OSError: {error}"
    except Exception as error:
        return f"RAISED {type(error).__name__}: {error}"


def _killed_at_each_change(
    source: Path, script: str, arguments: tuple[str, ...], tmp_path: Path, tag: str, every: int
) -> list[faults.Job]:
    """``script`` stopped before each of its changes (one in ``every``), and once not at all."""
    plain = faults.Job(tmp_path / f"{tag}-plain", {}, arguments)
    prepare = removals._prepare if arguments else downloads._prepare
    prepare(plain.base)
    (trace,) = faults.run(source, script, [plain])
    assert trace.status == 0, trace.output + trace.errors[-2000:]
    jobs = [faults.Job(tmp_path / f"{tag}-a00000", {}, arguments)] + [
        faults.Job(tmp_path / f"{tag}-a{number + 1:05d}", {"kill": call.index}, arguments)
        for number, call in enumerate(trace.changes[::every])
    ]
    for job in jobs:
        prepare(job.base)
    faults.run(source, script, jobs)
    return jobs


def _the_released_version_archives_and_this_one_undoes(
    released: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, full: bool
) -> _Probe:
    _quiet(monkeypatch)
    probe = _Probe()
    consumer = removals._Consumer("remove")
    jobs = _killed_at_each_change(
        released, removals.PRODUCER, ("remove",), tmp_path, "gone", 1 if full else 3
    )
    # 0.2.2 starts (its replay finishes or cancels the removal), scans, and
    # applies its Tidy up a day later.
    turns = _released_turn(released, "removals", [job.base for job in jobs])
    for job, turn in zip(jobs, turns, strict=True):
        label = f"{job.base.name} {dict(job.plan)}"
        archived = faults.tree(job.base)
        probe.said("0.2.2 starts and archives", turn.output, job.base)
        probe.archived += _kinds(archived, job.base)
        if turn.status != 0 or "RAISED" in turn.output:
            probe.findings.append(f"FINDING {label}: 0.2.2's turn: {turn.output} {turn.errors}")
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            mine = _tidy_here(job.base, "undo")
            after = faults.tree(job.base)
            probe.said("this version undoes", mine, job.base)
            probe.hop(label, "this version's Undo", archived, after, job.base)
            probe.undone(label, "this version", mine, archived, after, job.base)
            # Its ordinary life afterwards: two starts and its own Tidy up.
            before = after
            for name, said in consumer.consume(job.base):
                now = faults.tree(job.base)
                probe.said(f"this version, {name}", said, job.base)
                if said.startswith("FINDING") or "RAISED" in said:
                    probe.findings.append(f"{said} [{label}, at {name}]")
                probe.hop(
                    label,
                    f"this version's {name}",
                    before,
                    now,
                    job.base,
                    exempt=[
                        (entry.path, entry.data)
                        for entry in before.entries
                        if consumer.exempt(entry, before)
                    ],
                )
                before = now
    return probe


def test_this_version_undoes_the_released_versions_archive(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _the_released_version_archives_and_this_one_undoes(
        released_source, tmp_path, monkeypatch, full=bool(os.environ.get(faults.FULL))
    )
    print("\n" + probe.summary("0.2.2 archives, this version undoes"))
    assert probe.archived, "0.2.2's Tidy up archived something for this version to undo"
    assert not probe.findings, "\n".join(sorted(set(probe.findings))[:30])


# -- an archive the released version did not finish ----------------------------------------------


def _archivable(base: Path) -> str:
    """What a download left in ``base`` that this version's Tidy up archives, by kind."""
    names = [entry.path.name for entry in faults.tree(_root(base)).entries if entry.size]
    if any(name.startswith(download.COMMIT_PREFIX) for name in names):
        return "a commit name"
    if any(name.startswith(download.LEGACY_STAGING_PREFIXES) for name in names):
        return "an older stage"
    return ""


def leftover_states(released: Path, tmp_path: Path) -> dict[str, list[tuple[Path, faults.Job]]]:
    """Each kind of file only this version archives, as a real download left it:
    0.2.x's own stage (its download, stopped) and this version's commit name."""
    chosen: dict[str, list[tuple[Path, faults.Job]]] = {}
    for tag, source in (("old", released), ("new", SOURCE)):
        for job in _killed_at_each_change(source, downloads.PRODUCER, (), tmp_path, tag, 1):
            if kind := _archivable(job.base):
                chosen.setdefault(kind, []).append((source, job))
    assert set(chosen) == {"a commit name", "an older stage"}, sorted(chosen)
    return chosen


def again(source: Path, job: faults.Job, tmp_path: Path, names: Sequence[str]) -> list[faults.Job]:
    """The leftover of ``job`` in fresh profiles, each as that download left it."""
    fresh = [faults.Job(tmp_path / name, job.plan, ()) for name in names]
    for each in fresh:
        downloads._prepare(each.base)
    faults.run(source, downloads.PRODUCER, fresh)
    return fresh


def test_this_version_undoes_an_archive_the_released_version_did_not_finish(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.2.2 starts on a finished removal of its own and dies before each change
    of that start (its replay, then its Tidy up's archive); this version runs
    Undo on whatever manifest that left, then starts twice and tidies."""
    _quiet(monkeypatch)
    full = bool(os.environ.get(faults.FULL))
    probe = _Probe()
    consumer = removals._Consumer("remove")
    turn = RELEASED_TURN

    def finished(names: Sequence[str]) -> list[faults.Job]:
        made = [faults.Job(tmp_path / name, {}, ("remove",)) for name in names]
        for each in made:
            removals._prepare(each.base)
        faults.run(released_source, removals.PRODUCER, made)
        return made

    (traced,) = finished(["trace"])
    (trace,) = faults.run(released_source, turn, [faults.Job(traced.base, {}, ("removals",))])
    assert "Archived" in trace.output, trace.output + trace.errors[-1000:]
    changes = trace.changes if full else trace.changes[::2]
    jobs = finished([f"a{number:05d}" for number in range(len(changes))])
    faults.run(
        released_source,
        turn,
        [
            faults.Job(each.base, {"kill": call.index}, ("removals",))
            for each, call in zip(jobs, changes, strict=True)
        ],
    )
    for each, call in zip(jobs, changes, strict=True):
        label = f"0.2.2's start killed before change {call.index} ({call.name})"
        before = faults.tree(each.base)
        probe.archived += _kinds(before, each.base)
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, each.base)
            mine = _tidy_here(each.base, "undo")
            after = faults.tree(each.base)
            probe.said("this version undoes", mine, each.base)
            probe.hop(label, "this version's Undo", before, after, each.base)
            probe.undone(label, "this version", mine, before, after, each.base)
            earlier = after
            for name, said in consumer.consume(each.base):
                now = faults.tree(each.base)
                probe.said(f"this version, {name}", said, each.base)
                if said.startswith("FINDING") or "RAISED" in said:
                    probe.findings.append(f"{said} [{label}, at {name}]")
                probe.hop(
                    label,
                    f"this version's {name}",
                    earlier,
                    now,
                    each.base,
                    exempt=[
                        (entry.path, entry.data)
                        for entry in earlier.entries
                        if consumer.exempt(entry, earlier)
                    ],
                )
                earlier = now
    print("\n" + probe.summary(f"an unfinished archive of 0.2.2, {len(changes)} of them"))
    assert not probe.findings, "\n".join(sorted(set(probe.findings))[:30])


# -- an Undo the released version did not finish -------------------------------------------------


def test_this_version_finishes_an_undo_the_released_version_did_not(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.2.2 archives what its own finished removal left, and its Undo dies
    before each of its changes; this version's Undo then puts everything back."""
    _quiet(monkeypatch)
    full = bool(os.environ.get(faults.FULL))
    probe = _Probe()

    def archived_by_the_released_version(names: Sequence[str]) -> list[faults.Job]:
        made = [faults.Job(tmp_path / name, {}, ("remove",)) for name in names]
        for each in made:
            removals._prepare(each.base)
        faults.run(released_source, removals.PRODUCER, made)
        for turn in _released_turn(released_source, "removals", [each.base for each in made]):
            assert "Archived" in turn.output, turn.output + turn.errors[-1000:]
        return made

    (traced,) = archived_by_the_released_version(["trace"])
    (trace,) = faults.run(released_source, TIDY, [faults.Job(traced.base, {}, ("undo",))])
    assert trace.output.startswith("UNDID"), trace.output + trace.errors[-1000:]
    changes = trace.changes if full else trace.changes[::2]
    jobs = archived_by_the_released_version([f"a{number:05d}" for number in range(len(changes))])
    archived = [faults.tree(each.base) for each in jobs]
    faults.run(
        released_source,
        TIDY,
        [
            faults.Job(each.base, {"kill": call.index}, ("undo",))
            for each, call in zip(jobs, changes, strict=True)
        ],
    )
    for each, call, was in zip(jobs, changes, archived, strict=True):
        label = f"0.2.2's Undo killed before change {call.index} ({call.name})"
        before = faults.tree(each.base)
        probe.archived += _kinds(was, each.base)
        probe.hop(label, "0.2.2's unfinished Undo", was, before, each.base)
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, each.base)
            mine = _tidy_here(each.base, "undo")
        after = faults.tree(each.base)
        probe.said("this version finishes the Undo", mine, each.base)
        probe.hop(label, "this version's Undo", before, after, each.base)
        probe.undone(label, "this version", mine, before, after, each.base)
        if "couldn't" not in mine:
            still = sorted(
                str(entry.path.relative_to(each.base))
                for entry in after.entries
                if _in_archive(entry.path) and not _is_bookkeeping(entry)
            )
            if still:
                probe.findings.append(
                    f"FINDING {label}: after this version finished the Undo, {still} is still "
                    f"archived: {mine}"
                )
    print("\n" + probe.summary(f"0.2.2's Undo stopped, this version finishes it, {len(changes)}"))
    assert probe.archived, "0.2.2's Tidy up archived something for its Undo to put back"
    assert changes, "0.2.2's Undo makes changes to stop before"
    assert not probe.findings, "\n".join(sorted(set(probe.findings))[:30])
